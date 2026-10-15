"use strict";
const servingGuard = require("./bigquery-serving-guard.cjs").installGuard();
require("node:test").after(() => servingGuard.assertClean());
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");
const runtimeRoot =
  process.env.GATEWAY_BIGQUERY_RUNTIME_DIR ||
  path.resolve(__dirname, "../../cube/bigquery-runtime");
const C = require(path.join(runtimeRoot, "contract.cjs"));
const file = path.join(runtimeRoot, "server.cjs");
function clean() {
  for (const k of Object.keys(process.env)) {
    try {
      C.validateEnvironment({ [k]: process.env[k] });
    } catch {
      delete process.env[k];
    }
  }
  process.env.NODE_ENV = "production";
  process.env.CUBEJS_DEV_MODE = "false";
  process.env.PORT = "0";
  process.env.CUBEJS_EXTERNAL_DEFAULT = "false";
}
test(
  "actual Cube1.8.5 programmatic startup and health issue zero provider/auth requests",
  { timeout: 20000 },
  async () => {
    assert.equal(fs.existsSync(file), true);
    clean();
    const { createCubeOptions, createServer } = require(file);
    const { PostgresLedger } = require(
      path.join(runtimeRoot, "postgres-ledger.cjs"),
    );
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
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "bq-static-server-"));
    let server, port;
    let authority;
    let provider = 0,
      db = 0;
    const { BigQuery } = require("@google-cloud/bigquery");
    const { Compute } = require("google-auth-library");
    const request = BigQuery.prototype.request,
      auth = Compute.prototype.getRequestHeaders;
    BigQuery.prototype.request = () => {
      provider++;
      throw Error("Unapproved provider call");
    };
    Compute.prototype.getRequestHeaders = () => {
      provider++;
      throw Error("Unapproved auth call");
    };
    try {
      fs.mkdirSync(path.join(root, "model"));
      fs.copyFileSync(
        path.join(__dirname, "bigquery-serving-bundle/model/usage.yml"),
        path.join(root, "model", "usage.yml"),
      );
      fs.writeFileSync(
        path.join(root, "security.json"),
        JSON.stringify(contract),
      );
      const bundle = require(path.join(runtimeRoot, "config.cjs")).loadBundle(
        root,
      );
      profile.model_bundle_digest = bundle.digest;
      const ledger = new PostgresLedger(
        {
          connect: async () => {
            db++;
            if (!authority)
              throw Error("Unexpected registry access at startup");
            return {
              query: async (sql, args) => {
                let value = true;
                if (sql.includes("source_check_attempt")) {
                  assert.deepEqual(JSON.parse(args[0]), authority);
                  value = {
                    allowed: true,
                    deadline_at: authority.deadline_at,
                    context_digest: authority.context_digest,
                  };
                } else if (
                  ![
                    "BEGIN",
                    "COMMIT",
                    "ROLLBACK",
                    "SET LOCAL statement_timeout = 5000",
                    "SET LOCAL synchronous_commit = on",
                    "SELECT gateway.source_check_epoch($1::text) AS value",
                  ].includes(sql)
                )
                  throw Error("Unexpected SQL function");
                return { rows: [{ value }] };
              },
              release() {},
            };
          },
        },
        { deploymentEpoch: "fixture-epoch" },
      );
      const options = createCubeOptions({
        profile,
        bundle,
        signingKey: "x".repeat(43),
        ledger,
      });
      assert.equal(options.compilerCacheSize, 32);
      assert.equal(options.maxCompilerCacheKeepAlive, 120000);
      assert.equal(options.allowNodeRequire, false);
      server = createServer(options);
      const started = await server.listen();
      port = started.server.address().port;
      servingGuard.allowLoopbackPort(port);
      const response = await fetch(`http://127.0.0.1:${port}/livez`);
      assert.equal(response.status, 200);
      const missing = await fetch(
        `http://127.0.0.1:${port}/cubejs-api/v1/meta`,
      );
      assert.equal(missing.status, 403);
      assert.equal(provider, 0);
      assert.equal(db, 0);
      // Static metadata exercises the real compiler against retained bytes. Only
      // this unit test's read-only registry authority boundary is stubbed.
      const scope = {
        principal_id: "person",
        policy_revision: 1,
        tenant_id: "tenant",
        row_constraints: [
          { member: "Usage.tenant", operator: "eq", values: ["tenant"] },
          { member: "Usage.group", operator: "in", values: ["public"] },
        ],
      };
      const R = require(path.join(runtimeRoot, "config.cjs")),
        S = require(path.join(runtimeRoot, "security.cjs"));
      authority = {
        provider: "bigquery",
        source_id: profile.source_id,
        source_revision: profile.revision,
        context: profile.context,
        capacity_key: R.capacityKey(profile),
        definition_digest: C.digest(profile),
        request_digest: "b".repeat(64),
        prepared_digest: C.digest({}),
        scope_digest: S.authorizationScopeDigest(scope),
        decision_digest: "e".repeat(64),
        binding_digest: "f".repeat(64),
        model_bundle_digest: bundle.digest,
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
      const now = Math.floor(Date.now() / 1000),
        claims = {
          ...scope,
          iss: "dpg-gateway",
          aud: "dpg-cube-source-v1",
          iat: now,
          exp: now + 60,
          model_bundle_digest: bundle.digest,
          context: profile.context,
          adapter_revision: "cube-bigquery-v1",
          contract_digest: C.digest(contract),
          source_attempt: authority,
          cube_query_digest: C.digest({}),
        };
      const token = require("jsonwebtoken").sign(claims, "x".repeat(43), {
        algorithm: "HS256",
      });
      fs.writeFileSync(
        path.join(root, "model", "usage.yml"),
        "cubes: invalid-unreviewed-content\n",
      );
      fs.writeFileSync(path.join(root, "security.json"), "{}");
      const meta = await fetch(`http://127.0.0.1:${port}/cubejs-api/v1/meta`, {
        headers: {
          Authorization: "Bearer " + token,
          "X-Request-Id": authority.session_id + ".g1",
        },
      });
      const result = await meta.json();
      assert.equal(meta.status, 200, JSON.stringify(result));
      assert.equal(result.cubes[0].name, "Usage");
      assert.equal(provider, 0);
      // A source attempt cannot be paired with another valid signed entitlement.
      const swapped = { ...claims, principal_id: "different-principal" };
      const rejected = await fetch(
        `http://127.0.0.1:${port}/cubejs-api/v1/meta`,
        {
          headers: {
            Authorization:
              "Bearer " +
              require("jsonwebtoken").sign(swapped, "x".repeat(43), {
                algorithm: "HS256",
              }),
            "X-Request-Id": authority.session_id + ".g1",
          },
        },
      );
      assert.equal(rejected.status, 403);
      assert.equal(db, 1);
      assert.equal(provider, 0);
    } finally {
      if (server) {
        servingGuard.revokeLoopbackPort(port);
        await server.shutdown("SIGTERM");
      }
      BigQuery.prototype.request = request;
      Compute.prototype.getRequestHeaders = auth;
      fs.rmSync(root, { recursive: true, force: true });
      require("@cubejs-backend/native").resetLogger("error");
    }
  },
);
test("server rejects ambient side channels including agent forwarding and native SQL gateways", () => {
  clean();
  const { validateServerEnvironment } = require(file);
  assert.equal(typeof validateServerEnvironment, "function");
  assert.doesNotThrow(() => validateServerEnvironment());
  for (const name of [
    "CUBEJS_AGENT_ENDPOINT_URL",
    "CUBE_JS_NATIVE_API_GATEWAY_INTERNAL",
    "CUBEJS_SQL_PORT",
    "CUBEJS_EXT_DB_HOST",
    "CUBEJS_API_SECRET",
    "NODE_TLS_REJECT_UNAUTHORIZED",
  ]) {
    process.env[name] = "false";
    try {
      assert.throws(() => validateServerEnvironment(), name);
    } finally {
      delete process.env[name];
    }
  }
});
test("maintained HTTP port uses numeric PORT and rejects ignored Cube or Unix-socket overrides", () => {
  clean();
  const { validateServerEnvironment } = require(file);
  const saved = process.env.PORT;
  try {
    for (const value of [
      "/tmp/cube.sock",
      "-1",
      "65536",
      "0x20",
      "",
      "04000",
    ]) {
      process.env.PORT = value;
      assert.throws(() => validateServerEnvironment());
    }
    process.env.PORT = "0";
    assert.doesNotThrow(() => validateServerEnvironment());
    process.env.CUBEJS_PORT = "0";
    assert.throws(() => validateServerEnvironment());
  } finally {
    delete process.env.CUBEJS_PORT;
    if (saved === undefined) delete process.env.PORT;
    else process.env.PORT = saved;
  }
});
