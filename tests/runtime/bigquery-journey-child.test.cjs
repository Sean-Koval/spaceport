"use strict";
const guard = require("./bigquery-serving-guard.cjs").installGuard();
const test = require("node:test"),
  assert = require("node:assert/strict"),
  fs = require("node:fs"),
  path = require("node:path"),
  os = require("node:os"),
  { spawn } = require("node:child_process");
const runtime =
  process.env.GATEWAY_BIGQUERY_RUNTIME_DIR ||
  path.resolve(__dirname, "../../cube/bigquery-runtime");
const C = require(path.join(runtime, "contract.cjs")),
  R = require(path.join(runtime, "config.cjs"));
test(
  "two-context genuine Cube child starts with no provider/registry work and shuts down naturally",
  { timeout: 15000 },
  async () => {
    const root = fs.mkdtempSync(
      path.join(os.tmpdir(), "source-journey-child-"),
    );
    let child,
      ports = [];
    try {
      fs.mkdirSync(path.join(root, "secrets"));
      const profiles = ["consumer", "fixture_test"].map((context, index) => {
        const bundle = path.join(root, context);
        fs.cpSync(path.join(__dirname, "bigquery-serving-bundle"), bundle, {
          recursive: true,
        });
        fs.appendFileSync(
          path.join(bundle, "model/usage.yml"),
          "# " + context + "\n",
        );
        const digest = R.bundleDigest(bundle),
          passwordFile = path.join(root, "secrets", context + "-db"),
          keyFile = path.join(root, "secrets", context + "-key");
        fs.writeFileSync(passwordFile, "p".repeat(43));
        fs.writeFileSync(keyFile, (index ? "y" : "x").repeat(43));
        return {
          source: {
            provider: "bigquery",
            source_id: "usage-" + context.replace("_", "-"),
            revision: 1,
            context,
            auth: {
              mode: "gke-workload-identity",
              expected_principal:
                (index ? "fixture" : "executor") +
                "@fixture-project.iam.gserviceaccount.com",
            },
            billing_project: "fixture-project",
            data_project: "fixture-project",
            dataset: "inputs",
            tables: ["usage"],
            location: "US",
            dataset_location: "US",
            cube_endpoint:
              "https://" + (index ? "fixture" : "consumer") + ".example.test",
            model_bundle_digest: digest,
            source_policy_digest: "a".repeat(64),
            max_jobs_per_execution: 2,
            per_job_maximum_bytes_billed: 1048576,
            total_execution_bytes: 2097152,
            max_active_jobs: 2,
            max_global_active_jobs: 4,
            job_timeout_ms: 25000,
          },
          bundle: { root: bundle, digest },
          runtime: {
            user: index ? "fixture_runtime" : "consumer_runtime",
            password_file: passwordFile,
          },
          signing_key_file: keyFile,
        };
      });
      const config = path.join(root, "config.json");
      fs.writeFileSync(
        config,
        JSON.stringify({
          schema_version: 1,
          deployment_epoch: "synthetic-epoch",
          registry: {
            host: "127.0.0.1",
            port: 1,
            database: "gateway_source_test",
          },
          profiles,
        }),
      );
      const env = {
        PATH: process.env.PATH,
        HOME: "/tmp/spaceport-no-adc",
        NODE_ENV: "production",
        CUBEJS_DEV_MODE: "false",
        PORT: "0",
        GATEWAY_SOURCE_TEST_AUTHORIZATION: "private-synthetic-source-only",
        GATEWAY_BQ_JOURNEY_CONFIG_FILE: config,
        GATEWAY_BIGQUERY_RUNTIME_DIR: runtime,
      };
      if (process.env.NODE_PATH) env.NODE_PATH = process.env.NODE_PATH;
      child = spawn(
        process.execPath,
        [path.join(__dirname, "bigquery-journey-child.cjs")],
        { env, stdio: ["ignore", "pipe", "pipe"] },
      );
      let pending = "",
        bytes = 0,
        ready,
        result;
      let resolveReady, rejectReady;
      const readiness = new Promise((resolve, reject) => {
        resolveReady = resolve;
        rejectReady = reject;
      });
      child.stdout.on("data", (chunk) => {
        bytes += chunk.length;
        if (bytes > 65536) {
          child.kill("SIGKILL");
          return rejectReady(Error("Bounded child output exceeded"));
        }
        pending += chunk.toString("utf8");
        let newline;
        while ((newline = pending.indexOf("\n")) >= 0) {
          const line = pending.slice(0, newline);
          pending = pending.slice(newline + 1);
          try {
            if (line.startsWith("GATEWAY_BQ_JOURNEY_READY ")) {
              assert.equal(ready, undefined);
              ready = JSON.parse(
                line.slice("GATEWAY_BQ_JOURNEY_READY ".length),
              );
              resolveReady(ready);
            }
            if (line.startsWith("GATEWAY_BQ_JOURNEY_RESULT ")) {
              assert.equal(result, undefined);
              result = JSON.parse(
                line.slice("GATEWAY_BQ_JOURNEY_RESULT ".length),
              );
            }
          } catch {
            rejectReady(Error("Invalid bounded child protocol"));
            child.kill("SIGKILL");
          }
        }
      });
      child.stderr.on("data", (chunk) => {
        bytes += chunk.length;
        if (bytes > 65536) child.kill("SIGKILL");
      });
      const completion = new Promise((resolve, reject) => {
        child.once("error", reject);
        child.once("close", (code, signal) => {
          if (!ready) rejectReady(Error("Child did not become ready"));
          resolve({ code, signal });
        });
      });
      const timeout = setTimeout(
        () => rejectReady(Error("Child startup exceeded bound")),
        8000,
      );
      try {
        await readiness;
      } finally {
        clearTimeout(timeout);
      }
      assert.equal(ready.schema_version, 1);
      assert.deepEqual(
        ready.listeners.map((l) => l.context),
        ["consumer", "fixture_test"],
      );
      assert.deepEqual(
        ready.listeners.map((l) => l.origin),
        profiles.map((p) => p.source.cube_endpoint),
      );
      ports = ready.listeners.map((l) => l.port);
      assert.equal(new Set(ports).size, 2);
      for (const port of ports) {
        guard.allowLoopbackPort(port);
        assert.equal(
          (await fetch("http://127.0.0.1:" + port + "/livez")).status,
          200,
        );
      }
      child.kill("SIGTERM");
      assert.deepEqual(await completion, { code: 0, signal: null });
      assert.deepEqual(result, {
        schema_version: 1,
        status: "passed",
        counts: { inserts: 0, metadata: 0, results: 0, provider_errors: 0 },
      });
    } finally {
      if (child && child.exitCode === null) {
        child.kill("SIGKILL");
        await new Promise((resolve) => child.once("close", resolve));
      }
      for (const port of ports) guard.revokeLoopbackPort(port);
      fs.rmSync(root, { recursive: true, force: true });
      guard.assertClean();
    }
  },
);

