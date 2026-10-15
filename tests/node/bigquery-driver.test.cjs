"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {
  AdmissionLedger,
  initializeLedger,
} = require("../../cube/bigquery/admission-ledger.cjs");
const {
  boundedDriverClass,
} = require("../../cube/bigquery/bounded-driver.cjs");
const C = require("../../cube/bigquery/runtime-contract.cjs");
const identity = {
  deploymentId: "fixture-deployment",
  ledgerId: "8e2f0371-447a-4c0b-b5a3-7769efeff389",
};
const source = {
  projectId: "fixture-project",
  datasetId: "fixture_dataset",
  location: "US",
  keyFilename: "/run/secrets/bq_fixture",
  jobTimeoutMs: 25000,
  maxRows: 1000,
  maxResultBytes: 1048576,
  maxMetadataBytes: 65536,
  pollIntervalMs: 1,
};
const permit = (overrides = {}) => ({
  executionId: "0dd0b6bb-6541-41be-80e9-ecb69a41ee6f",
  generation: 1,
  scopeDigest: "a".repeat(64),
  queryDigest: "b".repeat(64),
  bindingDigest: "c".repeat(64),
  bundleDigest: "d".repeat(64),
  sourceDigest: C.digest({
    projectId: source.projectId,
    datasetId: source.datasetId,
    location: source.location,
  }),
  deadlineMs: Date.now() + 60000,
  maxJobs: 3,
  perJobBytes: "100",
  totalBytes: "300",
  ...overrides,
});
const query = (overrides = {}) => ({
  query: "SELECT ? AS tenant",
  params: ["north"],
  parameterMode: "positional",
  useLegacySql: false,
  wrapIntegers: true,
  ...overrides,
});
function fixture(
  t,
  {
    submitFailure = false,
    metadataChange = () => {},
    rows = [{ tenant: "north" }],
    nextQuery = null,
    status = "DONE",
    deadlineMs,
    sourceChanges = {},
  } = {},
) {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "bq-driver-"));
  fs.chmodSync(parent, 0o700);
  const directory = path.join(parent, "ledger");
  initializeLedger(directory, {
    ...identity,
    limits: {
      maxJournalBytes: 262144,
      maxRecordBytes: 8192,
      maxExecutions: 8,
      maxJobsPerExecution: 8,
    },
  });
  const ledger = new AdmissionLedger(directory, identity);
  const p = permit(deadlineMs ? { deadlineMs } : {});
  ledger.register(p, Date.now());
  const jobs = new Map(),
    calls = [];
  let cancels = 0;
  const sdk = {
    async createQueryJob(request) {
      calls.push(C.copy(request));
      const metadata = {
        jobReference: {
          projectId: source.projectId,
          location: source.location,
          jobId: request.jobId,
        },
        configuration: {
          jobTimeoutMs: String(request.jobTimeoutMs),
          query: {
            query: request.query,
            useLegacySql: false,
            parameterMode: "POSITIONAL",
            queryParameters: request.params.map((value) => ({
              parameterType: { type: "STRING" },
              parameterValue: { value },
            })),
            maximumBytesBilled: request.maximumBytesBilled,
            useQueryCache: false,
          },
        },
        status: { state: status },
      };
      metadataChange(metadata);
      const job = {
        getMetadata: async () => [metadata],
        getQueryResults: async () => [rows, nextQuery, {}],
        cancel: async () => {
          cancels++;
          return [{}];
        },
      };
      jobs.set(request.jobId, job);
      if (submitFailure) throw new Error("PRIVATE PROVIDER ERROR secret");
      return [job];
    },
    job(id, options) {
      assert.equal(options.location, "US");
      return (
        jobs.get(id) || {
          getMetadata: async () => {
            throw new Error("404 PRIVATE");
          },
          cancel: async () => {
            cancels++;
          },
        }
      );
    },
  };
  class FakeUpstream {
    constructor(options) {
      this.bigquery = sdk;
      this.options = options;
    }
    async query(sql, params, options) {
      const value = await this.runQueryJob(
        query({ query: sql, params }),
        options,
      );
      return value[0];
    }
  }
  const Driver = boundedDriverClass(FakeUpstream);
  const driver = new Driver({
    source: { ...source, ...sourceChanges },
    ledger,
    scopeDigest: p.scopeDigest,
  });
  t.after(() => {
    try {
      ledger.close();
    } catch {}
    fs.rmSync(parent, { recursive: true, force: true });
  });
  return {
    driver,
    ledger,
    p,
    calls,
    jobs,
    directory,
    sdk,
    get cancels() {
      return cancels;
    },
    request: { requestId: p.executionId + ".g1" },
  };
}
test("actual subclass path enforces provider request fields and registers terminal job", async (t) => {
  const f = fixture(t);
  assert.deepEqual(
    await f.driver.query("SELECT ? AS tenant", ["north"], f.request),
    [{ tenant: "north" }],
  );
  assert.equal(f.calls.length, 1);
  const r = f.calls[0];
  assert.equal(r.maximumBytesBilled, "100");
  assert.equal(r.jobTimeoutMs, 25000);
  assert.equal(r.location, "US");
  assert.equal(r.useLegacySql, false);
  assert.equal(r.useQueryCache, false);
  assert.match(r.jobId, /^sp_[a-f0-9]{64}$/);
  assert.deepEqual(f.ledger.seal(f.p.executionId, 1), {
    sealed: true,
    allJobsDone: true,
    jobs: 1,
  });
});
test("identical polls reuse first config/job even when time decreases", async (t) => {
  const f = fixture(t);
  await f.driver.runQueryJob(query(), f.request);
  const first = f.calls[0];
  await f.driver.runQueryJob(query(), f.request);
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].jobTimeoutMs, first.jobTimeoutMs);
});
test("ambiguous submission reconciles one exact job without replacement", async (t) => {
  const f = fixture(t, { submitFailure: true });
  await f.driver.runQueryJob(query(), f.request);
  await f.driver.runQueryJob(query(), f.request);
  assert.equal(f.calls.length, 1);
});
for (const changed of [
  "jobId",
  "projectId",
  "location",
  "query",
  "params",
  "bytes",
  "timeout",
  "mode",
  "cache",
])
  test(`reconciliation rejects changed ${changed}`, async (t) => {
    const f = fixture(t, {
      submitFailure: true,
      metadataChange: (m) => {
        if (["jobId", "projectId", "location"].includes(changed))
          m.jobReference[changed] = "wrong";
        if (changed === "query") m.configuration.query.query = "SELECT PRIVATE";
        if (changed === "params")
          m.configuration.query.queryParameters[0].parameterValue.value =
            "south";
        if (changed === "bytes")
          m.configuration.query.maximumBytesBilled = "101";
        if (changed === "timeout") m.configuration.jobTimeoutMs = "999";
        if (changed === "mode") m.configuration.query.useLegacySql = true;
        if (changed === "cache") m.configuration.query.useQueryCache = true;
      },
    });
    await assert.rejects(
      f.driver.runQueryJob(query(), f.request),
      /unavailable/,
    );
    assert.equal(f.calls.length, 1);
    assert.equal(f.ledger.seal(f.p.executionId, 1).allJobsDone, false);
  });
