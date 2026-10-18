"""Transactional PostgreSQL registry primitives for internal gateway services."""
from contextlib import asynccontextmanager
from uuid import UUID, uuid4
from weakref import WeakKeyDictionary

from pydantic import ValidationError
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from gateway.contracts.canonical import canonical_bytes
from gateway.contracts.errors import ErrorCode, GatewayError, invalid_input
from gateway.contracts.execution import PinnedProduct
from gateway.contracts.manifest import Manifest, validate_manifest
from .models import AuditEvent, BindingRevision, DraftRevision, PolicyRevision, PolicyRow, ProductRow, ProductVersion, TestRun, VersionState


def conflict():
    return GatewayError(ErrorCode.CONFLICT, str(uuid4()), "The registry revision or publication conflicts with current state.")


def not_found():
    return GatewayError(ErrorCode.NOT_FOUND_OR_FORBIDDEN, str(uuid4()), "The requested product is unavailable.")


def database_error(error: DBAPIError):
    code = getattr(error.orig, "sqlstate", None)
    if code in {"23505", "40001", "40P01", "P0001"}:
        return conflict()
    return GatewayError(ErrorCode.UPSTREAM_UNAVAILABLE, str(uuid4()), "The registry is unavailable.")


class UnitOfWork:
    """One session/transaction. Lock heads before revisions in product→policy order.

    Locking reads autoflush pending writes, then refresh already-loaded heads.
    Callers may append through session.add and flush; escaping exceptions abort the
    whole transaction. Publication here is an evidence-integrity primitive, not
    publisher authorization or fixture orchestration (owned by Task 9).
    """
    def __init__(self, session: AsyncSession):
        self.session = session

    async def lock_product(self, product_id: str) -> ProductRow:
        row = await self.session.scalar(select(ProductRow).where(ProductRow.id == product_id).with_for_update().execution_options(populate_existing=True))
        if row is None:
            raise not_found()
        return row

    async def lock_draft(self, draft_id: str) -> DraftRevision:
        row = await self.session.scalar(select(DraftRevision).where(DraftRevision.id == draft_id).with_for_update())
        if row is None:
            raise not_found()
        return row

    async def lock_policy_head(self, policy_id: str) -> PolicyRow:
        row = await self.session.scalar(select(PolicyRow).where(PolicyRow.id == policy_id).with_for_update().execution_options(populate_existing=True))
        if row is None:
            raise not_found()
        return row

    async def lock_policy(self, policy_id: str) -> PolicyRevision:
        head = await self.lock_policy_head(policy_id)
        row = await self.session.scalar(select(PolicyRevision).where(
            PolicyRevision.policy_id == policy_id, PolicyRevision.revision == head.current_revision).with_for_update())
        if row is None:
            raise not_found()
        return row

    async def insert_version(self, *, draft_id: str, expected_revision: int,
                             test_run_id: str, actor_id: str, version_id: str | None = None,
                             source_operation_id: str | None = None) -> ProductVersion:
        # Read inert product ID first; all authoritative checks follow locked heads.
        product_id = await self.session.scalar(select(DraftRevision.product_id).where(DraftRevision.id == draft_id))
        if product_id is None:
            raise not_found()
        if source_operation_id is None:
            product = await self.lock_product(product_id)
            draft = await self.lock_draft(draft_id)
            manifest = validate_manifest(draft.manifest)
            policy = await self.lock_policy(manifest.policy_id)
        else:
            # The protected recorder already holds the complete C/P/D/Q/V/E/A
            # prefix. This suffix may read those rows but must acquire no locks.
            try:
                if type(source_operation_id) is not str or str(UUID(source_operation_id)) != source_operation_id:
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise not_found() from None
            receipt = (await self.session.execute(text("""SELECT * FROM gateway.source_publication_operations
                WHERE id=CAST(:id AS uuid) AND transaction_id=pg_current_xact_id()"""),
                {"id":source_operation_id})).mappings().one_or_none()
            if (receipt is None or receipt["action"] != "publish"
                or (receipt["product_id"],receipt["version_id"],receipt["draft_id"],receipt["test_run_id"],
                    receipt["actor_snapshot"].get("id"),receipt["expected_draft_revision"])
                != (product_id,version_id,draft_id,test_run_id,actor_id,expected_revision)):
                raise not_found()
            product = await self.session.get(ProductRow, product_id, populate_existing=True)
            draft = await self.session.get(DraftRevision, draft_id)
            if (product is None or draft is None or product.publication_revision != receipt["next_product_revision"]
                or product.source_operation_id != source_operation_id or product.current_version_id != receipt["previous_version_id"]):
                raise not_found()
            manifest = validate_manifest(draft.manifest)
            head = await self.session.get(PolicyRow,manifest.policy_id,populate_existing=True)
            policy = None if head is None else await self.session.scalar(select(PolicyRevision).where(
                PolicyRevision.policy_id==head.id,PolicyRevision.revision==head.current_revision))
            if policy is None:
                raise not_found()
        test = await self.session.get(TestRun, test_run_id)
        binding = await self.session.scalar(select(BindingRevision).where(
            BindingRevision.binding_id == manifest.binding_id,
            BindingRevision.revision == manifest.binding_revision))
        if (type(expected_revision) is not int or product.draft_revision != expected_revision
            or product.latest_draft_id != draft.id or draft.revision != expected_revision
            or draft.content_digest != manifest.content_digest or test is None or binding is None
            or test.status != "passed" or test.product_id != product.id or test.draft_id != draft.id
            or test.draft_digest != draft.content_digest or test.binding_id != binding.id
            or test.binding_digest != binding.content_digest or test.policy_id != policy.policy_id
            or test.policy_revision != policy.revision or binding.model_bundle_digest != manifest.model_bundle_digest
            or binding.adapter_revision != manifest.adapter_revision):
            raise conflict()
        row = ProductVersion(id=version_id or str(uuid4()), product_id=product.id,
            version=manifest.version, manifest=manifest.model_dump(mode="json"),
            manifest_canonical=canonical_bytes(manifest.model_dump(mode="python", exclude={"content_digest"})).decode("utf-8"),
            content_digest=draft.content_digest, binding_id=binding.id, binding_digest=binding.content_digest,
            policy_id=policy.policy_id, policy_revision=policy.revision, test_run_id=test.id,
            draft_id=draft.id, actor_id=actor_id, name=manifest.name,
            domain=manifest.domain, classification=manifest.classification,source_operation_id=source_operation_id)
        self.session.add(row)
        await self.session.flush()
        self.session.add(VersionState(version_id=row.id, status="published", actor_id=actor_id, reason="Published tested revision",
            source_operation_id=source_operation_id))
        product.current_version_id = row.id
        if source_operation_id is None:
            self.session.add(AuditEvent(actor_id=actor_id, action="publish", product_id=product.id,
                version_id=row.id, policy_revision=policy.revision, decision="allow", correlation_id=str(uuid4())))
        else:
            product.source_operation_id=source_operation_id
        await self.session.flush()
        return row


