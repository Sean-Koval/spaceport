"use strict";
// Full serving acceptance: actual Cube HTTP/JWT/compiler/driver/SDK and real
// PostgreSQL functions. Only the final provider request boundary is synthetic.
const fs = require("node:fs"),
  path = require("node:path"),
  assert = require("node:assert/strict");
let checkpoint = "source-config";
const CHECKS = [
  "http_jwt_scope",
  "compiler_retained_bytes",
  "continue_wait",
  "main_refresh_budget",
  "same_job_repoll",
  "cache_eviction",
  "cube_restart",
  "current_authority_revocation",
  "late_metadata_observation",
  "generation_reclaim",
  "zero_job_settlement",
  "unknown_submission_hold",
];
const OPERATIONS = ["revoke_queued", "reclaim", "close_main", "close_zero"];
const CASES = ["main", "revocation", "reclaim", "zero_job", "unknown"];
async function run() {
  assert.equal(
    process.env.GATEWAY_SOURCE_TEST_AUTHORIZATION,
    "private-synthetic-source-only",
  );
  const fixtureFile = process.env.GATEWAY_SOURCE_FIXTURE_FILE;
  assert.ok(path.isAbsolute(fixtureFile || ""));
  const bytes = fs.readFileSync(fixtureFile);
  assert.ok(bytes.length <= 65536);
  const fixture = JSON.parse(bytes.toString("utf8"));
  assert.equal(fixture.schema_version, 1);
  assert.ok(
    ["registry-db", "127.0.0.1", "localhost"].includes(fixture.registry.host),
  );
  assert.equal(fixture.registry.database, "gateway_source_test");
  assert.ok(
    Number.isInteger(fixture.registry.port) &&
      fixture.registry.port > 0 &&
      fixture.registry.port <= 65535,
  );
  const guard = require("./bigquery-serving-guard.cjs").installGuard({
    registry: fixture.registry,
    allowedSecretFiles: [
      fixture.signing_key_file,
      fixture.runtime.password_file,
      fixture.observer.password_file,
    ],
  });
  const root =
    process.env.GATEWAY_BIGQUERY_RUNTIME_DIR ||
    path.resolve(__dirname, "../../cube/bigquery-runtime");
  const C = require(path.join(root, "contract.cjs")),
    R = require(path.join(root, "config.cjs")),
    S = require(path.join(root, "security.cjs"));
  const { PostgresLedger } = require(path.join(root, "postgres-ledger.cjs")),
    { createCubeOptions, createServer } = require(
      path.join(root, "server.cjs"),
    );
  const { reconcileOnce } = require(path.join(root, "reconciler.cjs"));
  const { Pool } = require("pg"),
    jwt = require("jsonwebtoken");
  const profile = C.immutable(R.validateProfile(fixture.source));
  const bundle = R.loadBundle(fixture.bundle.root, fixture.bundle.digest);
  assert.equal(bundle.digest, profile.model_bundle_digest);
  assert.deepEqual(bundle.contract, fixture.bundle.contract);
  assert.deepEqual(fixture.cases.map((c) => c.name).sort(), [...CASES].sort());
  const cases = new Map(fixture.cases.map((c) => [c.name, C.immutable(c)]));
  for (const c of cases.values()) {
    assert.deepEqual(c.claims.source_attempt, c.source_attempt);
    assert.equal(c.claims.cube_query_digest, C.digest(c.cube_query));
    assert.equal(c.source_attempt.prepared_digest, C.digest(c.cube_query));
    assert.equal(c.source_attempt.definition_digest, C.digest(profile));
    assert.equal(
      c.source_attempt.scope_digest,
      S.authorizationScopeDigest(c.claims),
    );
  }
  const key = R.regularFile(fixture.signing_key_file, 1024)
    .toString("utf8")
    .trim();
  assert.match(key, /^[A-Za-z0-9_-]{43}$/);
  const poolFor = (role) =>
    new Pool({
      ...fixture.registry,
      user: role.user,
      password: R.regularFile(role.password_file, 1024).toString("utf8").trim(),
      ssl: false,
      max: 2,
      connectionTimeoutMillis: 5000,
      query_timeout: 6000,
    });
  const runtimePool = poolFor(fixture.runtime),
    observerPool = poolFor(fixture.observer);
  assert.notEqual(fixture.runtime.user, fixture.observer.user);
  runtimePool.on("error", () => {});
  observerPool.on("error", () => {});
  const ledger = new PostgresLedger(runtimePool, {
      deploymentEpoch: fixture.deployment_epoch,
    }),
    observer = new PostgresLedger(observerPool, {
      deploymentEpoch: fixture.deployment_epoch,
    });
  const providerJobs = new Map(),
    states = new Map(
      CASES.map((name) => [
        name,
        {
          hold: ["main", "revocation", "reclaim"].includes(name),
          inserts: 0,
          metadata: 0,
          results: 0,
        },
      ]),
    );
  const groupCase = new Map(
    fixture.cases.map((c) => [c.source_attempt.session_id, c.name]),
  );
  const { BigQuery } = require("@google-cloud/bigquery");
  const originalRequest = BigQuery.prototype.request;
  function provider(context, req, callback) {
    try {
      const uri = req.uri || "";
      if (req.method === "POST" && uri === "/jobs") {
        assert.ok(context);
        const name = groupCase.get(context.session_id);
        assert.ok(name);
        const state = states.get(name),
          wire = structuredClone(req.json);
        assert.deepEqual(Object.keys(wire).sort(), [
          "configuration",
          "jobReference",
        ]);
        assert.deepEqual(Object.keys(wire.configuration).sort(), [
          "jobTimeoutMs",
          "query",
        ]);
        const q = wire.configuration.query;
        assert.deepEqual(Object.keys(q).sort(), [
          "maximumBytesBilled",
          "parameterMode",
          "query",
          "queryParameters",
          "useLegacySql",
          "useQueryCache",
        ]);
        assert.equal(
          q.maximumBytesBilled,
          String(context.max_bytes_per_submission),
        );
        assert.equal(q.useLegacySql, false);
        assert.equal(q.useQueryCache, false);
        assert.equal(q.parameterMode, "positional");
        assert.equal(wire.jobReference.projectId, profile.billing_project);
        assert.equal(wire.jobReference.location, profile.location);
        assert.match(wire.jobReference.jobId, /^sp_[a-f0-9]{64}$/);
        assert.ok(
          Number(wire.configuration.jobTimeoutMs) > 0 &&
            Number(wire.configuration.jobTimeoutMs) <= profile.job_timeout_ms,
        );
        assert.equal(
          providerJobs.has(wire.jobReference.jobId),
          false,
          "Provider ID cannot be inserted twice",
        );
        state.inserts++;
        const job = {
          wire,
          name,
          requestId: context.session_id + ".g" + context.generation,
          unknown: name === "unknown",
        };
        providerJobs.set(wire.jobReference.jobId, job);
        if (job.unknown)
          return setImmediate(() =>
            callback(
              Object.assign(new Error("Synthetic lost submission response"), {
                code: 500,
              }),
            ),
          );
        return setImmediate(() => callback(null, metadata(job)));
      }
      const match = /^\/?(jobs|queries)\/(sp_[a-f0-9]{64})$/.exec(uri);
      assert.ok(match);
      const job = providerJobs.get(match[2]);
      if (!job || job.unknown) {
        if (job && match[1] === "jobs") states.get(job.name).metadata++;
        return setImmediate(() =>
          callback(
            Object.assign(new Error("Synthetic unknown source job"), {
              code: 404,
            }),
          ),
        );
      }
      const state = states.get(job.name);
      if (match[1] === "jobs") {
        state.metadata++;
        return setImmediate(() => callback(null, metadata(job)));
      }
      state.results++;
      assert.equal(state.hold, false);
      const q = job.wire.configuration.query,
        refresh = q.query.trim() === "SELECT 'stable' AS refresh_key";
      let fields, rows;
      if (refresh) {
        fields = [{ name: "refresh_key", type: "STRING" }];
        rows = [{ f: [{ v: "stable" }] }];
      } else {
        fields = [
          { name: "usage__account", type: "STRING" },
          { name: "usage__day_day", type: "STRING" },
          { name: "usage__events", type: "STRING" },
        ];
        rows = cases.get(job.name).expected_rows.map((row) => ({
          f: [
            { v: row["Usage.account"] },
            { v: row["Usage.day.day"] ?? row["Usage.day"] },
            { v: row["Usage.events"] },
          ],
        }));
      }
      return setImmediate(() =>
        callback(null, {
          jobReference: job.wire.jobReference,
          jobComplete: true,
          totalRows: String(rows.length),
          schema: { fields },
          rows,
        }),
      );
    } catch (_) {
      setImmediate(() =>
        callback(new Error("Synthetic provider contract mismatch")),
      );
    }
  }
  function metadata(job) {
    const state = states.get(job.name),
      wire = job.wire,
      q = structuredClone(wire.configuration.query);
    q.parameterMode = "POSITIONAL";
    Object.assign(q, {
      priority: "INTERACTIVE",
      createSession: false,
      allowLargeResults: false,
      flattenResults: true,
      destinationTable: {
        projectId: profile.billing_project,
        datasetId: "_anonymous_fixture",
        tableId: "anonymous_" + wire.jobReference.jobId.slice(3, 35),
      },
      createDisposition: "CREATE_IF_NEEDED",
      writeDisposition: "WRITE_EMPTY",
    });
    return {
      jobReference: wire.jobReference,
      user_email: profile.auth.expected_principal,
      configuration: {
        jobType: "QUERY",
        dryRun: false,
        jobTimeoutMs: wire.configuration.jobTimeoutMs,
        query: q,
      },
      status: { state: state.hold ? "RUNNING" : "DONE" },
      statistics: state.hold ? {} : { query: { statementType: "SELECT" } },
    };
  }
  BigQuery.prototype.request = function (req, cb) {
    return provider(null, req, cb);
  };
  let server, url;
  const finishedControls = new Set();
  async function start() {
    const options = createCubeOptions({
        profile,
        bundle,
        signingKey: key,
        ledger,
      }),
      factory = options.driverFactory;
    options.driverFactory = (ctx) => {
      const driver = factory(ctx);
      driver.bigquery.request = (req, cb) =>
        provider(ctx.securityContext.source_attempt, req, cb);
      return driver;
    };
    server = createServer(options);
    const started = await server.listen();
    guard.allowLoopbackPort(started.server.address().port);
    url = "http://127.0.0.1:" + started.server.address().port;
  }
  async function stop() {
    if (server) {
      guard.revokeLoopbackPort(Number(new URL(url).port));
      await server.shutdown("SIGTERM");
      server = null;
      require("@cubejs-backend/native").resetLogger("error");
    }
  }
  function token(c) {
    const now = Math.floor(Date.now() / 1000);
    return jwt.sign(
      {
        ...c.claims,
        iat: now,
        exp: Math.min(
          now + 60,
          Math.ceil(Date.parse(c.source_attempt.deadline_at) / 1000),
        ),
      },
      key,
      { algorithm: "HS256" },
    );
  }
  async function load(c) {
    const response = await fetch(url + "/cubejs-api/v1/load", {
      method: "POST",
      headers: {
        Authorization: "Bearer " + token(c),
        "X-Request-Id":
          c.source_attempt.session_id + ".g" + c.source_attempt.generation,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ query: c.cube_query, cache: "no-cache" }),
    });
    return { status: response.status, body: await response.json() };
  }
  async function poll(c) {
    const deadline = Math.min(
      Date.now() + 15000,
      Date.parse(c.source_attempt.deadline_at),
    );
    while (Date.now() < deadline) {
      const out = await load(c);
      if (out.body.error === "Continue wait") {
        await new Promise((r) => setTimeout(r, 100));
        continue;
      }
      assert.equal(out.status, 200);
      assert.ok(Array.isArray(out.body.data));
      return out.body.data;
    }
    throw Error("Fixture load did not complete");
  }
  async function control(operation) {
    assert.ok(OPERATIONS.includes(operation));
    assert.equal(finishedControls.has(operation), false);
    finishedControls.add(operation);
    process.stdout.write(
      "GATEWAY_SOURCE_CONTROL " + JSON.stringify({ operation }) + "\n",
    );
    const filename = path.join(
        path.dirname(fixtureFile),
        "source-runtime-control-" + operation + ".json",
      ),
      deadline = Date.now() + 20000;
    while (Date.now() < deadline) {
      try {
        const data = R.regularFile(filename, 65536),
          ack = JSON.parse(data.toString("utf8"));
        assert.equal(ack.schema_version, 1);
        assert.equal(ack.operation, operation);
        return ack;
      } catch (error) {
        if (fs.existsSync(filename)) throw error;
      }
      await new Promise((r) => setTimeout(r, 25));
    }
    throw Error("Fixture control acknowledgement did not arrive");
  }
  async function waitStatus(c, predicate) {
    const deadline = Date.now() + 8000;
    while (Date.now() < deadline) {
      const status = await ledger.status(c.source_attempt.session_id);
      if (predicate(status)) return status;
      await new Promise((r) => setTimeout(r, 50));
    }
    throw Error("Fixture source status did not converge");
  }
  async function waitProvider(name) {
    const deadline = Date.now() + 8000;
    while (states.get(name).metadata === 0 && Date.now() < deadline)
      await new Promise((resolve) => setTimeout(resolve, 25));
    assert.ok(states.get(name).metadata > 0);
    assert.equal(states.get(name).inserts, 1);
  }
  function exactRows(actual, expected) {
    assert.equal(actual.length, expected.length);
    for (let i = 0; i < expected.length; i++) {
      assert.deepEqual(
        Object.keys(actual[i]).sort(),
        Object.keys(expected[i]).sort(),
      );
      for (const [k, v] of Object.entries(expected[i]))
        assert.equal(actual[i][k], v);
    }
  }
  try {
    checkpoint = "source-startup";
    await start();
    assert.equal((await fetch(url + "/livez")).status, 200);
    assert.equal((await fetch(url + "/cubejs-api/v1/meta")).status, 403);
    assert.equal(providerJobs.size, 0);
    checkpoint = "source-main";
    const main = cases.get("main");
    const first = await load(main);
    assert.equal(first.body.error, "Continue wait");
    await waitProvider("main");
    states.get("main").hold = false;
    exactRows(await poll(main), main.expected_rows);
    assert.equal(states.get("main").inserts, 2);
    let status = await ledger.status(main.source_attempt.session_id);
    assert.equal(status.admitted_count, 2);
    assert.equal(status.unresolved_count, 0);
    assert.equal(status.group_settled, false);
    const originalIds = [...providerJobs.entries()]
      .filter(([, j]) => j.name === "main")
      .map(([id]) => id)
      .sort();
    exactRows(await poll(main), main.expected_rows);
    assert.equal(states.get("main").inserts, 2);
    server.core.orchestratorStorage.clear();
    exactRows(await poll(main), main.expected_rows);
    assert.equal(states.get("main").inserts, 2);
    await stop();
    await start();
    exactRows(await poll(main), main.expected_rows);
    assert.deepEqual(
      [...providerJobs.entries()]
        .filter(([, j]) => j.name === "main")
        .map(([id]) => id)
        .sort(),
      originalIds,
    );
    assert.equal(states.get("main").inserts, 2);
    checkpoint = "source-revocation";
    const revoked = cases.get("revocation");
    assert.equal((await load(revoked)).body.error, "Continue wait");
    await waitProvider("revocation");
    await control("revoke_queued");
    states.get("revocation").hold = false;
    assert.equal((await load(revoked)).status, 403);
    status = await waitStatus(revoked, (s) => s.sealed);
    assert.ok(status.unresolved_count > 0);
    assert.equal(states.get("revocation").results, 0);
    const observed = await reconcileOnce({
      ledger: observer,
      profile,
      observerAuth: {
        mode: "gke-workload-identity",
        expected_principal: "observer@fixture-project.iam.gserviceaccount.com",
      },
    });
    assert.ok(observed.observed > 0);
    status = await ledger.status(revoked.source_attempt.session_id);
    assert.equal(status.terminal, true);
    assert.equal(states.get("revocation").results, 0);
    checkpoint = "source-reclaim";
    let reclaimed = cases.get("reclaim");
    assert.equal((await load(reclaimed)).body.error, "Continue wait");
    await waitProvider("reclaim");
    const before = [...providerJobs.entries()]
      .filter(([, j]) => j.name === "reclaim")
      .map(([id, j]) => [id, j.wire.configuration.jobTimeoutMs]);
    const ack = await control("reclaim");
    assert.equal(
      ack.source_attempt.session_id,
      reclaimed.source_attempt.session_id,
    );
    assert.equal(
      ack.source_attempt.generation,
      reclaimed.source_attempt.generation + 1,
    );
    reclaimed = C.immutable({
      ...reclaimed,
      source_attempt: ack.source_attempt,
      claims: ack.claims,
    });
    cases.set("reclaim", reclaimed);
    states.get("reclaim").hold = false;
    exactRows(await poll(reclaimed), reclaimed.expected_rows);
    assert.equal(states.get("reclaim").inserts, 2);
    for (const [id, timeout] of before)
      assert.equal(
        providerJobs.get(id).wire.configuration.jobTimeoutMs,
        timeout,
      );
    status = await ledger.status(reclaimed.source_attempt.session_id);
    assert.equal(status.sealed, false);
    assert.equal(status.admitted_count, 2);
    assert.equal(status.unresolved_count, 0);
    await control("close_main");
    status = await ledger.status(main.source_attempt.session_id);
    assert.equal(status.group_settled, true);
    assert.equal(status.sealed, true);
    checkpoint = "source-zero";
    const zero = cases.get("zero_job");
    await control("close_zero");
    status = await ledger.status(zero.source_attempt.session_id);
    assert.equal(status.admitted_count, 0);
    assert.equal(status.group_settled, true);
    assert.equal(states.get("zero_job").inserts, 0);
    checkpoint = "source-unknown";
    const unknown = cases.get("unknown");
    let outcome = await load(unknown);
    const unknownDeadline = Date.now() + 15000;
    while (
      outcome.body.error === "Continue wait" &&
      Date.now() < unknownDeadline
    ) {
      await new Promise((r) => setTimeout(r, 50));
      outcome = await load(unknown);
    }
    assert.notEqual(outcome.body.error, "Continue wait");
    assert.ok(outcome.body.error);
    assert.equal(states.get("unknown").inserts, 1);
    status = await waitStatus(unknown, (s) => s.sealed);
    assert.equal(status.admitted_count, 1);
    assert.equal(status.unresolved_count, 1);
    assert.equal(status.group_settled, false);
    await stop();
    await start();
    const repeated = await load(unknown);
    assert.ok(repeated.body.error);
    assert.equal(states.get("unknown").inserts, 1);
    status = await ledger.status(unknown.source_attempt.session_id);
    assert.equal(status.unresolved_count, 1);
    assert.equal(status.group_settled, false);
    const unknownLookups = states.get("unknown").metadata;
    const unknownObservation = await reconcileOnce({
      ledger: observer,
      profile,
      observerAuth: {
        mode: "gke-workload-identity",
        expected_principal: "observer@fixture-project.iam.gserviceaccount.com",
      },
    });
    assert.equal(unknownObservation.claimed, 1, "unknown claimed count");
    assert.ok(unknownObservation.unknown > 0);
    assert.ok(states.get("unknown").metadata > unknownLookups);
    assert.equal(states.get("unknown").inserts, 1);
    assert.equal(states.get("unknown").results, 0);
    status = await ledger.status(unknown.source_attempt.session_id);
    assert.equal(status.unresolved_count, 1);
    assert.equal(status.group_settled, false);
    checkpoint = "source-cleanup";
    await stop();
    guard.assertClean();
    assert.deepEqual([...finishedControls].sort(), [...OPERATIONS].sort());
    return {
      schema_version: 1,
      status: "passed",
      operations: OPERATIONS,
      cases: CASES,
      checks: CHECKS,
      provider: "fake",
      registry: "postgresql",
    };
  } finally {
    await stop();
    BigQuery.prototype.request = originalRequest;
    await ledger.close();
    await observer.close();
    guard.assertClean();
  }
}
run()
  .then((result) =>
    process.stdout.write(
      "GATEWAY_SOURCE_RUNTIME_RESULT " + JSON.stringify(result) + "\n",
    ),
  )
  .catch(() => {
    process.stdout.write(
      "GATEWAY_SOURCE_RUNTIME_RESULT " +
        JSON.stringify({ schema_version: 1, status: "failed", checkpoint }) +
        "\n",
    );
    process.exitCode = 1;
  });
