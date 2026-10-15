"use strict";
const servingGuard = require("./bigquery-serving-guard.cjs").installGuard();
require("node:test").after(() => servingGuard.assertClean());
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const runtimeRoot =
  process.env.GATEWAY_BIGQUERY_RUNTIME_DIR ||
  path.resolve(__dirname, "../../cube/bigquery-runtime");
const C = require(path.join(runtimeRoot, "contract.cjs"));
const root = runtimeRoot;
function source() {
  return {
    projectId: "fixture-project",
    location: "US",
    principal: "executor@fixture-project.iam.gserviceaccount.com",
    inputTables: ["fixture-project.inputs.usage"],
    jobTimeoutMs: 25000,
    pollIntervalMs: 1,
    maxRows: 1000,
    maxResultBytes: 1048576,
    maxMetadataBytes: 65536,
  };
}
function context() {
  return {
    provider: "bigquery",
    source_id: "fixture-source",
    source_revision: 1,
    context: "consumer",
    capacity_key: "bq:fixture",
    definition_digest: "a".repeat(64),
    request_digest: "b".repeat(64),
    prepared_digest: "c".repeat(64),
    scope_digest: "d".repeat(64),
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
}
function cleanEnv() {
  for (const key of Object.keys(process.env)) {
    try {
      C.validateEnvironment({ [key]: process.env[key] });
    } catch {
      delete process.env[key];
    }
  }
}
test("actual maintained driver constructs explicit lazy Compute auth with zero auth/SDK calls", () => {
  assert.equal(fs.existsSync(path.join(root, "driver.cjs")), true);
  cleanEnv();
  const { createServingDriver } = require(path.join(root, "driver.cjs"));
  const { PostgresLedger } = require(path.join(root, "postgres-ledger.cjs"));
  const ledger = new PostgresLedger(
    {
      connect: () => {
        throw Error("Unexpected DB access during construction");
      },
    },
    { deploymentEpoch: "fixture-epoch" },
  );
  const driver = createServingDriver({
    source: source(),
    context: context(),
    ledger,
  });
  const { BigQueryDriver } = require("@cubejs-backend/bigquery-driver");
  const { Compute } = require("google-auth-library");
  assert.ok(driver instanceof BigQueryDriver);
  assert.ok(driver.bigquery.authClient.cachedCredential instanceof Compute);
  assert.equal(
    driver.bigquery.authClient.cachedCredential.serviceAccountEmail,
    source().principal,
  );
  assert.equal(driver.options.autoRetry, false);
  assert.equal(driver.options.maxRetries, 0);
  assert.equal(driver.options.keyFilename, undefined);
  assert.equal(driver.options.credentials, undefined);
  assert.deepEqual(driver.options.scopes, [
    "https://www.googleapis.com/auth/bigquery",
  ]);
  assert.equal(driver.storage, null);
});
test("all inherited discovery/write/export/stream paths fail closed before provider access; startup testConnection is static", async () => {
  cleanEnv();
  const { createServingDriver, DISABLED_METHODS } = require(
    path.join(root, "driver.cjs"),
  );
  const { PostgresLedger } = require(path.join(root, "postgres-ledger.cjs"));
  const driver = createServingDriver({
    source: source(),
    context: context(),
    ledger: new PostgresLedger(
      {
        connect: () => {
          throw Error("Unexpected DB");
        },
      },
      { deploymentEpoch: "fixture-epoch" },
    ),
  });
  driver.bigquery.request = () => {
    throw Error("Provider request must never run");
  };
  for (const method of DISABLED_METHODS)
    await assert.rejects(
      async () => driver[method](),
      (e) => e.message === C.SAFE,
      method,
    );
  await driver.testConnection();
  assert.equal(driver.readOnly(), true);
  assert.equal(await driver.isUnloadSupported(), false);
});
test("missing/conflicting generations or invalid SQL fail before even ledger admission", async () => {
  cleanEnv();
  const { createServingDriver } = require(path.join(root, "driver.cjs"));
  const { PostgresLedger } = require(path.join(root, "postgres-ledger.cjs"));
  const ctx = context();
  let calls = 0;
  const driver = createServingDriver({
    source: source(),
    context: ctx,
    ledger: new PostgresLedger(
      {
        connect: () => {
          calls++;
          throw Error("DB should not run");
        },
      },
      { deploymentEpoch: "fixture-epoch" },
    ),
  });
  for (const requestId of [
    undefined,
    ctx.session_id + ".g2",
    ctx.session_id + ".g1-span-id",
    ctx.session_id + ".g01",
    ctx.session_id + ".g1,other",
  ])
    await assert.rejects(driver.query("SELECT ?", ["a"], { requestId }));
  await assert.rejects(
    driver.query("DELETE FROM usage", [], {
      requestId: ctx.session_id + ".g1",
    }),
  );
  assert.equal(calls, 0);
});
module.exports = { source, context };
test("actual SDK insert serialization includes only reviewed source fields and retains exact first config", async () => {
  cleanEnv();
  const { submissionOptions } = require(path.join(root, "driver.cjs"));
  assert.equal(typeof submissionOptions, "function");
  const { BigQuery } = require("@google-cloud/bigquery");
  const { Compute } = require("google-auth-library");
  const client = new BigQuery({
    projectId: "fixture-project",
    location: "US",
    authClient: new Compute(),
    autoRetry: false,
    maxRetries: 0,
  });
  let wire;
  client.request = (req, cb) => {
    wire = structuredClone(req.json);
    setImmediate(() =>
      cb(null, {
        jobReference: wire.jobReference,
        status: { state: "PENDING" },
      }),
    );
  };
  const job = {
    job_id: "sp_" + "a".repeat(64),
    location: "US",
    intent: { maximum_bytes_billed: "1048576", job_timeout_ms: 25000 },
  };
  await client.createQueryJob(
    submissionOptions(
      {
        query: "SELECT ? AS total",
        params: ["9007199254740993"],
        parameterMode: "positional",
        useLegacySql: false,
        wrapIntegers: true,
      },
      job,
    ),
  );
  assert.deepEqual(Object.keys(wire.configuration).sort(), [
    "jobTimeoutMs",
    "query",
  ]);
  assert.deepEqual(Object.keys(wire.configuration.query).sort(), [
    "maximumBytesBilled",
    "parameterMode",
    "query",
    "queryParameters",
    "useLegacySql",
    "useQueryCache",
  ]);
  assert.equal(wire.configuration.jobTimeoutMs, "25000");
  assert.equal(wire.configuration.query.maximumBytesBilled, "1048576");
  assert.deepEqual(wire.configuration.query.queryParameters, [
    {
      parameterType: { type: "STRING" },
      parameterValue: { value: "9007199254740993" },
    },
  ]);
  assert.equal(wire.jobReference.jobId, job.job_id);
});
test("SDK insert with retained job ID does not create another job on conflict or unknown response", async () => {
  cleanEnv();
  const { submissionOptions } = require(path.join(root, "driver.cjs"));
  const { BigQuery } = require("@google-cloud/bigquery");
  const { Compute } = require("google-auth-library");
  for (const code of [409, 500]) {
    let calls = 0;
    const sdk = new BigQuery({
      projectId: "fixture-project",
      location: "US",
      authClient: new Compute(),
      autoRetry: false,
      maxRetries: 0,
    });
    sdk.request = (req, cb) => {
      calls++;
      setImmediate(() =>
        cb(Object.assign(new Error("provider diagnostic"), { code })),
      );
    };
    await assert.rejects(
      sdk.createQueryJob(
        submissionOptions(
          {
            query: "SELECT 1",
            params: [],
            parameterMode: "positional",
            useLegacySql: false,
            wrapIntegers: true,
          },
          {
            job_id: "sp_" + "a".repeat(64),
            location: "US",
            intent: { maximum_bytes_billed: "1", job_timeout_ms: 1 },
          },
        ),
      ),
    );
    assert.equal(calls, 1);
  }
});

test("first durable intent reserves transit headroom and rejects short time before admission", async () => {
  cleanEnv();
  const { createServingDriver } = require(path.join(root, "driver.cjs"));
  const { PostgresLedger } = require(path.join(root, "postgres-ledger.cjs"));
  for (const remaining of [30000, 5000]) {
    const ctx = context();
    ctx.deadline_at = new Date(Date.now() + remaining).toISOString();
    const ledger = new PostgresLedger(
      {
        connect: () => {
          throw Error("Unexpected direct SQL");
        },
      },
      { deploymentEpoch: "fixture-epoch" },
    );
    let admission = 0,
      rpc = 0;
    ledger.checkAttempt = async () => ({
      allowed: true,
      context_digest: ctx.context_digest,
      deadline_at: ctx.deadline_at,
    });
    ledger.lookupJob = async () => null;
    ledger.admitJob = async (context, key, intent) => {
      admission++;
      assert.ok(
        intent.job_timeout_ms > 23000 && intent.job_timeout_ms <= 24000,
      );
      throw Error("Bounded admission probe stops here");
    };
    ledger.sealAttempt = async () => ({});
    const driver = createServingDriver({
      source: { ...source(), jobTimeoutMs: 30000 },
      context: ctx,
      ledger,
    });
    driver.bigquery.request = () => {
      rpc++;
      throw Error("Unexpected provider");
    };
    await assert.rejects(
      driver.query("SELECT ? AS total", ["1"], {
        requestId: ctx.session_id + ".g1",
      }),
    );
    assert.equal(admission, remaining === 30000 ? 1 : 0);
    assert.equal(rpc, 0);
  }
});
