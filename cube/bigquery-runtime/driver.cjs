"use strict";
const { performance } = require("node:perf_hooks");
const C = require("./contract.cjs");
const { PostgresLedger } = require("./postgres-ledger.cjs");
const DISABLED_METHODS = Object.freeze([
  "stream",
  "loadTablesForDataset",
  "tablesSchema",
  "getSchemas",
  "getTablesForSpecificSchemas",
  "getColumnsForSpecificTables",
  "getTablesQuery",
  "tableColumnTypes",
  "createSchemaIfNotExists",
  "loadPreAggregationIntoTable",
  "unload",
  "uploadTable",
  "uploadTableWithIndexes",
  "downloadQueryResults",
  "downloadTable",
  "queryColumnTypes",
  "createTable",
  "dropTable",
  "truncateTable",
  "indexesSql",
  "awaitForJobStatus",
  "waitForJobResult",
  "streamQuery",
  "tablesSchemaV2",
  "tableColumnTypesWithPrecision",
  "createTableRaw",
  "primaryKeys",
  "foreignKeys",
  "extractUnloadedFilesFromS3",
  "extractFilesFromGCS",
  "extractFilesFromAzure",
]);
function validateSource(s) {
  C.fields(s, [
    "projectId",
    "location",
    "principal",
    "inputTables",
    "jobTimeoutMs",
    "pollIntervalMs",
    "maxRows",
    "maxResultBytes",
    "maxMetadataBytes",
  ]);
  C.text(s.projectId, C.PROJECT);
  C.text(s.location, C.LOCATION);
  C.text(s.principal, C.PRINCIPAL);
  if (
    !Array.isArray(s.inputTables) ||
    !s.inputTables.length ||
    s.inputTables.length > 50 ||
    s.inputTables.some(
      (v) =>
        typeof v !== "string" ||
        !/^[-a-z0-9]+\.[A-Za-z_][A-Za-z0-9_]{0,127}\.[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(
          v,
        ),
    )
  )
    C.fail();
  C.integer(s.jobTimeoutMs, 1, 120000);
  C.integer(s.pollIntervalMs, 1, 5000);
  C.integer(s.maxRows, 1, 1000);
  C.integer(s.maxResultBytes, 1, 1048576);
  C.integer(s.maxMetadataBytes, 1024, 65536);
  return C.immutable(s);
}
function validateAttempt(a) {
  C.fields(a, [
    "provider",
    "source_id",
    "source_revision",
    "context",
    "capacity_key",
    "definition_digest",
    "request_digest",
    "prepared_digest",
    "scope_digest",
    "decision_digest",
    "binding_digest",
    "model_bundle_digest",
    "catalog_digest",
    "max_submissions",
    "max_bytes_per_submission",
    "max_total_bytes",
    "group_id",
    "session_id",
    "generation",
    "authority_stamp",
    "context_digest",
    "deadline_at",
  ]);
  if (
    a.provider !== "bigquery" ||
    !["consumer", "fixture_test"].includes(a.context)
  )
    C.fail();
  C.text(a.source_id, /^[^\x00-\x1f\x7f]{1,128}$/);
  C.integer(a.source_revision, 1, 2147483647);
  C.text(a.capacity_key, /^[A-Za-z0-9:_-]{1,512}$/);
  for (const k of [
    "definition_digest",
    "request_digest",
    "prepared_digest",
    "scope_digest",
    "decision_digest",
    "binding_digest",
    "model_bundle_digest",
    "authority_stamp",
    "context_digest",
  ])
    C.text(a[k], C.HEX);
  if (a.catalog_digest !== null) C.text(a.catalog_digest, C.HEX);
  C.text(a.group_id, C.UUID);
  C.text(a.session_id, C.UUID);
  C.integer(a.generation, 1, 2147483647);
  C.integer(a.max_submissions, 1, 8);
  C.integer(a.max_bytes_per_submission, 1, Number.MAX_SAFE_INTEGER);
  C.integer(
    a.max_total_bytes,
    a.max_bytes_per_submission,
    Number.MAX_SAFE_INTEGER,
  );
  C.text(
    a.deadline_at,
    /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)$/,
  );
  if (!Number.isFinite(Date.parse(a.deadline_at))) C.fail();
  return C.immutable(a);
}
function requestId(a) {
  return `${a.session_id}.g${a.generation}`;
}
function normalizedQuery(v) {
  C.fields(v, [
    "query",
    "params",
    "parameterMode",
    "useLegacySql",
    "wrapIntegers",
  ]);
  if (
    typeof v.query !== "string" ||
    Buffer.byteLength(v.query) > 16384 ||
    !/^\s*SELECT\b/i.test(v.query) ||
    v.query.includes(";") ||
    v.parameterMode !== "positional" ||
    v.useLegacySql !== false ||
    v.wrapIntegers !== true ||
    !Array.isArray(v.params) ||
    v.params.length > 100 ||
    v.params.some((p) => typeof p !== "string" || p.length > 128)
  )
    C.fail();
  return C.copy(v);
}
function firstJobTimeout(
  maximum,
  deadline,
  monotonic,
  wallNow = Date.now(),
  monotonicNow = performance.now(),
) {
  // Operational transit headroom for the first durable admission only. This is
  // not a guarantee on total DB latency: SQL still rejects an overlong intent.
  // Replays never recalculate this value or refresh the retained configuration.
  const remaining =
    Math.floor(Math.min(deadline - wallNow, monotonic - monotonicNow)) - 6000;
  C.integer(maximum, 1, 120000);
  C.integer(remaining, 1, 120000);
  return Math.min(maximum, remaining);
}
function submissionOptions(value, job) {
  const q = normalizedQuery(value);
  return {
    query: q.query,
    params: q.params,
    parameterMode: "positional",
    useLegacySql: false,
    jobId: job.job_id,
    location: job.location,
    maximumBytesBilled: job.intent.maximum_bytes_billed,
    jobTimeoutMs: job.intent.job_timeout_ms,
    useQueryCache: false,
  };
}
async function bounded(start, deadline, monotonic) {
  let timer;
  try {
    const left = Math.min(deadline - Date.now(), monotonic - performance.now());
    if (left <= 0) C.fail();
    return await Promise.race([
      start(),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(C.SAFE)), left);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}
function explicitAuth(principal) {
  C.text(principal, C.PRINCIPAL, 254);
  C.validateEnvironment();
  const { Compute } = require("google-auth-library");
  class PinnedCompute extends Compute {
    async getRequestHeaders(...args) {
      C.validateEnvironment();
      return super.getRequestHeaders(...args);
    }
    async refreshTokenNoCache(...args) {
      C.validateEnvironment();
      return super.refreshTokenNoCache(...args);
    }
  }
  return new PinnedCompute({
    serviceAccountEmail: principal,
    scopes: ["https://www.googleapis.com/auth/bigquery"],
  });
}
function createServingDriver({ source, context, ledger }) {
  const s = validateSource(source),
    a = validateAttempt(context);
  if (!(ledger instanceof PostgresLedger)) C.fail();
  C.validateEnvironment();
  if (
    require("@cubejs-backend/bigquery-driver/package.json").version !== "1.8.5"
  )
    C.fail();
  const { BigQueryDriver } = require("@cubejs-backend/bigquery-driver");
  class ServingBigQueryDriver extends BigQueryDriver {
    constructor() {
      super({
        projectId: s.projectId,
        location: s.location,
        authClient: explicitAuth(s.principal),
        keyFilename: undefined,
        credentials: undefined,
        scopes: ["https://www.googleapis.com/auth/bigquery"],
        exportBucket: undefined,
        readOnly: true,
        preAggregations: false,
        autoRetry: false,
        maxRetries: 0,
        pollTimeout: s.jobTimeoutMs / 1000,
        pollMaxInterval: s.pollIntervalMs / 1000,
        apiEndpoint: "https://bigquery.googleapis.com",
        timeout: 5000,
      });
    }
    readOnly() {
      return true;
    }
    async isUnloadSupported() {
      return false;
    }
    capabilities() {
      return {};
    }
    async testConnection() {
      /* Static construction only; readiness never discovers or queries source data. */
    }
    async runQueryJob(value, options, withResults = true) {
      let accepted = false;
      try {
        const q = normalizedQuery(value);
        if (
          withResults !== true ||
          !C.plain(options) ||
          options.requestId !== requestId(a)
        )
          C.fail();
        accepted = true;
        const current = await ledger.checkAttempt(a);
        if (
          current.allowed !== true ||
          current.context_digest !== a.context_digest
        )
          C.fail();
        let deadline = Math.min(
          Date.parse(a.deadline_at),
          Date.parse(current.deadline_at),
        );
        const originWall = Date.now(),
          originMono = performance.now();
        let monotonic = originMono + Math.min(120000, deadline - originWall);
        const tighten = (at) => {
          const date = Date.parse(at);
          if (!Number.isFinite(date)) C.fail();
          deadline = Math.min(deadline, date);
          monotonic = Math.min(monotonic, originMono + deadline - originWall);
          if (Date.now() >= deadline || performance.now() >= monotonic)
            C.fail();
        };
        tighten(current.deadline_at);
        const semantic = {
          query_digest: C.digest(q.query),
          parameters_digest: C.digest(q.params),
          parameter_types_digest: C.digest(q.params.map(() => "STRING")),
          definition_digest: a.definition_digest,
        };
        const key = C.digest(semantic);
        let job = await ledger.lookupJob(a, key),
          submit = false;
        if (job === null) {
          const intent = {
            query_digest: semantic.query_digest,
            parameters_digest: semantic.parameters_digest,
            parameter_types_digest: semantic.parameter_types_digest,
            maximum_bytes_billed: String(a.max_bytes_per_submission),
            job_timeout_ms: firstJobTimeout(
              s.jobTimeoutMs,
              deadline,
              monotonic,
            ),
          };
          C.integer(intent.job_timeout_ms, 1, 120000);
          intent.configuration_digest = C.configurationDigest({
            project_id: s.projectId,
            location: s.location,
            principal: s.principal,
            intent,
          });
          const admission = await ledger.admitJob(a, key, intent);
          if (typeof admission?.submit_once !== "boolean") C.fail();
          job = admission.job;
          submit = admission.submit_once;
        }
        const validateJob = (j) => {
          if (
            !C.plain(j) ||
            j.session_id !== a.session_id ||
            j.group_id !== a.group_id ||
            j.semantic_key !== key ||
            j.project_id !== s.projectId ||
            j.location !== s.location ||
            j.principal !== s.principal ||
            j.intent?.query_digest !== semantic.query_digest ||
            j.intent?.parameters_digest !== semantic.parameters_digest ||
            j.intent?.parameter_types_digest !==
              semantic.parameter_types_digest ||
            j.intent?.maximum_bytes_billed !==
              String(a.max_bytes_per_submission) ||
            C.configurationDigest(j) !== j.intent.configuration_digest
          )
            C.fail();
          C.text(j.job_id, /^sp_[a-f0-9]{64}$/);
          C.text(j.intent_digest, C.HEX);
          C.integer(j.intent.job_timeout_ms, 1, 120000);
          return j;
        };
        validateJob(job);
        const check = async () => {
          C.validateEnvironment();
          tighten(a.deadline_at);
          const checked = validateJob(await ledger.checkDataRpc(a, job.job_id));
          if (checked.intent_digest !== job.intent_digest) C.fail();
          const now = await ledger.checkAttempt(a);
          if (now.allowed !== true || now.context_digest !== a.context_digest)
            C.fail();
          tighten(now.deadline_at);
        };
        // Only the invocation that received an acknowledged new-intent COMMIT owns
        // this in-memory one-use submission right. Unknown insert outcomes are lookup-only.
        if (submit) {
          await check();
          try {
            await bounded(
              () => this.bigquery.createQueryJob(submissionOptions(q, job)),
              deadline,
              monotonic,
            );
          } catch (_) {
            /* Retain intent; lookup exact ID below. */
          }
        }
        const handle = this.bigquery.job(job.job_id, {
          location: job.location,
        });
        while (true) {
          await check();
          const [metadata] = await bounded(
            () => handle.getMetadata(),
            deadline,
            monotonic,
          );
          const observation = C.validateMetadata(metadata, job, s);
          // Matched late terminal metadata is useful even after data authority expires.
          await ledger.observeJob(job.job_id, observation);
          await check();
          if (observation.state === "done") {
            if (observation.terminal_error) C.fail();
            await check();
            const rows = await bounded(
              () =>
                handle.getQueryResults({
                  wrapIntegers: true,
                  autoPaginate: false,
                  maxResults: s.maxRows + 1,
                }),
              deadline,
              monotonic,
            );
            await check();
            return C.validateRows(rows, s);
          }
          await bounded(
            () =>
              new Promise((resolve) => setTimeout(resolve, s.pollIntervalMs)),
            deadline,
            monotonic,
          );
        }
      } catch (_) {
        if (accepted) {
          try {
            await ledger.sealAttempt(a, "runtime_error");
          } catch {}
        }
        C.fail();
      }
    }
  }
  for (const method of DISABLED_METHODS)
    Object.defineProperty(ServingBigQueryDriver.prototype, method, {
      value: () => C.fail(),
    });
  return new ServingBigQueryDriver();
}
module.exports = {
  createServingDriver,
  validateSource,
  validateAttempt,
  requestId,
  normalizedQuery,
  submissionOptions,
  firstJobTimeout,
  explicitAuth,
  bounded,
  DISABLED_METHODS,
};
