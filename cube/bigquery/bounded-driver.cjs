"use strict";
// Executable offline candidate, deliberately not selected by any gateway/runtime factory.
const C = require("./runtime-contract.cjs");
const { performance } = require("node:perf_hooks");
const { AdmissionLedger } = require("./admission-ledger.cjs");
const SAFE = "BigQuery source job is unavailable.";
const disabled = () => {
  throw new Error("BigQuery source method is disabled.");
};
function validateSource(value) {
  C.exact(value, [
    "projectId",
    "datasetId",
    "location",
    "keyFilename",
    "jobTimeoutMs",
    "maxRows",
    "maxResultBytes",
    "maxMetadataBytes",
    "pollIntervalMs",
  ]);
  C.jobConfig({
    projectId: value.projectId,
    location: value.location,
    queryHash: "0".repeat(64),
    parametersHash: "0".repeat(64),
    maximumBytesBilled: "1",
    jobTimeoutMs: value.jobTimeoutMs,
  });
  C.text(value.datasetId, /^[A-Za-z_][A-Za-z0-9_]{0,127}$/);
  C.text(
    value.keyFilename,
    /^\/run\/secrets\/[A-Za-z][A-Za-z0-9_-]{0,127}$/,
    160,
  );
  C.integer(value.maxRows, 1, 1000);
  C.integer(value.maxResultBytes, 1, 1048576);
  C.integer(value.maxMetadataBytes, 1024, 65536);
  C.integer(value.pollIntervalMs, 1, 5000);
  return Object.freeze(C.copy(value));
}
function normalizedQuery(value) {
  C.exact(value, [
    "query",
    "params",
    "parameterMode",
    "useLegacySql",
    "wrapIntegers",
  ]);
  // Defense in depth over retained reviewed Cube SQL, never an arbitrary SQL authorizer.
  if (
    typeof value.query !== "string" ||
    Buffer.byteLength(value.query) > 16384 ||
    !/^\s*SELECT\b/i.test(value.query) ||
    value.query.includes(";") ||
    value.parameterMode !== "positional" ||
    value.useLegacySql !== false ||
    value.wrapIntegers !== true ||
    !Array.isArray(value.params) ||
    value.params.length > 100 ||
    value.params.some((p) => typeof p !== "string" || p.length > 128)
  )
    C.fail();
  return C.copy(value);
}
async function bounded(start, deadlineMs, monotonicDeadline) {
  let timer;
  try {
    const remaining = Math.min(
      deadlineMs - Date.now(),
      monotonicDeadline - performance.now(),
    );
    if (remaining <= 0) throw new Error(SAFE);
    return await Promise.race([
      start(),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(SAFE)), remaining);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}
function validateMetadata(metadata, job, maxBytes) {
  if (
    !C.plain(metadata) ||
    Buffer.byteLength(JSON.stringify(metadata)) > maxBytes
  )
    C.fail();
  const ref = metadata.jobReference,
    config = metadata.configuration,
    q = config?.query;
  if (
    !C.plain(ref) ||
    !C.plain(config) ||
    !C.plain(q) ||
    ref.jobId !== job.jobId ||
    ref.projectId !== job.config.projectId ||
    ref.location !== job.config.location ||
    q.useLegacySql !== false ||
    q.useQueryCache !== false ||
    q.parameterMode !== "POSITIONAL" ||
    typeof q.query !== "string" ||
    config.jobTimeoutMs !== String(job.config.jobTimeoutMs) ||
    q.maximumBytesBilled !== job.config.maximumBytesBilled ||
    config.load ||
    config.extract ||
    config.copy ||
    q.createSession ||
    q.connectionProperties ||
    q.userDefinedFunctionResources ||
    q.tableDefinitions
  )
    C.fail();
  const allowedConfig = new Set(["query", "jobTimeoutMs", "jobType"]);
  const allowedQuery = new Set([
    "query",
    "useLegacySql",
    "useQueryCache",
    "parameterMode",
    "queryParameters",
    "maximumBytesBilled",
  ]);
  if (
    Object.keys(config).some((k) => !allowedConfig.has(k)) ||
    Object.keys(q).some((k) => !allowedQuery.has(k)) ||
    (config.jobType !== undefined && config.jobType !== "QUERY")
  )
    C.fail();
  // Provider-generated anonymous destination metadata is NOT yet reviewed. Reject it too.
  const params = q.queryParameters || [];
  if (!Array.isArray(params) || params.length > 100) C.fail();
  const values = params.map((p) => {
    C.exact(p, ["parameterType", "parameterValue"]);
    C.exact(p.parameterType, ["type"]);
    C.exact(p.parameterValue, ["value"]);
    if (
      p.parameterType.type !== "STRING" ||
      typeof p.parameterValue.value !== "string" ||
      p.parameterValue.value.length > 128
    )
      C.fail();
    return p.parameterValue.value;
  });
  const observed = {
    projectId: ref.projectId,
    location: ref.location,
    queryHash: C.digest(q.query),
    parametersHash: C.digest(values),
    maximumBytesBilled: q.maximumBytesBilled,
    jobTimeoutMs: Number(config.jobTimeoutMs),
  };
  if (
    C.digest(observed) !== job.configDigest ||
    !C.plain(metadata.status) ||
    !["PENDING", "RUNNING", "DONE"].includes(metadata.status.state)
  )
    C.fail();
  return metadata.status;
}
function validateRows(result, source) {
  if (
    !Array.isArray(result) ||
    !Array.isArray(result[0]) ||
    result[0].length > source.maxRows + 1 ||
    result[1] ||
    result[2]?.pageToken
  )
    C.fail();
  let size = 2;
  for (const row of result[0]) {
    if (!C.plain(row) || Object.keys(row).length > 52) C.fail();
    for (const [key, value] of Object.entries(row)) {
      if (key.length > 132) C.fail();
      let wire = value;
      // The inherited pinned driver's hydration runs after this raw result admission.
      if (value !== null && typeof value === "object") {
        if (typeof value.value === "string" && Object.keys(value).length <= 3)
          wire = value.value;
        else if (
          value.constructor?.name === "Big" &&
          typeof value.toString === "function"
        )
          wire = value.toString();
        else C.fail();
      }
      if (
        wire !== null &&
        typeof wire !== "string" &&
        typeof wire !== "boolean"
      )
        C.fail();
      size +=
        Buffer.byteLength(JSON.stringify(key)) +
        Buffer.byteLength(JSON.stringify(wire)) +
        2;
      if (size > source.maxResultBytes) C.fail();
    }
    size += 2;
    if (size > source.maxResultBytes) C.fail();
  }
  return result;
}
function boundedDriverClass(UpstreamDriver) {
  if (
    typeof UpstreamDriver !== "function" ||
    typeof UpstreamDriver.prototype.query !== "function"
  )
    C.fail();
  class BoundedBigQueryDriver extends UpstreamDriver {
    #source;
    #ledger;
    #scopeDigest;
    #sourceDigest;
    constructor({ source, ledger, scopeDigest }) {
      const checked = validateSource(source);
      C.text(scopeDigest, C.HEX);
      if (!(ledger instanceof AdmissionLedger)) C.fail();
      super({
        projectId: checked.projectId,
        location: checked.location,
        keyFilename: checked.keyFilename,
        credentials: undefined,
        exportBucket: undefined,
        scopes: ["https://www.googleapis.com/auth/bigquery"],
        readOnly: true,
        preAggregations: false,
        autoRetry: false,
        pollTimeout: checked.jobTimeoutMs / 1000,
        pollMaxInterval: checked.pollIntervalMs / 1000,
      });
      this.#source = checked;
      this.#ledger = ledger;
      this.#scopeDigest = scopeDigest;
      this.#sourceDigest = C.digest({
        projectId: checked.projectId,
        datasetId: checked.datasetId,
        location: checked.location,
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
    async runQueryJob(value, options, withResults = true) {
      let job,
        handle,
        executionId,
        generation,
        verified = false;
      let deadline = 0,
        monotonicDeadline = 0,
        originWall = 0,
        originMonotonic = 0;
      const tighten = (absolute) => {
        deadline = Math.min(deadline, absolute);
        monotonicDeadline = Math.min(
          monotonicDeadline,
          originMonotonic + deadline - originWall,
        );
      };
      const expired = () =>
        Date.now() >= deadline || performance.now() >= monotonicDeadline;
      try {
        const q = normalizedQuery(value);
        if (
          withResults !== true ||
          !C.plain(options) ||
          typeof options.requestId !== "string"
        )
          C.fail();
        const match = /^([a-f0-9-]{36})\.g([1-9][0-9]{0,9})$/.exec(
          options.requestId,
        );
        if (!match) C.fail();
        executionId = match[1];
        generation = Number(match[2]);
        const attempt = this.#ledger.attempt(executionId, generation);
        if (
          attempt.scopeDigest !== this.#scopeDigest ||
          attempt.sourceDigest !== this.#sourceDigest
        )
          C.fail();
        deadline = attempt.deadlineMs;
        const now = Date.now();
        originWall = now;
        originMonotonic = performance.now();
        monotonicDeadline = originMonotonic + Math.min(120000, deadline - now);
        if (now >= deadline) C.fail();
        const semantic = {
          queryHash: C.digest(q.query),
          parametersHash: C.digest(q.params),
          sourceDigest: this.#sourceDigest,
        };
        const key = C.digest(semantic);
        const firstConfig = {
          projectId: this.#source.projectId,
          location: this.#source.location,
          queryHash: semantic.queryHash,
          parametersHash: semantic.parametersHash,
          maximumBytesBilled: attempt.perJobBytes,
          jobTimeoutMs: Math.min(this.#source.jobTimeoutMs, deadline - now),
        };
        const admitted = this.#ledger.admit(
          executionId,
          generation,
          key,
          firstConfig,
          now,
        );
        job = admitted.job;
        // Admission commits before creating a promise that could touch the provider.
        if (expired()) throw new Error(SAFE);
        if (admitted.submit) {
          try {
            [handle] = await bounded(
              () =>
                this.bigquery.createQueryJob({
                  ...q,
                  jobId: job.jobId,
                  location: job.config.location,
                  maximumBytesBilled: job.config.maximumBytesBilled,
                  jobTimeoutMs: job.config.jobTimeoutMs,
                  useQueryCache: false,
                }),
              deadline,
              monotonicDeadline,
            );
          } catch (_) {
            handle = this.bigquery.job(job.jobId, {
              location: job.config.location,
            });
          }
        } else
          handle = this.bigquery.job(job.jobId, {
            location: job.config.location,
          });
        while (true) {
          const current = this.#ledger.attempt(executionId, generation); // Fence every subsequent local read/poll.
          tighten(current.deadlineMs);
          if (expired()) throw new Error(SAFE);
          const [metadata] = await bounded(
            () => handle.getMetadata(),
            deadline,
            monotonicDeadline,
          );
          const status = validateMetadata(
            metadata,
            job,
            this.#source.maxMetadataBytes,
          );
          verified = true;
          const afterMetadata = this.#ledger.attempt(executionId, generation);
          tighten(afterMetadata.deadlineMs);
          if (expired()) throw new Error(SAFE);
          if (status.state === "DONE") {
            this.#ledger.observe(executionId, key, "done");
            if (status.errorResult) throw new Error(SAFE);
            const result = await bounded(
              () =>
                handle.getQueryResults({
                  wrapIntegers: true,
                  autoPaginate: false,
                  maxResults: this.#source.maxRows + 1,
                }),
              deadline,
              monotonicDeadline,
            );
            const latest = this.#ledger.attempt(executionId, generation);
            tighten(latest.deadlineMs);
            if (expired()) throw new Error(SAFE);
            return validateRows(result, this.#source);
          }
          this.#ledger.observe(executionId, key, "running");
          await bounded(
            () =>
              new Promise((resolve) =>
                setTimeout(resolve, this.#source.pollIntervalMs),
              ),
            deadline,
            monotonicDeadline,
          );
        }
      } catch (_) {
        if (job && handle && verified && expired()) {
          try {
            this.#ledger.attempt(executionId, generation);
            this.#ledger.observe(executionId, job.key, "cancel_requested");
            await bounded(
              () => handle.cancel(),
              Date.now() + 1000,
              performance.now() + 1000,
            );
          } catch {}
        }
        throw new Error(SAFE);
      }
    }
  }
  // Audit every entry point inherited from the exact pinned release. No general discovery or writes.
  for (const name of [
    "stream",
    "loadTablesForDataset",
    "tablesSchema",
    "getSchemas",
    "getTablesForSpecificSchemas",
    "getColumnsForSpecificTables",
    "getTablesQuery",
    "tableColumnTypes",
    "testConnection",
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
  ])
    Object.defineProperty(BoundedBigQueryDriver.prototype, name, {
      value: disabled,
    });
  return BoundedBigQueryDriver;
}
function loadPinnedDriverClass() {
  try {
    if (
      require("@cubejs-backend/bigquery-driver/package.json").version !==
      "1.8.5"
    )
      C.fail();
    const { BigQueryDriver } = require("@cubejs-backend/bigquery-driver");
    if (typeof BigQueryDriver.prototype.runQueryJob !== "function") C.fail();
    return boundedDriverClass(BigQueryDriver);
  } catch (_) {
    throw new Error("Pinned BigQuery driver is unavailable.");
  }
}
module.exports = { boundedDriverClass, loadPinnedDriverClass };