test("missing job after ambiguous submit remains unknown across new driver and never resubmits", async (t) => {
  const f = fixture(t);
  f.sdk.createQueryJob = async () => {
    f.calls.push("attempt");
    throw new Error("PRIVATE");
  };
  await assert.rejects(f.driver.runQueryJob(query(), f.request), /unavailable/);
  await assert.rejects(f.driver.runQueryJob(query(), f.request), /unavailable/);
  assert.equal(f.calls.length, 1);
  assert.equal(f.ledger.seal(f.p.executionId, 1).allJobsDone, false);
});
test("main, checkpoint/refresh, and other auxiliary queries share total budget", async (t) => {
  const f = fixture(t);
  await f.driver.runQueryJob(query(), f.request);
  await f.driver.runQueryJob(
    query({ query: "SELECT MAX(loaded_at) FROM checked_view", params: [] }),
    f.request,
  );
  await f.driver.runQueryJob(
    query({ query: "SELECT 3", params: [] }),
    f.request,
  );
  await assert.rejects(
    f.driver.runQueryJob(query({ query: "SELECT 4", params: [] }), f.request),
    /unavailable/,
  );
  assert.equal(f.calls.length, 3);
  assert.equal(f.ledger.status(f.p.executionId).reservedBytes, "300");
});
test("simultaneous identical calls only submit once", async (t) => {
  const f = fixture(t);
  await Promise.all([
    f.driver.runQueryJob(query(), f.request),
    f.driver.runQueryJob(query(), f.request),
  ]);
  assert.equal(f.calls.length, 1);
});
test("all unsupported SDK escape routes fail before any provider work", async (t) => {
  const f = fixture(t);
  for (const method of [
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
  ]) {
    await assert.rejects(
      Promise.resolve().then(() => f.driver[method]("private")),
      /disabled/,
    );
  }
  assert.equal(f.calls.length, 0);
  assert.equal(f.driver.readOnly(), true);
  assert.equal(await f.driver.isUnloadSupported(), false);
});
test("missing attempt or wrong scope/unsafe query options make zero calls", async (t) => {
  const f = fixture(t);
  for (const options of [
    undefined,
    {},
    { requestId: "forged" },
    { requestId: f.p.executionId + ".g2" },
  ])
    await assert.rejects(f.driver.runQueryJob(query(), options), /unavailable/);
  for (const q of [
    query({ destination: "table" }),
    query({ useLegacySql: true }),
    query({ query: "DELETE FROM table" }),
    query({ query: "SELECT 1; DELETE FROM table" }),
    query({ params: [7] }),
    query({ params: ["x".repeat(129)] }),
  ])
    await assert.rejects(f.driver.runQueryJob(q, f.request), /unavailable/);
  assert.equal(f.calls.length, 0);
});
test("deadline cancellation acknowledgement leaves ledger nonterminal", async (t) => {
  const f = fixture(t, { status: "RUNNING", deadlineMs: Date.now() + 50 });
  await assert.rejects(f.driver.runQueryJob(query(), f.request), /unavailable/);
  assert.equal(f.calls.length, 1);
  assert.equal(f.cancels, 1);
  assert.equal(f.ledger.seal(f.p.executionId, 1).allJobsDone, false);
});
test("provider error DONE is terminal but result fails with redacted error", async (t) => {
  const f = fixture(t, {
    metadataChange: (m) => {
      m.status.errorResult = { message: "SECRET SQL" };
    },
  });
  await assert.rejects(
    f.driver.runQueryJob(query(), f.request),
    (e) => e.message === "BigQuery source job is unavailable.",
  );
  assert.equal(f.ledger.seal(f.p.executionId, 1).allJobsDone, true);
});
for (const issue of [
  "row-overflow",
  "byte-overflow",
  "next-page",
  "nested-value",
  "bad-metadata",
])
  test(`bounded result rejects ${issue} without partial output`, async (t) => {
    const options =
      issue === "row-overflow"
        ? { rows: Array(1002).fill({ v: "a" }) }
        : issue === "byte-overflow"
          ? { rows: [{ v: "x".repeat(1048577) }] }
          : issue === "next-page"
            ? { nextQuery: { pageToken: "private" } }
            : issue === "nested-value"
              ? { rows: [{ v: { nested: { secret: "x" } } }] }
              : {
                  metadataChange: (m) => {
                    m.extra = "x".repeat(65537);
                  },
                };
    const f = fixture(t, options);
    await assert.rejects(
      f.driver.runQueryJob(query(), f.request),
      /unavailable/,
    );
  });