# Private construction provenance. Mutable SQLAlchemy URL display metadata is
# not proof of the endpoint originally given to the engine's connection pool.
# This scoped record is never serialized, logged or returned as credentials.
_REGISTRY_CONSTRUCTIONS = WeakKeyDictionary()


def _record_registry_construction(registry):
    from sqlalchemy.engine import Engine, URL
    from sqlalchemy.ext.asyncio import AsyncEngine
    from sqlalchemy.util import immutabledict
    engine, sessions = registry.engine, registry.sessions
    if type(registry) is not Registry or type(engine) is not AsyncEngine or type(sessions) is not async_sessionmaker:
        return
    sync = engine.sync_engine
    if type(sync) is not Engine or type(sync.pool) is not NullPool or type(sync.url) is not URL:
        return
    url = sync.url
    endpoint = (url.drivername, url.username, url.host, url.port, url.database)
    if (any(type(value) is not str for value in (endpoint[0], endpoint[1], endpoint[2], endpoint[4]))
        or endpoint[3] is not None and type(endpoint[3]) is not int or type(url.query) is not immutabledict):
        return
    pool = sync.pool
    _REGISTRY_CONSTRUCTIONS[registry] = (engine, sessions, sync, pool, url, endpoint, not bool(url.query),
        pool._creator_arg, pool._invoke_creator, sync.dialect)


def registry_construction_endpoint(registry):
    """Read a verified original endpoint tuple, or None after object/bind drift.

    Only the exact internally constructed object graph is inspected. A proxy,
    replaced URL, pool, callback or session binding cannot borrow provenance.
    """
    from sqlalchemy.engine import Engine
    from sqlalchemy.ext.asyncio import AsyncEngine
    if type(registry) is not Registry:
        return None
    retained = _REGISTRY_CONSTRUCTIONS.get(registry)
    if retained is None:
        return None
    engine, sessions, sync, pool, url, endpoint, no_query, creator, invoke, dialect = retained
    data = registry.__dict__
    if (type(data) is not dict or any(type(key) is not str for key in data)
        or data.get('engine') is not engine or data.get('sessions') is not sessions):
        return None
    if type(engine) is not AsyncEngine or type(sessions) is not async_sessionmaker or type(sync) is not Engine:
        return None
    for value in (sync.__dict__, pool.__dict__, sessions.__dict__):
        if type(value) is not dict or any(type(key) is not str for key in value):
            return None
    if engine.sync_engine is not sync or sync.pool is not pool or sync.url is not url or sync.dialect is not dialect:
        return None
    if type(pool) is not NullPool or pool._creator_arg is not creator or pool._invoke_creator is not invoke:
        return None
    values = sessions.__dict__
    if type(values) is not dict or type(values.get('kw')) is not dict or values.get('class_') is not AsyncSession:
        return None
    kwargs = values['kw']
    if (any(type(key) is not str for key in kwargs)
        or set(kwargs) != {'bind', 'autoflush', 'expire_on_commit'}
        or kwargs['bind'] is not engine or kwargs['autoflush'] is not True
        or kwargs['expire_on_commit'] is not False):
        return None
    return endpoint if no_query else None


