"""Owner-only inert deployment provisioning. No publication or fake test evidence."""
import argparse
import asyncio
import logging
from pathlib import Path
from alembic import command
from alembic.config import Config
from sqlalchemy import select,text

from gateway.contracts.canonical import canonical_digest
from gateway.registry.models import BindingRevision,PolicyRow,PolicyRevision,PrincipalRow,PrincipalGrant
from gateway.registry.repository import Registry
from gateway.runtime_config import ROOT,load_assets,database_url
from gateway.logging import configure_logging


async def provision(owner: Registry):
    assets = load_assets()
    async with owner.transaction() as uow:
        await uow.session.execute(text('SELECT pg_advisory_xact_lock(72140214)'))
        head = await uow.session.get(PolicyRow,assets.policy_id)
        if head is not None:
            # Existing grants/active flags/policy heads belong to operators.
            # Verify original immutable deployment identities, never reset them.
            revision = await uow.session.scalar(select(PolicyRevision).where(
                PolicyRevision.policy_id==assets.policy_id,PolicyRevision.revision==1))
            if revision is None or revision.content_digest != canonical_digest(assets.policy_content):
                raise ValueError('Existing registry differs from the reviewed bootstrap')
            for binding in assets.bindings:
                row = await uow.session.scalar(select(BindingRevision).where(
                    BindingRevision.binding_id==binding.binding_id,BindingRevision.revision==binding.revision))
                if row is None or row.content_digest != binding.digest:
                    raise ValueError('Existing binding differs from the reviewed bootstrap')
            for account in assets.accounts:
                row = await uow.session.get(PrincipalRow,account['id'])
                if row is None or row.kind!='demo' or row.subject!='synthetic-'+account['id']:
                    raise ValueError('Existing identity differs from the reviewed bootstrap')
            return
        if (await uow.session.scalar(select(PrincipalRow.id).limit(1))) is not None:
            raise ValueError('Bootstrap requires an empty registry or its exact existing deployment')
        uow.session.add(PolicyRow(id=assets.policy_id,current_revision=1))
        await uow.session.flush()
        uow.session.add(PolicyRevision(id='demo-policy-1',policy_id=assets.policy_id,revision=1,
            content=assets.policy_content,content_digest=canonical_digest(assets.policy_content),actor_id='demo-operator'))
        for binding in assets.bindings:
            uow.session.add(BindingRevision(id='demo-'+binding.binding_id+'-1',binding_id=binding.binding_id,
                revision=binding.revision,content=binding.model_dump(mode='json'),content_digest=binding.digest,
                model_bundle_digest=binding.model_bundle_digest,adapter_revision=binding.adapter_revision,actor_id='demo-operator'))
        for account in assets.accounts:
            uow.session.add(PrincipalRow(id=account['id'],subject='synthetic-'+account['id'],kind='demo'))
        await uow.session.flush()
        for account in assets.accounts:
            if account['roles']:
                uow.session.add(PrincipalGrant(principal_id=account['id'],policy_id=assets.policy_id,
                    tenant_id=account['tenant_id'],roles=account['roles'],entitlements=account['entitlements']))


def migrate():
    from sqlalchemy import create_engine
    config = Config(str(ROOT/'alembic.ini'))
    config.set_main_option('script_location',str(ROOT/'migrations'))
    engine = create_engine(database_url('owner'),echo=False,hide_parameters=True,connect_args={'connect_timeout':5})
    try:
        with engine.begin() as connection:
            config.attributes['connection'] = connection
            command.upgrade(config,'head')
    finally: engine.dispose()


async def run_provision():
    owner = Registry(database_url('owner'))
    try: await provision(owner)
    finally: await owner.close()


def main():
    parser = argparse.ArgumentParser(description='Provision only the synthetic private demo')
    parser.add_argument('operation',choices=['migrate','provision'])
    args = parser.parse_args()
    configure_logging()
    try:
        migrate() if args.operation == 'migrate' else asyncio.run(run_provision())
    except Exception:
        logging.error('',extra={'event':'operation_failed'})
        return 1
    return 0


if __name__ == '__main__': raise SystemExit(main())
