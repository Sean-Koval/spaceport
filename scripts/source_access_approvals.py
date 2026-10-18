"""Explicit owner review/import/revoke commands; no provider credential access."""
import asyncio
import json
from pathlib import Path

from gateway.contracts.canonical import canonical_bytes
from gateway.source_access_contracts import MAX_APPROVAL_BYTES, validate_source_approval
from gateway.source_access_operator import (OperatorError, build_review_request, install_approval,
    revoke_approval, validate_package_approval, load_registry_approvals)
from scripts.spaceport_files import check_output, parse_json, read_bytes, write_bytes, write_json


def configure_parser(parser):
    actions = parser.add_subparsers(dest='action', required=True)
    for name in ('review-request', 'canonicalize', 'import', 'reference', 'revoke'):
        command = actions.add_parser(name)
        if name != 'canonicalize': command.add_argument('--config', required=True)
        if name in {'review-request', 'import', 'reference'}:
            command.add_argument('--package-id', required=True)
            command.add_argument('--revision', required=True, type=int)
        if name in {'canonicalize', 'import', 'reference'}: command.add_argument('--approval-file', required=True)
        if name in {'review-request', 'canonicalize', 'reference'}: command.add_argument('--output', required=True)
        if name in {'import', 'revoke'}: command.add_argument('--receipt-file', required=True)
        if name in {'revoke', 'reference'}: command.add_argument('--approval-id', required=True)
        if name == 'revoke':
            command.add_argument('--expected-digest', required=True)
            command.add_argument('--reason-code', required=True)
    return parser


def _load(args):
    from gateway.source_pilot_config import load_source_pilot_config
    from gateway.source_pilot_assets import load_source_assets
    path = Path(args.config).absolute()
    config = load_source_pilot_config(str(path))
    assets = load_source_assets(config, path.parent)
    return config, assets


def _package(assets, args):
    found = [row for row in assets.packages.packages
        if (row.package.package_id, row.package.revision) == (args.package_id, args.revision)]
    if len(found) != 1: raise ValueError('Source approval package is unavailable')
    return found[0]


async def _registry(config, operation, *, role='owner'):
    from gateway.registry.repository import Registry
    config.validate_files(role=role)
    registry = Registry(config.database_url(role))
    try:
        return await operation(registry)
    finally:
        await registry.close()


def run(args):
    """One explicit action; stdout never contains review, source or secret data."""
    dispatched = False
    try:
        if args.action == 'canonicalize':
            check_output(args.output)
            raw = read_bytes(args.approval_file, limit=MAX_APPROVAL_BYTES, private=True)
            approval = validate_source_approval(canonical_bytes(parse_json(raw)))
            write_bytes(args.output, approval.canonical_content)
            result = dict(status='canonical-review-document', approval_state='unapproved')
        elif args.action == 'review-request':
            check_output(args.output)
            _, assets = _load(args)
            write_json(args.output, build_review_request(_package(assets, args)))
            result = dict(status='requires-owner-review', approval_state='unapproved')
        elif args.action == 'reference':
            from gateway.source_pilot_config import SourceApprovalRef
            check_output(args.output)
            config, assets = _load(args); retained = _package(assets, args)
            approval = validate_source_approval(read_bytes(args.approval_file, limit=MAX_APPROVAL_BYTES, private=True))
            validate_package_approval(retained, approval)
            ref = SourceApprovalRef(package_id=retained.package.package_id, revision=retained.package.revision,
                approval_id=args.approval_id, approval_revision=approval.revision, approval_digest=approval.content_digest)
            asyncio.run(_registry(config, lambda registry: load_registry_approvals(registry, ((ref, retained),)), role='app'))
            write_json(args.output, ref.model_dump(mode='json'))
            result = dict(status='reference-verified', authority='runtime-recheck-required')
        else:
            check_output(args.receipt_file)
            if args.action == 'import':
                config, assets = _load(args)
                retained = _package(assets, args)
                raw = read_bytes(args.approval_file, limit=MAX_APPROVAL_BYTES, private=True)
                validate_package_approval(retained, validate_source_approval(raw))
                operation = lambda registry: install_approval(registry, retained, raw)
            elif args.action == 'revoke':
                # Revocation must remain possible after local package assets change.
                from gateway.source_pilot_config import load_source_pilot_config
                config = load_source_pilot_config(str(Path(args.config).absolute()))
                operation = lambda registry: revoke_approval(registry, args.approval_id, args.expected_digest, args.reason_code)
            else:
                raise ValueError('Unknown source approval action')
            dispatched = True
            receipt = asyncio.run(_registry(config, operation))
            write_json(args.receipt_file, receipt)
            result = dict(status='recorded', operation=args.action, state=receipt['state'])
        print(json.dumps(result, sort_keys=True))
        return 0
    except OperatorError as error:
        print(json.dumps(dict(status='uncertain' if error.uncertain else 'unavailable', code='SOURCE_APPROVAL_UNAVAILABLE'), sort_keys=True))
        return 3 if error.uncertain else 2
    except Exception:
        print(json.dumps(dict(status='uncertain' if dispatched else 'unavailable', code='SOURCE_APPROVAL_UNAVAILABLE'), sort_keys=True))
        return 3 if dispatched else 2


def main(argv=None):
    from scripts.spaceport import Parser
    try:
        return run(configure_parser(Parser(description='Explicit protected source approvals')).parse_args(argv))
    except Exception:
        print(json.dumps(dict(status='unavailable', code='SOURCE_APPROVAL_UNAVAILABLE'), sort_keys=True))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