class Registry:
    def __init__(self, database_url: str):
        url = make_url(database_url)
        if url.drivername != "postgresql+psycopg":
            raise ValueError("The registry requires PostgreSQL with the psycopg driver")
        # Never echo SQL/binds, credentials, JSON manifests or result data.
        self.engine = create_async_engine(url, echo=False, hide_parameters=True, poolclass=NullPool,
                                         connect_args={"connect_timeout": 5})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        _record_registry_construction(self)

    async def close(self):
        await self.engine.dispose()

    @asynccontextmanager
    async def transaction(self):
        try:
            async with self.sessions() as session:
                async with session.begin():
                    yield UnitOfWork(session)
        except DBAPIError as error:
            raise database_error(error) from None

    async def save_draft(self, product_id: str, manifest: Manifest, expected_revision: int, actor_id: str) -> DraftRevision:
        if type(expected_revision) is not int or expected_revision < 0 or not actor_id:
            raise invalid_input("The expected revision and actor are invalid.")
        if not isinstance(manifest, Manifest):
            raise invalid_input("A validated manifest is required.")
        # Revalidate serialized content even if an internal caller used model_construct.
        manifest = validate_manifest(manifest.model_dump(mode="json"))
        if manifest.product_id != product_id:
            raise invalid_input("The manifest belongs to another product.")
        async with self.transaction() as uow:
            await uow.session.execute(insert(ProductRow).values(id=product_id).on_conflict_do_nothing())
            product = await uow.lock_product(product_id)
            if product.draft_revision != expected_revision:
                raise conflict()
            row = DraftRevision(id=str(uuid4()), product_id=product_id, revision=expected_revision + 1,
                manifest=manifest.model_dump(mode="json"),
                manifest_canonical=canonical_bytes(manifest.model_dump(mode="python",exclude={"content_digest"})).decode("utf-8"),
                content_digest=manifest.content_digest, actor_id=actor_id)
            uow.session.add(row)
            await uow.session.flush()
            # Database CAS and unique revisions remain valid across multiple processes.
            changed = await uow.session.execute(update(ProductRow).where(ProductRow.id == product_id,
                ProductRow.draft_revision == expected_revision).values(
                    draft_revision=row.revision, latest_draft_id=row.id).returning(ProductRow.id))
            if changed.scalar_one_or_none() is None:
                raise conflict()
            uow.session.add(AuditEvent(actor_id=actor_id, action="save_draft", product_id=product_id,
                decision="allow", correlation_id=str(uuid4())))
            return row

    async def resolve(self, product_id: str, version: str | None = None) -> PinnedProduct:
        try:
            async with self.sessions() as session:
                stmt = select(ProductVersion, BindingRevision).join(BindingRevision,
                    ProductVersion.binding_id == BindingRevision.id).join(VersionState,
                    VersionState.version_id == ProductVersion.id).where(
                    ProductVersion.product_id == product_id, VersionState.status.in_(["published", "deprecated"]))
                if version is None:
                    stmt = stmt.join(ProductRow, ProductRow.current_version_id == ProductVersion.id).where(ProductRow.id == product_id)
                else:
                    stmt = stmt.where(ProductVersion.version == version)
                result = (await session.execute(stmt)).one_or_none()
                if result is None:
                    raise not_found()
                row, binding = result
                try:
                    manifest = validate_manifest(row.manifest)
                except GatewayError:
                    raise GatewayError(ErrorCode.BINDING_DRIFT, str(uuid4()), "The pinned product is invalid.") from None
                if manifest.content_digest != row.content_digest or binding.content_digest != row.binding_digest:
                    raise GatewayError(ErrorCode.BINDING_DRIFT, str(uuid4()), "The pinned product binding is unavailable.")
                return PinnedProduct(manifest=manifest, binding_digest=row.binding_digest, version_id=row.id)
        except DBAPIError as error:
            raise database_error(error) from None
        except ValidationError:
            raise GatewayError(ErrorCode.BINDING_DRIFT, str(uuid4()), "The pinned product is invalid.") from None