test("sealed ledger rejects new refresh jobs but permits known terminal result replay", async (t) => {
  const f = fixture(t);
  await f.driver.runQueryJob(query(), f.request);
  f.ledger.seal(f.p.executionId, 1);
  await f.driver.runQueryJob(query(), f.request);
  await assert.rejects(
    f.driver.runQueryJob(query({ query: "SELECT 2", params: [] }), f.request),
    /unavailable/,
  );
  assert.equal(f.calls.length, 1);
});
test("remaining inherited discovery/storage/write helpers cannot bypass guarded query path", async (t) => {
  const f = fixture(t);
  for (const method of [
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
    await assert.rejects(
      Promise.resolve().then(() => f.driver[method]()),
      /disabled/,
    );
  assert.equal(f.calls.length, 0);
});
test("a fenced generation during result retrieval never releases old results", async (t) => {
  const f = fixture(t);
  const create = f.sdk.createQueryJob.bind(f.sdk);
  f.sdk.createQueryJob = async (r) => {
    const [job] = await create(r);
    job.getQueryResults = async () => {
      f.ledger.register({ ...f.p, generation: 2 }, Date.now());
      return [[{ tenant: "north" }], null, {}];
    };
    return [job];
  };
  await assert.rejects(f.driver.runQueryJob(query(), f.request), /unavailable/);
});
test("tightened execution deadline applies to in-flight polling", async (t) => {
  const f = fixture(t, { status: "RUNNING" });
  const create = f.sdk.createQueryJob.bind(f.sdk);
  f.sdk.createQueryJob = async (r) => {
    const [job] = await create(r);
    const metadata = job.getMetadata;
    job.getMetadata = async () => {
      f.ledger.register({ ...f.p, deadlineMs: Date.now() + 5 }, Date.now());
      job.getMetadata = metadata;
      return metadata();
    };
    return [job];
  };
  const start = Date.now();
  await assert.rejects(f.driver.runQueryJob(query(), f.request), /unavailable/);
  assert.ok(Date.now() - start < 1000);
});
for (const field of [
  "destinationTable",
  "writeDisposition",
  "createDisposition",
  "unknownExecutionField",
])
  test(`provider reconciliation rejects unapproved ${field}`, async (t) => {
    const f = fixture(t, {
      metadataChange: (m) => {
        m.configuration.query[field] =
          field === "destinationTable"
            ? {
                projectId: "fixture-project",
                datasetId: "fixture_dataset",
                tableId: "permanent_result",
              }
            : "WRITE_TRUNCATE";
      },
    });
    await assert.rejects(
      f.driver.runQueryJob(query(), f.request),
      /unavailable/,
    );
    assert.equal(f.ledger.seal(f.p.executionId, 1).allJobsDone, false);
  });
test("generation revoked during metadata causes zero result RPCs", async (t) => {
  const f = fixture(t);
  const create = f.sdk.createQueryJob.bind(f.sdk);
  let reads = 0;
  f.sdk.createQueryJob = async (r) => {
    const [job] = await create(r);
    const get = job.getMetadata;
    job.getMetadata = async () => {
      const result = await get();
      f.ledger.register({ ...f.p, generation: 2 }, Date.now());
      return result;
    };
    job.getQueryResults = async () => {
      reads++;
      return [[{ tenant: "north" }], null, {}];
    };
    return [job];
  };
  await assert.rejects(f.driver.runQueryJob(query(), f.request), /unavailable/);
  assert.equal(reads, 0);
});
test("wall-clock rollback cannot extend the original monotonic polling deadline", async (t) => {
  const f = fixture(t, { status: "RUNNING", deadlineMs: Date.now() + 50 });
  const create = f.sdk.createQueryJob.bind(f.sdk);
  const wallNow = Date.now;
  let rollback = false;
  f.sdk.createQueryJob = async (request) => {
    const [job] = await create(request);
    const get = job.getMetadata;
    job.getMetadata = async () => {
      if (!rollback) {
        rollback = true;
        Date.now = () => wallNow() - 60000;
      }
      return get();
    };
    return [job];
  };
  const work = f.driver.runQueryJob(query(), f.request).then(
    () => "unexpected-success",
    () => "finished",
  );
  let observed;
  try {
    observed = await Promise.race([
      work,
      new Promise((resolve) => setTimeout(() => resolve("extended"), 200)),
    ]);
  } finally {
    Date.now = wallNow;
    await work;
  }
  assert.equal(observed, "finished");
  assert.equal(f.cancels, 1);
  assert.equal(f.ledger.seal(f.p.executionId, 1).allJobsDone, false);
});
