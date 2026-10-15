"use strict";
const servingGuard = require("./bigquery-serving-guard.cjs").installGuard();
require("node:test").after(() => servingGuard.assertClean());
// Unit transport boundary tests. These do not simulate or qualify durable SQL transitions.
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const runtimeRoot =
  process.env.GATEWAY_BIGQUERY_RUNTIME_DIR ||
  path.resolve(__dirname, "../../cube/bigquery-runtime");
const C = require(path.join(runtimeRoot, "contract.cjs"));
const file = path.join(runtimeRoot, "reconciler.cjs");
const profile = {
  provider: "bigquery",
  source_id: "usage",
  revision: 1,
  context: "consumer",
  auth: {
    mode: "gke-workload-identity",
    expected_principal: "executor@fixture-project.iam.gserviceaccount.com",
  },
  billing_project: "fixture-project",
  data_project: "fixture-project",
  dataset: "inputs",
  tables: ["usage"],
  location: "US",
  dataset_location: "US",
  cube_endpoint: "https://cube.example.test",
  model_bundle_digest: "1".repeat(64),
  source_policy_digest: "2".repeat(64),
  max_jobs_per_execution: 2,
  per_job_maximum_bytes_billed: 1048576,
  total_execution_bytes: 2097152,
  max_active_jobs: 1,
  max_global_active_jobs: 2,
  job_timeout_ms: 25000,
};
const observerAuth = {
  mode: "gke-workload-identity",
  expected_principal: "observer@fixture-project.iam.gserviceaccount.com",
};
function clean() {
  for (const k of Object.keys(process.env)) {
    try {
      C.validateEnvironment({ [k]: process.env[k] });
    } catch {
      delete process.env[k];
    }
  }
}
test("metadata-only facade is lazy and has no query/row/insert/cancel entrypoint", () => {
  assert.equal(fs.existsSync(file), true);
  clean();
  const { MetadataClient } = require(file);
  const client = new MetadataClient(profile, observerAuth);
  assert.deepEqual(
    Object.getOwnPropertyNames(Object.getPrototypeOf(client)).sort(),
    ["constructor", "read"].sort(),
  );
});
for (const rotated of [false, true])
  test(`bounded observation pass retains old metadata authority after rotation=${rotated}`, async () => {
    clean();
    const { reconcileOnce } = require(file);
    const { PostgresLedger } = require(
      path.join(runtimeRoot, "postgres-ledger.cjs"),
    );
    const job = {
      job_id: "sp_" + "a".repeat(64),
      session_id: "f22bdd8e-2f70-4e11-8987-ed9bdd4e176c",
      group_id: "a1e393af-394c-4de5-8932-a7d1dfb967c9",
      semantic_key: "b".repeat(64),
      project_id: profile.billing_project,
      location: "US",
      principal: profile.auth.expected_principal,
      intent: {
        query_digest: C.digest("SELECT 1"),
        parameters_digest: C.digest([]),
        parameter_types_digest: C.digest([]),
        maximum_bytes_billed: "1048576",
        job_timeout_ms: 25000,
      },
      intent_digest: "c".repeat(64),
      state: "intent",
      terminal_error: null,
    };
    job.intent.configuration_digest = C.configurationDigest(job);
    const calls = [];
    const client = {
      query: async (sql, args) => {
        calls.push({ sql, args });
        let value = true;
        if (sql.includes("reconcile_claim"))
          value = [
            {
              job,
              source: profile,
              observation_token: "opaque-observation-token",
            },
          ];
        if (sql.includes("check_observation")) value = job;
        return { rows: [{ value }] };
      },
      release() {},
    };
    const ledger = new PostgresLedger(
      { connect: async () => client },
      { deploymentEpoch: "fixture-epoch" },
    );
    const { BigQuery } = require("@google-cloud/bigquery");
    const original = BigQuery.prototype.request;
    let provider = 0;
    BigQuery.prototype.request = function (req, callback) {
      provider++;
      assert.equal(req.uri, "jobs/" + job.job_id);
      assert.equal(
        this.authClient.cachedCredential.serviceAccountEmail,
        observerAuth.expected_principal,
      );
      setImmediate(() =>
        callback(null, {
          jobReference: {
            jobId: job.job_id,
            projectId: job.project_id,
            location: job.location,
          },
          user_email: job.principal,
          configuration: {
            jobTimeoutMs: "25000",
            query: {
              query: "SELECT 1",
              queryParameters: [],
              parameterMode: "POSITIONAL",
              useLegacySql: false,
              useQueryCache: false,
              maximumBytesBilled: "1048576",
            },
          },
          status: { state: "DONE" },
          statistics: { query: { statementType: "SELECT" } },
        }),
      );
    };
    try {
      const currentProfile = rotated
        ? {
            ...profile,
            revision: 2,
            auth: {
              ...profile.auth,
              expected_principal:
                "replacement@fixture-project.iam.gserviceaccount.com",
            },
          }
        : profile;
      assert.deepEqual(
        await reconcileOnce({ ledger, profile: currentProfile, observerAuth }),
        {
          claimed: 1,
          observed: 1,
          unknown: 0,
        },
      );
    } finally {
      BigQuery.prototype.request = original;
    }
    assert.equal(provider, 1);
    assert.ok(calls.some((c) => c.sql.includes("source_bq_observe")));
    assert.ok(calls.some((c) => c.sql.includes("source_settle_group")));
    assert.equal(
      calls.some((c) =>
        /source_(check_attempt|bq_check_data|bq_admit)/.test(c.sql),
      ),
      false,
    );
    const evidence = JSON.parse(
      calls.find((c) => c.sql.includes("source_bq_observe")).args[1],
    );
    assert.deepEqual(evidence, {
      intent_digest: job.intent_digest,
      state: "done",
      terminal_error: false,
    });
  });