test("initially supplied preaggregation override is rejected and never cleared", () => {
  const { resetOwnedCubePin } = require("./bigquery-journey-child.cjs");
  const { validateServerEnvironment } = require(
    path.join(runtime, "server.cjs"),
  );
  process.env.CUBEJS_PRE_AGGREGATIONS_SCHEMA = "untrusted_schema";
  try {
    assert.throws(() => resetOwnedCubePin(false));
    assert.throws(() => resetOwnedCubePin(true));
    assert.throws(() => validateServerEnvironment());
    assert.equal(
      process.env.CUBEJS_PRE_AGGREGATIONS_SCHEMA,
      "untrusted_schema",
    );
  } finally {
    delete process.env.CUBEJS_PRE_AGGREGATIONS_SCHEMA;
  }
});

test("actual SDK read serialization has only effective GET on exact retained-job paths", async () => {
  for (const key of Object.keys(process.env)) {
    try {
      C.validateEnvironment({ [key]: process.env[key] });
    } catch {
      delete process.env[key];
    }
  }
  const { readJobPath } = require("./bigquery-journey-child.cjs");
  const { BigQuery } = require("@google-cloud/bigquery");
  const auth = require(path.join(runtime, "driver.cjs")).explicitAuth(
    "executor@fixture-project.iam.gserviceaccount.com",
  );
  const client = new BigQuery({
    projectId: "fixture-project",
    authClient: auth,
    location: "US",
    autoRetry: false,
    maxRetries: 0,
  });
  const id = "sp_" + "a".repeat(64),
    calls = [];
  client.request = (request, callback) => {
    readJobPath(request);
    calls.push({ uri: request.uri, method: request.method });
    setImmediate(() =>
      callback(
        null,
        request.uri.startsWith("/queries/")
          ? {
              jobComplete: true,
              totalRows: "0",
              schema: { fields: [] },
              rows: [],
            }
          : {
              jobReference: {
                projectId: "fixture-project",
                location: "US",
                jobId: id,
              },
              status: { state: "DONE" },
            },
      ),
    );
  };
  const job = client.job(id, { location: "US" });
  await job.getMetadata();
  await job.getQueryResults({ autoPaginate: false, maxResults: 1 });
  assert.deepEqual(calls, [
    { uri: "jobs/" + id, method: undefined },
    { uri: "/queries/" + id, method: undefined },
  ]);
  guard.assertClean();
});

test("read fake rejects every non-GET method and unrelated URI", () => {
  const { readJobPath } = require("./bigquery-journey-child.cjs");
  const id = "sp_" + "a".repeat(64);
  for (const uri of ["jobs/" + id, "/queries/" + id]) {
    for (const method of [undefined, "GET"])
      assert.doesNotThrow(() => readJobPath({ uri, method }));
    for (const method of [null, "", "POST", "DELETE", "PATCH", "get"])
      assert.throws(() => readJobPath({ uri, method }));
  }
  for (const uri of [
    "/jobs/" + id,
    "queries/" + id,
    "/queries/" + id + "/extra",
    "jobs",
    undefined,
  ])
    assert.throws(() => readJobPath({ uri }));
});

