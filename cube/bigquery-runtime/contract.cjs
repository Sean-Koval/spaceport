"use strict";
// Serving v1 contracts. The legacy offline POSIX controls remain independent.
const crypto = require("node:crypto");
const SAFE = "BigQuery source is unavailable.";
const HEX = /^[a-f0-9]{64}$/;
const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[a-f0-9]{12}$/;
const PROJECT = /^[a-z][a-z0-9-]{4,28}[a-z0-9]$/;
const LOCATION = /^(?:US|EU|[a-z]+(?:-[a-z]+)+[1-9][0-9]?)$/;
const PRINCIPAL =
  /^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com$/;
function fail() {
  throw new Error(SAFE);
}
function plain(v) {
  return (
    v !== null &&
    typeof v === "object" &&
    Object.getPrototypeOf(v) === Object.prototype
  );
}
function fields(v, names, required = true) {
  if (
    !plain(v) ||
    Object.keys(v).some((k) => !names.includes(k)) ||
    (required && names.some((k) => !Object.hasOwn(v, k)))
  )
    fail();
}
function text(v, pattern, max = 512) {
  if (typeof v !== "string" || v.length > max || !pattern.test(v)) fail();
  return v;
}
function integer(v, low, high) {
  if (!Number.isSafeInteger(v) || v < low || v > high) fail();
  return v;
}
function bytes(v) {
  text(v, /^[1-9][0-9]{0,15}$/, 16);
  if (BigInt(v) > 9007199254740991n) fail();
  return v;
}
function canonical(v) {
  const walk = (x, d) => {
    if (d > 24) fail();
    if (x === null || typeof x === "string" || typeof x === "boolean") return x;
    if (typeof x === "number" && Number.isSafeInteger(x)) return x;
    if (Array.isArray(x)) return x.map((i) => walk(i, d + 1));
    if (!plain(x)) fail();
    return Object.fromEntries(
      Object.keys(x)
        .sort()
        .map((k) => [k, walk(x[k], d + 1)]),
    );
  };
  return JSON.stringify(walk(v, 0));
}
const digest = (v) =>
  crypto.createHash("sha256").update(canonical(v)).digest("hex");
