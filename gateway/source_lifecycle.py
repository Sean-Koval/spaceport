"""Private, bounded source authority shared by queue and fixed fixture runners.

These values are never accepted from an HTTP/MCP request or product manifest.
Only the audited registry service may issue a SourceAttemptContext. Provider
adapters prepare SourceSessionSpec without a lease or a provider credential.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import re
from uuid import UUID
import json

from sqlalchemy import text


def _text(value, *, maximum=128):
    if type(value) is not str or not 1 <= len(value) <= maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Invalid bounded source identity")


def _digest(value):
    if type(value) is not str or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError("Invalid source fingerprint")


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("Invalid source bound")


def _uuid(value):
    if type(value) is not str:
        raise ValueError("Invalid source identity")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise ValueError("Invalid source identity") from None


@dataclass(frozen=True)
class SourceSessionSpec:
    provider: str
    source_id: str
    source_revision: int
    context: str
    capacity_key: str
    definition_digest: str
    request_digest: str
    prepared_digest: str
    scope_digest: str
    decision_digest: str
    binding_digest: str
    model_bundle_digest: str
    catalog_digest: str | None
    max_submissions: int
    max_bytes_per_submission: int | None
    max_total_bytes: int | None

    def __post_init__(self):
        if self.provider not in {"bigquery", "domo"} or self.context not in {"consumer", "fixture_test"}:
            raise ValueError("Unsupported source context")
        _text(self.source_id)
        _text(self.capacity_key, maximum=512)
        if re.fullmatch(r"[A-Za-z0-9:_-]+", self.capacity_key) is None:
            raise ValueError("Invalid source pool")
        _integer(self.source_revision, 1, 2**31 - 1)
        _integer(self.max_submissions, 1, 8)
        for name in ("definition_digest", "request_digest", "prepared_digest", "scope_digest",
                     "decision_digest", "binding_digest", "model_bundle_digest"):
            _digest(getattr(self, name))
        if self.catalog_digest is not None:
            _digest(self.catalog_digest)
        if self.provider == "bigquery":
            _integer(self.max_bytes_per_submission, 1, 2**53 - 1)
            _integer(self.max_total_bytes, self.max_bytes_per_submission, 2**53 - 1)
        elif (self.max_submissions != 1 or self.max_bytes_per_submission is not None
              or self.max_total_bytes is not None):
            raise ValueError("Domo has one send and no provider byte-cost bound")


@dataclass(frozen=True)
class SourceAttemptContext:
    group_id: str
    session_id: str
    generation: int
    authority_stamp: str
    context_digest: str
    deadline: datetime
    spec: SourceSessionSpec

    def __post_init__(self):
        _uuid(self.group_id)
        _uuid(self.session_id)
        _integer(self.generation, 1, 2**31 - 1)
        _digest(self.authority_stamp)
        _digest(self.context_digest)
        if (type(self.deadline) is not datetime or self.deadline.utcoffset() != timedelta(0)
                or type(self.spec) is not SourceSessionSpec):
            raise ValueError("Invalid committed source attempt")

    @property
    def request_id(self):
        return f"{self.session_id}.g{self.generation}"

    def wire_context(self):
        """Fresh serializable safe projection; contains neither rows nor lease token."""
        self.__post_init__()
        checked_spec = SourceSessionSpec(**asdict(self.spec))
        return {**asdict(checked_spec), "group_id": self.group_id, "session_id": self.session_id,
            "generation": self.generation, "authority_stamp": self.authority_stamp,
            "context_digest": self.context_digest, "deadline_at": self.deadline.isoformat()}


@dataclass(frozen=True)
class SourceStatus:
    session_id: str
    sealed: bool
    terminal: bool
    group_closed: bool
    group_settled: bool
    admitted_count: int
    unresolved_count: int

    def __post_init__(self):
        _uuid(self.session_id)
        for value in (self.sealed, self.terminal, self.group_closed, self.group_settled):
            if type(value) is not bool:
                raise ValueError("Invalid source status")
        _integer(self.admitted_count, 0, 8)
        _integer(self.unresolved_count, 0, self.admitted_count)
        if (self.terminal and self.unresolved_count
                or self.group_settled and not (self.group_closed and self.sealed and self.terminal)):
            raise ValueError("Inconsistent source status")

    @property
    def can_complete_session(self):
        return self.sealed and self.terminal


class DomoSendPermit:
    """One process-local exact-intent send right after a confirmed DB commit.

    This is an internal trusted-process capability, not a serializable token or
    Python sandbox. Only SourceLifecycle issues it; transport must consume start
    immediately before I/O. Restart/reclaim cannot reconstruct the nonce.
    """
    __slots__ = ("context", "plan_fingerprint", "_nonce", "_state")

    @classmethod
    def _from_committed(cls, context, plan_fingerprint, nonce):
        _digest(plan_fingerprint)
        _text(nonce, maximum=128)
        value = object.__new__(cls)
        value.context, value.plan_fingerprint = context, plan_fingerprint
        value._nonce, value._state = nonce, "unused"
        return value

    def __new__(cls, *args, **kwargs):
        raise TypeError("Send permits require committed source authority")

    def __repr__(self):
        return "DomoSendPermit(<redacted>)"

    def start_io(self, plan_fingerprint):
        if self._state != "unused" or plan_fingerprint != self.plan_fingerprint:
            raise ValueError("The exact source send permit is unavailable")
        self._state = "started"

    def _claim_not_sent(self):
        if self._state != "unused":
            raise ValueError("Definite no-I/O evidence is unavailable")
        self._state = "not_sent"
        return self._nonce

    def _success_proof(self):
        if self._state not in {"started", "observed"}:
            raise ValueError("Source response evidence is unavailable")
        self._state = "observed"
        return self._nonce


_COMMON_FUNCTIONS = ("source_check_epoch(text)", "source_check_attempt(jsonb)",
    "source_seal_attempt(jsonb,text)", "source_status(uuid)", "source_settle_group(uuid)")
_BQ_RUNTIME_FUNCTIONS = _COMMON_FUNCTIONS + ("source_bq_lookup(jsonb,text)",
    "source_bq_admit(jsonb,text,jsonb)", "source_bq_check_data(jsonb,text)", "source_bq_observe(text,jsonb)")
_BQ_OBSERVER_FUNCTIONS = ("source_check_epoch(text)", "source_status(uuid)",
    "source_settle_group(uuid)", "source_bq_observe(text,jsonb)",
    "source_bq_reconcile_claim(integer)", "source_bq_check_observation(text,text)", "source_expire_groups(integer)")
_DOMO_FUNCTIONS = _COMMON_FUNCTIONS + ("source_domo_arm_send(jsonb,text,text,jsonb)",
    "source_domo_observe(uuid,text,jsonb)", "source_domo_not_sent(jsonb,text)")


def provision_sources(connection, descriptors, role_mappings, *, deployment_epoch, max_active_operations=10):
    """Add exact reviewed source profiles to a caller-owned owner transaction.

    Logins already exist. No credentials are accepted, read, generated or saved.
    Replays compare immutable values and do not clear source obligations.
    """
    connection.execute(text("SET LOCAL synchronous_commit=on"))
    connection.execute(text("SELECT gateway.source_require_durability()"))
    _text(deployment_epoch)
    if deployment_epoch == "unconfigured":
        raise ValueError("An externally pinned deployment epoch is required")
    _integer(max_active_operations, 1, 100)
    if not isinstance(descriptors, (list, tuple)) or not 1 <= len(descriptors) <= 50:
        raise ValueError("A bounded source catalog is required")
    if not isinstance(role_mappings, (list, tuple)) or not 1 <= len(role_mappings) <= 100:
        raise ValueError("Bounded explicit source role mappings are required")
    owner = connection.execute(text("SELECT current_user=pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='gateway'")).scalar_one()
    if owner is not True:
        raise ValueError("Source provisioning requires the separately provisioned registry owner")
    connection.execute(text("SELECT scope_id FROM gateway.capacity_locks WHERE scope_id='deployment' FOR UPDATE"))
    settings = connection.execute(text("SELECT deployment_epoch,app_role,legacy_roles,max_active_operations FROM gateway.source_runtime_settings WHERE singleton FOR UPDATE")).mappings().one()
    if settings["deployment_epoch"] == "unconfigured":
        if connection.execute(text("SELECT count(*) FROM gateway.source_operation_groups")).scalar_one():
            raise ValueError("Source recovery requires independent operator reconciliation")
        connection.execute(text("UPDATE gateway.source_runtime_settings SET deployment_epoch=:epoch,max_active_operations=:active WHERE singleton"),
            {"epoch": deployment_epoch, "active": max_active_operations})
    elif (settings["deployment_epoch"], settings["max_active_operations"]) != (deployment_epoch, max_active_operations):
        raise ValueError("Source deployment epoch or retained capacity conflicts")
    profiles = {}
    for supplied in descriptors:
        from gateway.adapters.bigquery_runtime import BigQuerySource
        if type(supplied) is BigQuerySource:
            source = BigQuerySource.model_validate(supplied.model_dump(mode="python"))
            physical = {"provider": "bigquery", "billing_project": source.billing_project, "location": source.location}
            active, dispatches = source.max_active_jobs, 8000
        else:
            from gateway.adapters.domo_runtime import DomoSource
            if type(supplied) is not DomoSource:
                raise ValueError("A validated supported source descriptor is required")
            source = DomoSource.model_validate(supplied.model_dump(mode="python"))
            physical = {"provider": "domo", "customer": source.auth.expected_customer}
            active, dispatches = source.max_active_sends, source.max_dispatches
        identity = (source.source_id, source.revision, source.context)
        if identity in profiles:
            raise ValueError("Duplicate source descriptor identity")
        profiles[identity] = source
        pool = {"key": source.capacity_pool_id, "provider": source.provider,
            "physical": json.dumps(physical), "active": active, "dispatches": dispatches}
        connection.execute(text("""INSERT INTO gateway.source_pools(capacity_key,provider,physical_identity,max_active,max_dispatches)
            VALUES(:key,:provider,CAST(:physical AS jsonb),:active,:dispatches) ON CONFLICT(capacity_key) DO NOTHING"""), pool)
        if not connection.execute(text("""SELECT provider=:provider AND physical_identity=CAST(:physical AS jsonb)
            AND max_active=:active AND max_dispatches=:dispatches FROM gateway.source_pools WHERE capacity_key=:key"""), pool).scalar_one():
            raise ValueError("The retained physical source pool conflicts")
        values = {"id": source.source_id, "revision": source.revision, "context": source.context,
            "provider": source.provider, "key": source.capacity_pool_id, "digest": source.digest,
            "descriptor": json.dumps(source.model_dump(mode="json"))}
        connection.execute(text("""INSERT INTO gateway.source_profiles(source_id,revision,context,provider,capacity_key,descriptor_digest,descriptor)
            VALUES(:id,:revision,:context,:provider,:key,:digest,CAST(:descriptor AS jsonb))
            ON CONFLICT(source_id,revision,context) DO NOTHING"""), values)
        if not connection.execute(text("""SELECT provider=:provider AND capacity_key=:key AND descriptor_digest=:digest
            AND descriptor=CAST(:descriptor AS jsonb) FROM gateway.source_profiles WHERE source_id=:id AND revision=:revision AND context=:context"""), values).scalar_one():
            raise ValueError("The retained source profile conflicts")
    names = set()
    prepared_mappings = []
    for mapping in role_mappings:
        if (type(mapping) is not dict or set(mapping) != {"login_name", "source_id", "revision", "context", "role_kind"}
                or type(mapping["login_name"]) is not str
                or re.fullmatch(r"[a-z][a-z0-9_]{0,62}", mapping["login_name"]) is None
                or mapping["role_kind"] not in {"runtime", "observer"}):
            raise ValueError("Invalid source runtime role mapping")
        identity = (mapping["source_id"], mapping["revision"], mapping["context"])
        if identity not in profiles or mapping["login_name"] in settings["legacy_roles"]:
            raise ValueError("Source roles require explicit independent profile authority")
        if profiles[identity].provider == "domo" and mapping["role_kind"] != "runtime":
            raise ValueError("Domo has no general observer authority")
        names.add(mapping["login_name"])
        prepared_mappings.append((dict(mapping), profiles[identity].provider))
    found = connection.execute(text("""SELECT rolname,rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls,rolinherit
        FROM pg_roles WHERE rolname=ANY(CAST(:names AS text[]))"""), {"names": sorted(names)}).all()
    if (len(found) != len(names) or any(not row[1] or any(row[2:]) for row in found)
            or connection.execute(text("""SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member
                WHERE r.rolname=ANY(CAST(:names AS text[]))"""), {"names": sorted(names)}).scalar_one()):
        raise ValueError("Source runtime logins must be preexisting non-elevated roles without memberships")
    role_kinds = {}
    for mapping, provider in prepared_mappings:
        # A login cannot accumulate consumer/fixture or runtime/observer authority.
        role_identity = (provider, None if mapping["role_kind"] == "observer" else mapping["context"], mapping["role_kind"])
        previous = role_kinds.setdefault(mapping["login_name"], role_identity)
        if previous != role_identity:
            raise ValueError("Source role contexts and capabilities must be distinct")
        retained = connection.execute(text("SELECT context,role_kind,p.provider FROM gateway.source_role_mappings m JOIN gateway.source_profiles p USING(source_id,revision,context) WHERE login_name=:login_name"), mapping).all()
        if any((row[2], None if row[1] == "observer" else row[0], row[1]) != role_identity for row in retained):
            raise ValueError("Source role authority conflicts with retained mappings")
        signatures = _DOMO_FUNCTIONS if provider == "domo" else (
            _BQ_OBSERVER_FUNCTIONS if mapping["role_kind"] == "observer" else _BQ_RUNTIME_FUNCTIONS)
        if connection.execute(text("SELECT has_schema_privilege(:login_name,'gateway','CREATE')"),mapping).scalar_one():
            raise ValueError("Source runtime logins cannot create registry objects")
        allowed_oids={connection.execute(text("SELECT CAST(to_regprocedure(:signature) AS oid)"),
            {"signature":"gateway."+signature}).scalar_one() for signature in signatures}
        actual_oids=set(connection.execute(text("""SELECT p.oid FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='gateway' AND has_function_privilege(:login_name,p.oid,'EXECUTE')"""),mapping).scalars())
        if None in allowed_oids or actual_oids-allowed_oids:
            raise ValueError("Source runtime logins have unreviewed registry function authority")
        if connection.execute(text("""SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='gateway' AND c.relkind IN ('r','v','m','p') AND
              has_table_privilege(:login_name,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')"""), mapping).scalar_one():
            raise ValueError("Source runtime roles cannot have direct registry table privileges")
        connection.execute(text("""INSERT INTO gateway.source_role_mappings(login_name,source_id,revision,context,role_kind)
            VALUES(:login_name,:source_id,:revision,:context,:role_kind) ON CONFLICT DO NOTHING"""), mapping)
        quoted = '"' + mapping["login_name"] + '"'
        connection.execute(text(f"GRANT USAGE ON SCHEMA gateway TO {quoted}"))
        signatures = _DOMO_FUNCTIONS if provider == "domo" else (
            _BQ_OBSERVER_FUNCTIONS if mapping["role_kind"] == "observer" else _BQ_RUNTIME_FUNCTIONS)
        for signature in signatures:
            connection.execute(text(f"GRANT EXECUTE ON FUNCTION gateway.{signature} TO {quoted}"))


@dataclass(frozen=True)
class SourceGroupContext:
    group_id: str
    deadline: datetime

    def __post_init__(self):
        _uuid(self.group_id)
        if type(self.deadline) is not datetime or self.deadline.utcoffset() != timedelta(0):
            raise ValueError("Invalid source group deadline")


class SourceLifecycle:
    """Trusted registry service; provider proofs use separately scoped logins.

    Every returned authority or send permit crosses a confirmed transaction exit.
    A failed/ambiguous commit returns no capability and is never retried here.
    """
    def __init__(self, registry, policy, sources, *, deployment_epoch, provider_registries=None):
        _text(deployment_epoch)
        if deployment_epoch == "unconfigured":
            raise ValueError("An external source epoch is required")
        self.registry, self.policy, self.sources = registry, policy, sources
        self.deployment_epoch = deployment_epoch
        self.provider_registries = dict(provider_registries or {})
        if set(self.provider_registries) - {("domo","consumer"),("domo","fixture_test")}:
            raise ValueError("Only separately scoped Domo transport registries are accepted")

    @staticmethod
    def required(binding):
        return getattr(binding, "adapter_revision", None) in {"cube-bigquery-v1", "domo-named-v2"}

    def source(self, binding):
        provider = "bigquery" if binding.adapter_revision == "cube-bigquery-v1" else "domo"
        return self.sources.resolve(dict(provider=provider,source_id=binding.source_id,
            revision=binding.source_revision,context=binding.context,descriptor_digest=binding.source_digest))

    async def _epoch(self, uow):
        await uow.session.execute(text("SET LOCAL synchronous_commit=on"))
        await uow.session.execute(text("SELECT gateway.source_check_epoch(:epoch)"), {"epoch": self.deployment_epoch})

    async def _lock_authority(self, uow, product, *, draft_id=None):
        from gateway.execution.queue import lock_execution_capacity
        await lock_execution_capacity(uow)
        await self._epoch(uow)
        await uow.lock_product(product.manifest.product_id)
        if draft_id is not None:
            await uow.lock_draft(draft_id)
        await uow.lock_policy(product.manifest.policy_id)

    async def _snapshot(self, uow, authority_spec):
        row = (await uow.session.execute(text("""WITH snapshot AS MATERIALIZED
            (SELECT gateway.source_current_authority(CAST(:spec AS jsonb)) value)
            SELECT value,encode(sha256(convert_to(value::text,'UTF8')),'hex') stamp FROM snapshot"""),
            {"spec":json.dumps(authority_spec)})).one()
        return row.value, row.stamp

    @staticmethod
    def _decision(snapshot, principal, action, product, *, fixture=False):
        from gateway.auth.principals import PrincipalGrants, capabilities_for_roles
        from gateway.contracts.canonical import canonical_digest
        from gateway.policy.service import PolicyContent, derive_decision
        from gateway.registry.repository import not_found
        identities = [p for p in snapshot["principals"] if p["id"] == principal.id]
        if (len(identities) != 1 or not identities[0]["active"] or identities[0]["kind"] != principal.kind
                or principal.kind == "service" and identities[0]["service_client_id"] != principal.service_client_id):
            raise not_found()
        policy = snapshot["policy"]
        if not policy or policy["id"] != product.manifest.policy_id or canonical_digest(policy["content"]) != policy["digest"]:
            raise not_found()
        if fixture:
            draft = snapshot["draft"]
            if (not draft or draft["id"] != product.version_id or draft["id"] != draft["head_id"]
                    or draft["revision"] != draft["head_revision"]):
                raise not_found()
        elif action == "execute":
            version = snapshot["version"]
            if (not version or version["id"] != product.version_id or version["status"] not in {"published","deprecated"}
                    or version["digest"] != product.manifest.content_digest):
                raise not_found()
        grants = PrincipalGrants(principal_id=principal.id,grants=[dict(policy_id=g["policy_id"],tenant_id=g["tenant_id"],
            capabilities=capabilities_for_roles(g["roles"]),entitlements=g["entitlements"])
            for g in snapshot["grants"] if g["principal_id"] == principal.id and g["active"]])
        return derive_decision(grants, action, product, PolicyContent.model_validate(policy["content"]).rules, policy["revision"])

    async def _audit(self, uow, principal, action, product, decision, correlation_id, parameters):
        from uuid import uuid4
        from gateway.registry.models import AuditEvent
        event = self.policy.audit.event(principal,action,product=product,policy_revision=decision.policy_revision,
            decision="allow",correlation_id=correlation_id,parameters=parameters)
        row = AuditEvent(id=str(uuid4()),**event.model_dump(mode="json"))
        uow.session.add(row)
        await uow.session.flush()
        return row.id

    @staticmethod
    def _authority_spec(principal_id, actor_id, product, *, fixture=False, catalog_digest=None):
        return dict(principal_id=principal_id,actor_id=actor_id,policy_id=product.manifest.policy_id,
            version_id=None if fixture else product.version_id,draft_id=product.version_id if fixture else None,
            catalog_digest=catalog_digest)

    async def _insert_group(self,uow,*,owner_id,actor,product,source,roster,deadline,authority_spec,snapshot,decision):
        from uuid import uuid4
        audit = await self._audit(uow,actor,"execute" if source.context=="consumer" else "author",product,decision,
            (await uow.session.execute(text("SELECT request_id FROM gateway.executions WHERE id=:id"),{"id":owner_id})).scalar_one()
                if source.context=="consumer" else owner_id,{})
        submissions = source.max_jobs_per_execution * len(roster) if source.provider=="bigquery" else len(roster)
        total_bytes = source.total_execution_bytes * len(roster) if source.provider=="bigquery" else None
        if submissions > 8 or total_bytes is not None and total_bytes > 2**53-1:
            raise ValueError("The fixed suite exceeds its whole-run source budget")
        values = dict(id=str(uuid4()),kind=source.context,owner=owner_id,actor=actor.id,source=source.source_id,
            revision=source.revision,context=source.context,pool=source.capacity_pool_id,epoch=self.deployment_epoch,
            deadline=deadline,roster=json.dumps(roster),spec=json.dumps(authority_spec),snapshot=json.dumps(snapshot),
            audit=audit,submissions=submissions,bytes=total_bytes)
        await uow.session.execute(text("""INSERT INTO gateway.source_operation_groups(id,owner_kind,owner_id,actor_id,
            source_id,source_revision,context,capacity_key,deployment_epoch,deadline_at,roster,authority_spec,authority_snapshot,
            allow_audit_id,max_submissions,max_total_bytes) VALUES(CAST(:id AS uuid),:kind,:owner,:actor,:source,:revision,:context,
            :pool,:epoch,:deadline,CAST(:roster AS jsonb),CAST(:spec AS jsonb),CAST(:snapshot AS jsonb),:audit,:submissions,:bytes)"""),values)
        return SourceGroupContext(values["id"],deadline.astimezone(timezone.utc))

    async def reserve_consumer(self,uow,loaded,current,deadline):
        """Queue-owned transaction, before returning a worker claim."""
        from gateway.contracts.canonical import canonical_digest
        execution,spec,principal,executed,binding = loaded
        source = self.source(binding)
        await self._lock_authority(uow,spec.pinned_product)
        authority_spec = self._authority_spec(principal.id,principal.id,spec.pinned_product)
        snapshot,_ = await self._snapshot(uow,authority_spec)
        decision = self._decision(snapshot,principal,"execute",spec.pinned_product)
        if canonical_digest(decision) != canonical_digest(current):
            raise ValueError("Source authority changed during admission")
        existing = (await uow.session.execute(text("SELECT * FROM gateway.source_operation_groups WHERE owner_kind='consumer' AND owner_id=:owner"),
            {"owner":execution.id})).mappings().one_or_none()
        if existing:
            if (existing["closed"] or existing["settled_at"] is not None or existing["authority_snapshot"] != snapshot
                    or existing["deployment_epoch"] != self.deployment_epoch):
                raise ValueError("The retained source execution is unavailable")
            return SourceGroupContext(str(existing["id"]),min(deadline,existing["deadline_at"]).astimezone(timezone.utc))
        return await self._insert_group(uow,owner_id=execution.id,actor=principal,product=spec.pinned_product,
            source=source,roster=[execution.id],deadline=deadline,authority_spec=authority_spec,snapshot=snapshot,decision=decision)

    def _validate_plan(self, spec, prepared, executed, binding, supplied, *, catalog_digest=None):
        from gateway.adapters.base import authorization_scope_digest
        from gateway.contracts.canonical import canonical_digest
        from gateway.contracts.execution import PreparedQuery
        from gateway.contracts.query import QueryRequest
        source = self.source(binding)
        supplied = SourceSessionSpec(**asdict(supplied))
        if source.provider == "bigquery":
            from gateway.adapters.bigquery_runtime import compile_bigquery_query
            prepared = PreparedQuery.model_validate(prepared.model_dump(mode="python"))
            if canonical_digest(prepared.cube_query) != canonical_digest(compile_bigquery_query(binding,spec.pinned_product.manifest,
                    QueryRequest.model_validate(spec.canonical_query),executed)):
                raise ValueError("The prepared source query differs from its accepted plan")
            prepared_digest = canonical_digest(prepared.cube_query)
        else:
            from gateway.adapters.domo_runtime import validate_domo_prepared
            prepared = validate_domo_prepared(prepared,spec,executed,binding)
            prepared_digest = prepared.digest
        expected = SourceSessionSpec(provider=source.provider,source_id=source.source_id,source_revision=source.revision,
            context=source.context,capacity_key=source.capacity_pool_id,definition_digest=source.digest,
            request_digest=spec.canonical_hash,prepared_digest=prepared_digest,scope_digest=authorization_scope_digest(executed),
            decision_digest=canonical_digest(executed),binding_digest=binding.digest,model_bundle_digest=binding.model_bundle_digest,
            catalog_digest=catalog_digest,max_submissions=source.max_jobs_per_execution if source.provider=="bigquery" else 1,
            max_bytes_per_submission=source.per_job_maximum_bytes_billed if source.provider=="bigquery" else None,
            max_total_bytes=source.total_execution_bytes if source.provider=="bigquery" else None)
        if (supplied != expected or spec.context != source.context
                or prepared.model_bundle_digest != binding.model_bundle_digest
                or source.provider == "bigquery" and prepared.model_bundle_digest != source.model_bundle_digest):
            raise ValueError("Source plan does not match its immutable owner")
        return expected

    @staticmethod
    def _context(row):
        return SourceAttemptContext(group_id=str(row["group_id"]),session_id=str(row["id"]),generation=row["generation"],
            authority_stamp=row["authority_stamp"],context_digest=row["context_digest"],deadline=row["deadline_at"].astimezone(timezone.utc),
            spec=SourceSessionSpec(**row["plan"]))

    async def _materialize(self,uow,*,group,owner_id,case_id,spec,principal,actor,binding,prepared,executed,current,plan,generation,token,deadline):
        import secrets
        from uuid import uuid4
        from gateway.contracts.canonical import canonical_digest
        fixture=spec.context=="fixture_test"
        authority_spec=self._authority_spec(principal.id,actor.id,spec.pinned_product,fixture=fixture,catalog_digest=plan.catalog_digest)
        snapshot,stamp=await self._snapshot(uow,authority_spec)
        decision=self._decision(snapshot,principal,"execute",spec.pinned_product,fixture=fixture)
        if canonical_digest(decision)!=canonical_digest(current):
            raise ValueError("The source decision differs from the coherent authority snapshot")
        if fixture:
            self._decision(snapshot,actor,"author",spec.pinned_product,fixture=True)
        existing=(await uow.session.execute(text("SELECT * FROM gateway.source_sessions WHERE context=:context AND owner_id=:owner"),
            {"context":spec.context,"owner":owner_id})).mappings().one_or_none()
        if existing:
            if (existing["plan"]!=asdict(plan) or existing["authority_snapshot"]!=snapshot or str(existing["group_id"])!=group.group_id):
                raise ValueError("The retained source preparation conflicts")
            if existing["generation"]!=generation:
                await uow.session.execute(text("SELECT gateway.source_reclaim_session(CAST(:id AS uuid),:generation,:token,:deadline)"),
                    {"id":str(existing["id"]),"generation":generation,"token":token,"deadline":min(existing["deadline_at"],deadline)})
                existing=(await uow.session.execute(text("SELECT * FROM gateway.source_sessions WHERE id=:id"),{"id":existing["id"]})).mappings().one()
            return self._context(existing)
        audit=await self._audit(uow,principal,"execute",spec.pinned_product,decision,spec.request_id,dict(spec.canonical_query["inputs"]))
        sid=str(uuid4()); deadline=min(deadline,group.deadline)
        digest=canonical_digest(dict(group_id=group.group_id,session_id=sid,plan=asdict(plan),authority_stamp=stamp))
        values=dict(id=sid,group=group.group_id,case=case_id,owner=owner_id,source=plan.source_id,revision=plan.source_revision,
            context=plan.context,pool=plan.capacity_key,plan=json.dumps(asdict(plan)),nonce=secrets.token_urlsafe(32),
            spec=json.dumps(authority_spec),snapshot=json.dumps(snapshot),stamp=stamp,digest=digest,audit=audit,
            generation=generation,token=token,deadline=deadline)
        row=(await uow.session.execute(text("""INSERT INTO gateway.source_sessions(id,group_id,case_id,owner_id,source_id,
            source_revision,context,capacity_key,plan,provider_nonce,authority_spec,authority_snapshot,authority_stamp,
            context_digest,allow_audit_id,generation,lease_token,deadline_at) VALUES(CAST(:id AS uuid),CAST(:group AS uuid),
            :case,:owner,:source,:revision,:context,:pool,CAST(:plan AS jsonb),:nonce,CAST(:spec AS jsonb),CAST(:snapshot AS jsonb),
            :stamp,:digest,:audit,:generation,:token,:deadline) RETURNING *"""),values)).mappings().one()
        if plan.provider=="domo":
            from gateway.adapters.source_preparations import save_source_prepared
            await save_source_prepared(uow,plan.context,owner_id,sid,prepared,binding)
        return self._context(row)

    async def consumer_attempt(self,queue,claim,prepared,current,session_spec):
        async with self.registry.transaction() as uow:
            await self._lock_authority(uow,claim.spec.pinned_product)
            loaded=await queue._load(uow,claim.execution_id)
            await queue._fence(uow,claim)
            plan=self._validate_plan(claim.spec,prepared,claim.executed_decision,claim.binding,session_spec)
            group=await self.reserve_consumer(uow,loaded,current,claim.deadline)
            result=await self._materialize(uow,group=group,owner_id=claim.execution_id,case_id=claim.execution_id,
                spec=claim.spec,principal=claim.principal,actor=claim.principal,binding=claim.binding,prepared=prepared,
                executed=claim.executed_decision,current=current,plan=plan,generation=claim.generation,token=claim.lease_token,deadline=claim.deadline)
        return result

    async def begin_fixture_run(self,authority,run_id):
        from sqlalchemy import func, select
        _uuid(run_id)
        binding=authority.catalog.fixture_binding
        source=self.source(binding)
        roster=[case.fixture.id for case in authority.catalog.cases if case.kind not in {"permission_negative","input_negative"}]
        if not roster or len(roster)>8:
            raise ValueError("A bounded fixed source-positive fixture roster is required")
        async with self.registry.transaction() as uow:
            await self._lock_authority(uow,authority.product,draft_id=authority.draft.id)
            spec=self._authority_spec(authority.actor.id,authority.actor.id,authority.product,fixture=True,catalog_digest=authority.catalog.digest)
            snapshot,_=await self._snapshot(uow,spec)
            decision=self._decision(snapshot,authority.actor,"author",authority.product,fixture=True)
            if decision.policy_revision!=authority.policy_revision or snapshot["draft"]["digest"]!=authority.draft.content_digest:
                raise ValueError("Fixture author authority changed")
            now=await uow.session.scalar(select(func.clock_timestamp()))
            deadline=now+timedelta(seconds=min(120,decision.limits.execution_timeout_seconds,authority.product.manifest.limits.execution_timeout_seconds))
            result=await self._insert_group(uow,owner_id=run_id,actor=authority.actor,product=authority.product,source=source,
                roster=roster,deadline=deadline,authority_spec=spec,snapshot=snapshot,decision=decision)
        return result

    async def check_fixture_group(self,uow,group,authority,case):
        await self._epoch(uow)
        row=(await uow.session.execute(text("SELECT *,clock_timestamp() now FROM gateway.source_operation_groups WHERE id=CAST(:id AS uuid)"),
            {"id":group.group_id})).mappings().one()
        snapshot,_=await self._snapshot(uow,row["authority_spec"])
        if (row["closed"] or row["deadline_at"]<=row["now"] or row["authority_snapshot"]!=snapshot
                or row["actor_id"]!=authority.actor.id or row["authority_spec"]["draft_id"]!=authority.draft.id
                or row["authority_spec"]["catalog_digest"]!=authority.catalog.digest or case.fixture.id not in row["roster"]):
            raise ValueError("Fixture source group unavailable")
        return group.deadline

    async def fixture_attempt(self,group,authority,case,attempt,prepared,current_decision,session_spec):
        from gateway.contracts.execution import ExecutionSpec,PinnedProduct
        from gateway.contracts.identity import Principal,Decision
        from gateway.contracts.manifest import validate_manifest
        spec=ExecutionSpec(principal_id=attempt.fixed_principal_id,pinned_product=PinnedProduct(
            manifest=validate_manifest(attempt.fixture_manifest),binding_digest=attempt.fixture_binding_digest,version_id=attempt.draft_id),
            canonical_query=attempt.canonical_query,canonical_hash=attempt.canonical_hash,policy_revision=attempt.policy_revision,
            context="fixture_test",request_id=attempt.request_id)
        principal=Principal.model_validate(attempt.principal_snapshot)
        executed=Decision.model_validate(attempt.decision)
        binding=authority.catalog.fixture_binding
        plan=self._validate_plan(spec,prepared,executed,binding,session_spec,catalog_digest=authority.catalog.digest)
        async with self.registry.transaction() as uow:
            await self._lock_authority(uow,authority.product,draft_id=authority.draft.id)
            await self.check_fixture_group(uow,group,authority,case)
            result=await self._materialize(uow,group=group,owner_id=attempt.id,case_id=case.fixture.id,spec=spec,
                principal=principal,actor=authority.actor,binding=binding,prepared=prepared,executed=executed,current=current_decision,
                plan=plan,generation=1,token=attempt.runner_token,deadline=attempt.deadline_at)
        return result

    def _provider_registry(self,context):
        if type(context) is not SourceAttemptContext:
            raise ValueError("A committed source context is required")
        context.wire_context()
        try:
            return self.provider_registries[(context.spec.provider,context.spec.context)]
        except KeyError:
            raise ValueError("The separately scoped provider registry is unavailable") from None

    async def _call(self,registry,function,args,values):
        async with registry.transaction() as uow:
            await self._epoch(uow)
            result=(await uow.session.execute(text(f"SELECT gateway.{function}({args})"),values)).scalar_one()
        return result

    async def check_attempt(self,context):
        return await self._call(self._provider_registry(context),"source_check_attempt","CAST(:context AS jsonb)",
            {"context":json.dumps(context.wire_context())})

    async def domo_arm_send(self,context,plan_fingerprint,intent_metadata):
        import secrets,hashlib
        _digest(plan_fingerprint)
        nonce=secrets.token_urlsafe(32)
        result=await self._call(self._provider_registry(context),"source_domo_arm_send","CAST(:context AS jsonb),:plan,:nonce,CAST(:intent AS jsonb)",
            {"context":json.dumps(context.wire_context()),"plan":plan_fingerprint,"nonce":hashlib.sha256(nonce.encode()).hexdigest(),"intent":json.dumps(intent_metadata)})
        if result!={"armed":True,"session_id":context.session_id,"plan_fingerprint":plan_fingerprint}:
            raise ValueError("The source send was not durably armed")
        return DomoSendPermit._from_committed(context,plan_fingerprint,nonce)

    async def domo_observe_success(self,permit,evidence):
        if type(permit) is not DomoSendPermit:
            raise ValueError("An exact source send permit is required")
        result=await self._call(self._provider_registry(permit.context),"source_domo_observe","CAST(:id AS uuid),:nonce,CAST(:evidence AS jsonb)",
            {"id":permit.context.session_id,"nonce":permit._success_proof(),"evidence":json.dumps(evidence)})
        return SourceStatus(**result)

    async def domo_observe_not_sent(self,permit):
        if type(permit) is not DomoSendPermit:
            raise ValueError("An exact source send permit is required")
        result=await self._call(self._provider_registry(permit.context),"source_domo_not_sent","CAST(:context AS jsonb),:nonce",
            {"context":json.dumps(permit.context.wire_context()),"nonce":permit._claim_not_sent()})
        return SourceStatus(**result)

    async def seal_source(self,context,reason):
        result=await self._call(self.registry,"source_seal_attempt","CAST(:context AS jsonb),:reason",
            {"context":json.dumps(context.wire_context()),"reason":reason})
        return SourceStatus(**result)

    async def source_status(self,session_id):
        _uuid(session_id)
        return SourceStatus(**await self._call(self.registry,"source_status","CAST(:id AS uuid)",{"id":session_id}))

    async def close_group(self,group_id,reason):
        _uuid(group_id)
        return await self._call(self.registry,"source_close_group","CAST(:id AS uuid),:reason",{"id":group_id,"reason":reason})