test("TIMESTAMP wire microseconds hydrate through actual SDK and maintained driver", async () => {
  const { BigQuery } = require("@google-cloud/bigquery");
  const { BigQueryDriver } = require("@cubejs-backend/bigquery-driver");
  const auth = require(path.join(runtime, "driver.cjs")).explicitAuth(
    "executor@fixture-project.iam.gserviceaccount.com",
  );
  const client = new BigQuery({
    projectId: "fixture-project",
    authClient: auth,
    autoRetry: false,
    maxRetries: 0,
  });
  client.request = (request, callback) => {
    require("./bigquery-journey-child.cjs").readJobPath(request);
    assert.equal(request.qs["formatOptions.useInt64Timestamp"], true);
    setImmediate(() =>
      callback(null, {
        jobComplete: true,
        totalRows: "1",
        schema: {
          fields: [
            { name: "usage__account", type: "STRING" },
            { name: "usage__day_day", type: "TIMESTAMP" },
            { name: "usage__usage_events", type: "STRING" },
          ],
        },
        rows: [
          {
            f: [
              { v: "acct-a" },
              {
                v: String(
                  BigInt(Date.parse("2026-10-05T00:00:00.000Z")) * 1000n,
                ),
              },
              { v: "120" },
            ],
          },
        ],
      }),
    );
  };
  const result = await client
    .job("sp_" + "b".repeat(64), { location: "US" })
    .getQueryResults({
      autoPaginate: false,
      maxResults: 2,
      wrapIntegers: true,
    });
  assert.equal(
    result[0][0].usage__day_day.constructor.name,
    "BigQueryTimestamp",
  );
  assert.equal(result[0][0].usage__day_day.value, "2026-10-05T00:00:00.000Z");
  C.validateRows(result, { maxRows: 2, maxResultBytes: 1024 });
  const normalized = await BigQueryDriver.prototype.query.call(
    { runQueryJob: async () => result },
    "SELECT synthetic",
    [],
    {},
  );
  assert.deepEqual(normalized, [
    {
      usage__account: "acct-a",
      usage__day_day: "2026-10-05T00:00:00.000Z",
      usage__usage_events: "120",
    },
  ]);
  guard.assertClean();
});

test("fixed weekly oracle binds each scope predicate to its positional parameters", () => {
  const { predicateRows } = require("./bigquery-journey-child.cjs");
  const sql =
    "SELECT example FROM source AS `usage` WHERE (`usage`.event_date >= TIMESTAMP(?)) AND (`usage`.event_date < TIMESTAMP(?)) AND (`usage`.tenant_id = ?) AND (`usage`.access_group IN (?, ?)) AND (`usage`.account_id = ?) GROUP BY 1";
  const values = [
    "2026-10-05T00:00:00.000000Z",
    "2026-10-12T00:00:00.000000Z",
    "north",
    "core",
    "special",
    "acct-a",
  ];
  const rows = [
    {
      day: "2026-10-05T00:00:00.000",
      tenant: "north",
      group: "core",
      account: "acct-a",
    },
    {
      day: "2026-10-05T00:00:00.000",
      tenant: "south",
      group: "core",
      account: "acct-a",
    },
    {
      day: "2026-10-05T00:00:00.000",
      tenant: "north",
      group: "forbidden",
      account: "acct-a",
    },
    {
      day: "2026-10-12T00:00:00.000",
      tenant: "north",
      group: "core",
      account: "acct-a",
    },
    {
      day: "2026-10-05T00:00:00.000",
      tenant: "north",
      group: "special",
      account: "acct-b",
    },
  ];
  assert.deepEqual(predicateRows(sql, values, rows), [rows[0]]);
  for (const changed of [
    sql.replace("`usage`.tenant_id = ?", "? = ?"),
    sql.replace("`usage`.access_group IN (?, ?)", "? IN (?, ?)"),
    sql.replace(" AND ", " OR "),
    sql.replace("`usage`.account_id = ?", "1 = 1"),
  ])
    assert.throws(() => predicateRows(changed, values, rows));
  assert.throws(() =>
    predicateRows(
      sql,
      [...values.slice(0, 2), "core", "north", ...values.slice(4)],
      rows,
    ),
  );
  assert.throws(() => predicateRows(sql, [...values, "north"], rows));
  assert.throws(() =>
    predicateRows(
      sql.replace("(`usage`.event_date >= TIMESTAMP(?)) AND ", ""),
      values.slice(1),
      rows,
    ),
  );
  assert.throws(() =>
    predicateRows(
      sql.replace(" AND (`usage`.tenant_id = ?)", ""),
      [...values.slice(0, 2), ...values.slice(3)],
      rows,
    ),
  );
  assert.throws(() =>
    predicateRows(
      sql.replace(" AND (`usage`.access_group IN (?, ?))", ""),
      [...values.slice(0, 3), values[5]],
      rows,
    ),
  );
  assert.throws(() =>
    predicateRows(sql, [values[1], values[0], ...values.slice(2)], rows),
  );
});