const copy = (v) => JSON.parse(canonical(v));
function immutable(value) {
  const freeze = (x) => {
    if (x !== null && typeof x === "object") {
      Object.values(x).forEach(freeze);
      Object.freeze(x);
    }
    return x;
  };
  return freeze(copy(value));
}
function configurationDigest(job) {
  const i = job.intent;
  return digest({
    project_id: job.project_id,
    location: job.location,
    principal: job.principal,
    query_digest: i.query_digest,
    parameters_digest: i.parameters_digest,
    parameter_types_digest: i.parameter_types_digest,
    maximum_bytes_billed: i.maximum_bytes_billed,
    job_timeout_ms: i.job_timeout_ms,
    submitted_destination: false,
    submitted_create_disposition: false,
    submitted_write_disposition: false,
  });
}
function validateMetadata(metadata, job, source) {
  try {
    if (
      !plain(metadata) ||
      Buffer.byteLength(JSON.stringify(metadata)) >
        integer(source.maxMetadataBytes, 1024, 65536)
    )
      fail();
    const ref = metadata.jobReference,
      c = metadata.configuration,
      q = c?.query,
      i = job.intent;
    if (
      !plain(ref) ||
      !plain(c) ||
      !plain(q) ||
      !plain(i) ||
      ref.jobId !== job.job_id ||
      ref.projectId !== job.project_id ||
      ref.location !== job.location ||
      metadata.user_email !== job.principal
    )
      fail();
    text(job.job_id, /^sp_[a-f0-9]{64}$/);
    text(job.project_id, PROJECT);
    text(job.location, LOCATION);
    text(job.principal, PRINCIPAL);
    text(job.intent_digest, HEX);
    if (configurationDigest(job) !== i.configuration_digest) fail();
    fields(c, ["query", "jobTimeoutMs", "jobType", "dryRun", "labels"], false);
    fields(
      q,
      [
        "query",
        "queryParameters",
        "parameterMode",
        "useLegacySql",
        "useQueryCache",
        "maximumBytesBilled",
        "priority",
        "createSession",
        "allowLargeResults",
        "flattenResults",
        "destinationTable",
        "createDisposition",
        "writeDisposition",
      ],
      false,
    );
    if (
      (c.jobType !== undefined && c.jobType !== "QUERY") ||
      (c.dryRun !== undefined && c.dryRun !== false) ||
      (c.labels !== undefined &&
        (!plain(c.labels) || Object.keys(c.labels).length))
    )
      fail();
    if (
      (q.priority !== undefined && q.priority !== "INTERACTIVE") ||
      (q.createSession !== undefined && q.createSession !== false) ||
      ["allowLargeResults", "flattenResults"].some(
        (k) => q[k] !== undefined && typeof q[k] !== "boolean",
      )
    )
      fail();
    if (
      q.useLegacySql !== false ||
      q.useQueryCache !== false ||
      q.parameterMode !== "POSITIONAL" ||
      c.jobTimeoutMs !== String(i.job_timeout_ms) ||
      q.maximumBytesBilled !== i.maximum_bytes_billed ||
      typeof q.query !== "string" ||
      Buffer.byteLength(q.query) > 16384 ||
      digest(q.query) !== i.query_digest
    )
      fail();
    const params = q.queryParameters || [];
    if (!Array.isArray(params) || params.length > 100) fail();
    const values = params.map((p) => {
      fields(p, ["parameterType", "parameterValue"]);
      fields(p.parameterType, ["type"]);
      fields(p.parameterValue, ["value"]);
      if (
        p.parameterType.type !== "STRING" ||
        typeof p.parameterValue.value !== "string" ||
        p.parameterValue.value.length > 128
      )
        fail();
      return p.parameterValue.value;
    });
    if (
      digest(values) !== i.parameters_digest ||
      digest(params.map((p) => p.parameterType.type)) !==
        i.parameter_types_digest
    )
      fail();
    if (q.destinationTable !== undefined) {
      fields(q.destinationTable, ["projectId", "datasetId", "tableId"]);
      const d = q.destinationTable;
      if (d.projectId !== job.project_id) fail();
      text(d.datasetId, /^_[A-Za-z0-9_]{1,127}$/);
      text(d.tableId, /^[A-Za-z0-9_]{1,128}$/);
      if (
        !Array.isArray(source.inputTables) ||
        source.inputTables.includes(
          `${d.projectId}.${d.datasetId}.${d.tableId}`,
        )
      )
        fail();
      if (
        (q.createDisposition !== undefined &&
          q.createDisposition !== "CREATE_IF_NEEDED") ||
        (q.writeDisposition !== undefined &&
          q.writeDisposition !== "WRITE_EMPTY")
      )
        fail();
    } else if (
      q.createDisposition !== undefined ||
      q.writeDisposition !== undefined
    )
      fail();
    if (
      !plain(metadata.status) ||
      !["PENDING", "RUNNING", "DONE"].includes(metadata.status.state)
    )
      fail();
    const stats = metadata.statistics;
    if (
      (stats !== undefined && !plain(stats)) ||
      (stats &&
        ["parentJobId", "numChildJobs", "scriptStatistics", "sessionInfo"].some(
          (k) => Object.hasOwn(stats, k),
        ))
    )
      fail();
    if (
      stats?.query &&
      (!plain(stats.query) ||
        ["scriptStatistics", "sessionInfo", "statementType"].some(
          (k) => k !== "statementType" && Object.hasOwn(stats.query, k),
        ))
    )
      fail();
    const done = metadata.status.state === "DONE",
      error = Object.hasOwn(metadata.status, "errorResult");
    if (error && !plain(metadata.status.errorResult)) fail();
    if (done && !error && stats?.query?.statementType !== "SELECT") fail();
    if (!done && error) fail();
    return {
      intent_digest: job.intent_digest,
      state: done ? "done" : metadata.status.state.toLowerCase(),
      terminal_error: done ? error : null,
    };
  } catch (_) {
    fail();
  }
}
function validateRows(result, source) {
  try {
    if (
      !Array.isArray(result) ||
      !Array.isArray(result[0]) ||
      result[0].length > source.maxRows ||
      result[1] ||
      result[2]?.pageToken
    )
      fail();
    let size = 2;
    for (const row of result[0]) {
      if (!plain(row) || Object.keys(row).length > 52) fail();
      for (const [key, value] of Object.entries(row)) {
        if (key.length > 132) fail();
        let wire = value;
        if (value !== null && typeof value === "object") {
          const name = value.constructor?.name;
          if (
            ["BigQueryDate", "BigQueryTimestamp"].includes(name) &&
            typeof value.value === "string"
          )
            wire = value.value;
          else if (name === "Big" && typeof value.toString === "function")
            wire = value.toString();
          else fail();
        }
        if (wire !== null && !["string", "boolean"].includes(typeof wire))
          fail();
        size +=
          Buffer.byteLength(JSON.stringify(key)) +
          Buffer.byteLength(JSON.stringify(wire)) +
          2;
        if (size > source.maxResultBytes) fail();
      }
      size += 2;
      if (size > source.maxResultBytes) fail();
    }
    return result;
  } catch (_) {
    fail();
  }
}
function validateEnvironment(env = process.env) {
  // Presence (including an empty value) is rejected. Never silently prefer one
  // ambient credential/transport configuration over another.
  for (const key of Object.keys(env)) {
    const k = key.toUpperCase();
    if (
      /^(HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|GOOGLE_APPLICATION_CREDENTIALS|GOOGLE_AUTH_TOKEN|GCE_METADATA_HOST|GCE_METADATA_IP|GCE_METADATA_ROOT|BIGQUERY_EMULATOR_HOST|DEBUG|GOOGLE_SDK_NODE_LOGGING|NODE_TLS_REJECT_UNAUTHORIZED|NODE_EXTRA_CA_CERTS|SSL_CERT_FILE|SSL_CERT_DIR)$/.test(
        k,
      ) ||
      /^GOOGLE_/.test(k) ||
      /(?:UNIVERSE|TOKEN_URI|AUTH_ENDPOINT|API_ENDPOINT)/.test(k) ||
      /^CUBEJS_(?:DS_[A-Z0-9_]+_)?(?:DB_BQ_|DB_EXPORT_|BIGQUERY_)/.test(k)
    )
      fail();
  }
}
module.exports = {
  SAFE,
  HEX,
  UUID,
  PROJECT,
  LOCATION,
  PRINCIPAL,
  fail,
  plain,
  fields,
  text,
  integer,
  bytes,
  canonical,
  digest,
  copy,
  immutable,
  configurationDigest,
  validateMetadata,
  validateRows,
  validateEnvironment,
};
