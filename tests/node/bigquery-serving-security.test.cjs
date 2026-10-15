"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const C = require("../../cube/bigquery-runtime/contract.cjs");
const P = "../../cube/bigquery-runtime/security.cjs";
const contract = {
  schema_version: 1,
  adapter_revision: "cube-bigquery-v1",
  data_cube: "Usage",
  members: {
    "Usage.day": { kind: "time", type: "time" },
    "Usage.account": { kind: "dimension", type: "string" },
    "Usage.events": { kind: "measure", type: "number" },
    "Usage.tenant": { kind: "dimension", type: "string" },
    "Usage.group": { kind: "dimension", type: "string" },
  },
  tenant_member: "Usage.tenant",
  access_group_member: "Usage.group",
  time_member: "Usage.day",
  checkpoint_member: null,
};
function fixture() {
  const now = Math.floor(Date.now() / 1000);
  const source_attempt = {
    provider: "bigquery",
    source_id: "fixture-source",
    source_revision: 1,
    context: "consumer",
    capacity_key: "bigquery-pool",
    definition_digest: "a".repeat(64),
    request_digest: "b".repeat(64),
    prepared_digest: "c".repeat(64),
    scope_digest:
      "b2c387d4a7ea1f5b34e1f52d2bd5c997b07fc59432dde08c05be75b0d40463cd",
    decision_digest: "e".repeat(64),
    binding_digest: "f".repeat(64),
    model_bundle_digest: "1".repeat(64),
    catalog_digest: null,
    max_submissions: 2,
    max_bytes_per_submission: 1048576,
    max_total_bytes: 2097152,
    group_id: "a1e393af-394c-4de5-8932-a7d1dfb967c9",
    session_id: "f22bdd8e-2f70-4e11-8987-ed9bdd4e176c",
    generation: 1,
    authority_stamp: "2".repeat(64),
    context_digest: "3".repeat(64),
    deadline_at: new Date(Date.now() + 60000).toISOString(),
  };
  const query = {
    measures: ["Usage.events"],
    dimensions: ["Usage.account"],
    timeDimensions: [{ dimension: "Usage.day", granularity: "day" }],
    filters: [
      {
        member: "Usage.day",
        operator: "afterOrOnDate",
        values: ["2026-10-01"],
      },
      { member: "Usage.day", operator: "beforeDate", values: ["2026-10-08"] },
    ],
    limit: 1000,
    timezone: "UTC",
  };
  source_attempt.prepared_digest = C.digest(query);
  const value = {
    iss: "dpg-gateway",
    aud: "dpg-cube-source-v1",
    iat: now,
    exp: now + 60,
    principal_id: "person",
    tenant_id: "tenant",
    policy_revision: 1,
    model_bundle_digest: "1".repeat(64),
    context: "consumer",
    row_constraints: [
      { member: "Usage.tenant", operator: "eq", values: ["tenant"] },
      { member: "Usage.group", operator: "in", values: ["public"] },
    ],
    adapter_revision: "cube-bigquery-v1",
    contract_digest: C.digest(contract),
    source_attempt,
    cube_query_digest: C.digest(query),
  };
  const options = {
    now,
    bundleDigest: "1".repeat(64),
    endpointContext: "consumer",
    contract,
    sourceId: "fixture-source",
    sourceRevision: 1,
    definitionDigest: "a".repeat(64),
    capacityKey: "bigquery-pool",
  };
  return { value, query, options };
}
test("new JWT schema validates source context and rewrites retained independent scope", () => {
  assert.equal(
    require("node:fs").existsSync(require("node:path").resolve(__dirname, P)),
    true,
  );
  const S = require(P);
  const { value, query, options } = fixture();
  assert.deepEqual(S.validateContext(value, options), value);
  S.validateRawQuery(query, value, options);
  const rewritten = S.rewrite(query, value, options);
  assert.deepEqual(rewritten.filters[0].and.slice(-2), [
    { member: "Usage.tenant", operator: "equals", values: ["tenant"] },
    { member: "Usage.group", operator: "equals", values: ["public"] },
  ]);
  assert.equal(rewritten.dimensions.includes("Usage.loaded_at"), false);
});
test("raw-body digest and every retained source identity fail closed on mismatch", () => {
  const S = require(P);
  const { value, query, options } = fixture();
  assert.throws(() =>
    S.validateRawQuery({ ...query, limit: 999 }, value, options),
  );
  for (const mutate of [
    (v) => (v.aud = "dpg-cube"),
    (v) => (v.adapter_revision = "cube-rest-v2"),
    (v) => (v.source_attempt.provider = "domo"),
    (v) => (v.source_attempt.definition_digest = "9".repeat(64)),
    (v) => (v.source_attempt.context = "fixture_test"),
    (v) => (v.source_attempt.model_bundle_digest = "9".repeat(64)),
    (v) => (v.source_attempt.generation = 0),
    (v) => (v.extra = true),
  ]) {
    const v = structuredClone(value);
    mutate(v);
    assert.throws(() => S.validateContext(v, options));
  }
});
test("queue and cache identities partition generations while compiler key only uses static immutable identities", () => {
  const S = require(P);
  const { value, options } = fixture();
  const next = structuredClone(value);
  next.source_attempt.generation++;
  assert.notEqual(
    S.orchestratorId(value, options),
    S.orchestratorId(next, options),
  );
  assert.equal(S.compilerId(value, options), S.compilerId(next, options));
  const other = structuredClone(value);
  other.source_attempt.session_id = "82bcb027-9dbf-43ad-902e-1bf846612959";
  assert.notEqual(
    S.orchestratorId(value, options),
    S.orchestratorId(other, options),
  );
});
test("HTTP request ID rejects repeated/comma-joined headers and exact mismatched generations", () => {
  const S = require(P);
  const { value } = fixture();
  const id = value.source_attempt.session_id + ".g1";
  const request = {
    headers: { "x-request-id": id },
    rawHeaders: ["X-Request-Id", id],
  };
  assert.equal(S.validateRequestId(request, value), id);
  for (const req of [
    { headers: { "x-request-id": id } },
    { ...request, rawHeaders: ["x-request-id", id, "X-Request-ID", id] },
    { ...request, headers: { "x-request-id": id + ",x" } },
    { ...request, headers: { "x-request-id": id + "-span-x" } },
    { ...request, headers: { "x-request-id": id.replace(".g1", ".g2") } },
  ])
    assert.throws(() => S.validateRequestId(req, value));
});
module.exports = { fixture, contract };
test("valid signed outer scope cannot be swapped onto another retained source authority", () => {
  const S = require(P);
  const { value, options } = fixture();
  for (const mutate of [
    (v) => (v.principal_id = "another-valid-user"),
    (v) => (v.policy_revision = 2),
    (v) => {
      v.tenant_id = "another-tenant";
      v.row_constraints[0].values = ["another-tenant"];
    },
    (v) => (v.row_constraints[1].values = ["another-entitlement"]),
  ]) {
    const changed = structuredClone(value);
    mutate(changed);
    assert.throws(() => S.validateContext(changed, options));
  }
});
test("newly signed body fingerprint cannot replace the DB-retained BigQuery preparation", () => {
  const S = require(P);
  const { value, query, options } = fixture();
  const changed = { ...query, limit: 999 };
  const signed = { ...value, cube_query_digest: C.digest(changed) };
  assert.throws(() => S.validateRawQuery(changed, signed, options));
});
test("unknown freshness supports time plus measure with only the two hidden scope members", () => {
  const S = require(P),
    minimal = structuredClone(contract);
  delete minimal.members["Usage.account"];
  assert.deepEqual(S.validateContract(minimal), minimal);
  const { value, query, options } = fixture();
  const q = { ...query, dimensions: [] };
  value.contract_digest = C.digest(minimal);
  value.cube_query_digest = C.digest(q);
  value.source_attempt.prepared_digest = C.digest(q);
  assert.deepEqual(
    S.rewrite(q, value, { ...options, contract: minimal }).dimensions,
    [],
  );
});
