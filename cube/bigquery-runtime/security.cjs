"use strict";
// Independent serving-v1 enforcement; retained static contract is authority.
const C = require("./contract.cjs");
const { validateAttempt, requestId } = require("./driver.cjs");
const crypto = require("node:crypto");
const identifier = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const cubeMember = /^[A-Za-z][A-Za-z0-9_]*\.[A-Za-z][A-Za-z0-9_]*$/;
const digest = /^[a-f0-9]{64}$/;
const contextKeys = new Set([
  "iss",
  "aud",
  "iat",
  "exp",
  "principal_id",
  "tenant_id",
  "policy_revision",
  "model_bundle_digest",
  "context",
  "row_constraints",
  "adapter_revision",
  "contract_digest",
  "source_attempt",
  "cube_query_digest",
]);
const queryKeys = new Set([
  "measures",
  "dimensions",
  "timeDimensions",
  "filters",
  "order",
  "limit",
  "offset",
  "timezone",
  "ungrouped",
  "renewQuery",
  "cache",
  "cacheMode",
  "maskedMembers",
]);
const operators = new Set([
  "equals",
  "notEquals",
  "gt",
  "gte",
  "lt",
  "lte",
  "set",
  "notSet",
  "afterOrOnDate",
  "beforeDate",
]);
function plain(v) {
  return (
    v !== null &&
    typeof v === "object" &&
    Object.getPrototypeOf(v) === Object.prototype
  );
}
function fail() {
  throw new Error("Invalid reviewed gateway request");
}
function fields(v, names, required = false) {
  if (
    !plain(v) ||
    Object.keys(v).some((k) => !names.has(k)) ||
    (required && Object.keys(v).length !== names.size)
  )
    fail();
}
function canonical(v) {
  if (v === null || typeof v !== "object") return JSON.stringify(v);
  if (Array.isArray(v)) return "[" + v.map(canonical).join(",") + "]";
  return (
    "{" +
    Object.keys(v)
      .sort()
      .map((k) => JSON.stringify(k) + ":" + canonical(v[k]))
      .join(",") +
    "}"
  );
}
function validateContract(c) {
  fields(
    c,
    new Set([
      "schema_version",
      "adapter_revision",
      "data_cube",
      "members",
      "tenant_member",
      "access_group_member",
      "time_member",
      "checkpoint_member",
    ]),
    true,
  );
  if (
    c.schema_version !== 1 ||
    c.adapter_revision !== "cube-bigquery-v1" ||
    typeof c.data_cube !== "string" ||
    !/^[A-Za-z][A-Za-z0-9_]*$/.test(c.data_cube) ||
    !plain(c.members)
  )
    fail();
  const entries = Object.entries(c.members);
  if (entries.length < 4 || entries.length > 22) fail();
  for (const [name, item] of entries) {
    fields(item, new Set(["kind", "type"]), true);
    if (
      name.length > 128 ||
      !cubeMember.test(name) ||
      !name.startsWith(c.data_cube + ".") ||
      !["measure", "dimension", "time"].includes(item.kind) ||
      (item.kind === "measure" && item.type !== "number") ||
      (item.kind === "time" && item.type !== "time") ||
      (item.kind === "dimension" && !["string", "time"].includes(item.type))
    )
      fail();
  }
  const hidden = [
    c.tenant_member,
    c.access_group_member,
    c.time_member,
    ...(c.checkpoint_member === null ? [] : [c.checkpoint_member]),
  ];
  if (
    new Set(hidden).size !== hidden.length ||
    hidden.some((n) => typeof n !== "string" || !Object.hasOwn(c.members, n))
  )
    fail();
  for (const n of [c.tenant_member, c.access_group_member])
    if (c.members[n].kind !== "dimension" || c.members[n].type !== "string")
      fail();
  if (
    (c.checkpoint_member !== null &&
      (c.members[c.checkpoint_member]?.kind !== "dimension" ||
        c.members[c.checkpoint_member]?.type !== "string")) ||
    c.members[c.time_member].kind !== "time" ||
    entries.filter(([, i]) => i.kind === "time").length !== 1 ||
    entries.some(
      ([n, i]) =>
        i.kind === "dimension" &&
        i.type === "time" &&
        n !== c.checkpoint_member,
    ) ||
    !entries.some(([, i]) => i.kind === "measure")
  )
    fail();
  return c;
}
function contractDigest(c) {
  return crypto
    .createHash("sha256")
    .update(canonical(validateContract(c)))
    .digest("hex");
}
function authorizationScopeDigest(value) {
  const constraints = value.row_constraints.map((item) => ({
    member: item.member,
    values: [...new Set(item.values)].sort((a, b) =>
      Buffer.compare(Buffer.from(C.canonical(a)), Buffer.from(C.canonical(b))),
    ),
  }));
  constraints.sort((a, b) =>
    a.member < b.member ? -1 : a.member > b.member ? 1 : 0,
  );
  return C.digest({
    principal_id: value.principal_id,
    policy_revision: value.policy_revision,
    tenant_id: value.tenant_id,
    row_constraints: constraints,
  });
}
function validateContext(value, options) {
  const c = validateContract(options.contract);
  fields(value, contextKeys, true);
  if (
    value.iss !== "dpg-gateway" ||
    value.aud !== "dpg-cube-source-v1" ||
    typeof value.principal_id !== "string" ||
    !identifier.test(value.principal_id) ||
    typeof value.tenant_id !== "string" ||
    !identifier.test(value.tenant_id) ||
    !Number.isSafeInteger(value.policy_revision) ||
    value.policy_revision < 1 ||
    !Number.isSafeInteger(value.iat) ||
    !Number.isSafeInteger(value.exp) ||
    value.iat > options.now ||
    value.exp <= options.now ||
    value.exp <= value.iat ||
    value.exp - value.iat > 60 ||
    options.now - value.iat > 60 ||
    !digest.test(value.model_bundle_digest || "") ||
    value.model_bundle_digest !== options.bundleDigest ||
    !["consumer", "fixture_test"].includes(options.endpointContext) ||
    value.context !== options.endpointContext ||
    value.adapter_revision !== "cube-bigquery-v1" ||
    value.contract_digest !== contractDigest(c) ||
    !Array.isArray(value.row_constraints) ||
    value.row_constraints.length !== 2
  )
    fail();
  const seen = new Set();
  for (const item of value.row_constraints) {
    fields(item, new Set(["member", "operator", "values"]), true);
    if (
      ![c.tenant_member, c.access_group_member].includes(item.member) ||
      seen.has(item.member) ||
      !["eq", "in"].includes(item.operator) ||
      !Array.isArray(item.values) ||
      item.values.length < 1 ||
      item.values.length > 100 ||
      (item.operator === "eq" && item.values.length !== 1) ||
      new Set(item.values).size !== item.values.length ||
      item.values.some((v) => typeof v !== "string" || !identifier.test(v))
    )
      fail();
    seen.add(item.member);
  }
  const tenant = value.row_constraints.find(
    (i) => i.member === c.tenant_member,
  );
  if (
    !tenant ||
    tenant.operator !== "eq" ||
    tenant.values[0] !== value.tenant_id
  )
    fail();
  const a = validateAttempt(value.source_attempt);
  if (
    a.scope_digest !== authorizationScopeDigest(value) ||
    a.prepared_digest !== value.cube_query_digest
  )
    fail();
  if (
    a.context !== value.context ||
    a.model_bundle_digest !== value.model_bundle_digest ||
    a.source_id !== options.sourceId ||
    a.source_revision !== options.sourceRevision ||
    a.definition_digest !== options.definitionDigest ||
    a.capacity_key !== options.capacityKey ||
    !digest.test(value.cube_query_digest || "")
  )
    fail();
  return value;
}
function scopeId(context, options) {
  validateContext(context, options);
  const constraints = context.row_constraints
    .map((i) => ({ member: i.member, values: [...i.values].sort() }))
    .sort((a, b) => a.member.localeCompare(b.member));
  return (
    "dpg_bq_v1_" +
    crypto
      .createHash("sha256")
      .update(
        canonical([
          context.model_bundle_digest,
          context.context,
          context.contract_digest,
          context.principal_id,
          context.tenant_id,
          context.policy_revision,
          constraints,
          context.source_attempt,
        ]),
      )
      .digest("hex")
  );
}
function rewrite(query, context, options) {
  validateContext(context, options);
  const c = options.contract;
  fields(query, queryKeys);
  const hiddenScope = new Set([c.tenant_member, c.access_group_member]);
  function member(n, kind) {
    if (
      typeof n !== "string" ||
      !Object.hasOwn(c.members, n) ||
      (kind && c.members[n].kind !== kind)
    )
      fail();
  }
  for (const key of ["measures", "dimensions"]) {
    if (
      !Array.isArray(query[key]) ||
      query[key].length > 20 ||
      new Set(query[key]).size !== query[key].length
    )
      fail();
    query[key].forEach((n) => {
      member(n, key === "measures" ? "measure" : "dimension");
      if (hiddenScope.has(n)) fail();
    });
  }
  const required = Object.entries(c.members)
    .filter(([n, i]) => i.kind === "dimension" && !hiddenScope.has(n))
    .map(([n]) => n);
  if (
    query.dimensions.length !== required.length ||
    required.some((n) => !query.dimensions.includes(n))
  )
    fail();
  if (!Array.isArray(query.timeDimensions) || query.timeDimensions.length !== 1)
    fail();
  const td = query.timeDimensions[0];
  fields(td, new Set(["dimension", "granularity", "dateRange"]));
  if (
    td.dimension !== c.time_member ||
    td.granularity !== "day" ||
    td.dateRange !== undefined
  )
    fail();
  const order =
    query.order === undefined
      ? []
      : Array.isArray(query.order)
        ? query.order
        : plain(query.order)
          ? Object.entries(query.order)
          : null;
  if (!order || order.length > 5) fail();
  const sorted = new Set();
  for (const pair of order) {
    if (
      !Array.isArray(pair) ||
      pair.length !== 2 ||
      !["asc", "desc"].includes(pair[1]) ||
      sorted.has(pair[0])
    )
      fail();
    member(pair[0]);
    if (hiddenScope.has(pair[0]) || pair[0] === c.checkpoint_member) fail();
    sorted.add(pair[0]);
  }
  let count = 0;
  function filter(value, depth = 0) {
    if (!plain(value) || depth > 5 || ++count > 100) fail();
    if ("and" in value || "or" in value) {
      const keys = Object.keys(value);
      if (
        keys.length !== 1 ||
        !["and", "or"].includes(keys[0]) ||
        !Array.isArray(value[keys[0]]) ||
        !value[keys[0]].length
      )
        fail();
      value[keys[0]].forEach((i) => filter(i, depth + 1));
    } else {
      fields(value, new Set(["member", "operator", "values"]));
      member(value.member);
      if (
        c.members[value.member].kind === "measure" ||
        value.member === c.checkpoint_member ||
        !operators.has(value.operator)
      )
        fail();
      if (["set", "notSet"].includes(value.operator)) {
        if (value.values !== undefined) fail();
      } else if (
        !Array.isArray(value.values) ||
        !value.values.length ||
        value.values.length > 100 ||
        value.values.some((i) => typeof i !== "string" || i.length > 128)
      )
        fail();
    }
  }
  if (
    query.filters !== undefined &&
    (!Array.isArray(query.filters) || query.filters.length > 100)
  )
    fail();
  (query.filters || []).forEach((i) => filter(i));
  if (
    query.cache !== undefined ||
    (query.cacheMode !== undefined &&
      !["must-revalidate", "no-cache"].includes(query.cacheMode)) ||
    query.maskedMembers !== undefined ||
    (query.limit !== undefined &&
      (!Number.isSafeInteger(query.limit) ||
        query.limit < 1 ||
        query.limit > 1001)) ||
    (query.offset !== undefined && query.offset !== 0) ||
    (query.timezone !== undefined &&
      !["UTC", "America/New_York"].includes(query.timezone)) ||
    (query.ungrouped !== undefined && query.ungrouped !== false) ||
    (query.renewQuery !== undefined && typeof query.renewQuery !== "boolean")
  )
    fail();
  const mandatory = context.row_constraints.map((i) => ({
    member: i.member,
    operator: "equals",
    values: [...i.values],
  }));
  return {
    ...query,
    limit: query.limit || 1001,
    filters: [{ and: [...(query.filters || []), ...mandatory] }],
  };
}
function validateRawQuery(query, context, options) {
  validateContext(context, options);
  if (C.digest(query) !== context.cube_query_digest) fail();
  rewrite(query, context, options);
}
function validateRequestId(req, context) {
  const expected = requestId(context.source_attempt);
  if (!Array.isArray(req.rawHeaders) || req.rawHeaders.length % 2 !== 0) fail();
  const headers = [];
  for (let i = 0; i < req.rawHeaders.length; i += 2)
    if (req.rawHeaders[i].toLowerCase() === "x-request-id")
      headers.push(req.rawHeaders[i + 1]);
  if (
    headers.length !== 1 ||
    headers[0] !== expected ||
    req.headers?.["x-request-id"] !== expected
  )
    fail();
  return expected;
}
function compilerId(context, options) {
  validateContext(context, options);
  return (
    "dpg_bq_compiler_" +
    C.digest([
      context.model_bundle_digest,
      context.source_attempt.definition_digest,
    ])
  );
}
module.exports = {
  authorizationScopeDigest,
  validateContract,
  contractDigest,
  validateContext,
  validateRawQuery,
  validateRequestId,
  orchestratorId: scopeId,
  compilerId,
  rewrite,
};
