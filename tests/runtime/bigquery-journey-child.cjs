"use strict";
// Test-only final-provider seam for the genuine Python service/Cube/PG journey.
// No owner/app login, no fake lifecycle, and no cloud transport or credentials.
const fs = require("node:fs"),
  path = require("node:path"),
  assert = require("node:assert/strict");
const READY = "GATEWAY_BQ_JOURNEY_READY ",
  RESULT = "GATEWAY_BQ_JOURNEY_RESULT ";
let closing = false,
  finished = false,
  failed = false,
  guard;
const servers = [],
  ledgers = [];
const counts = { inserts: 0, metadata: 0, results: 0, provider_errors: 0 };
async function close() {
  if (closing) return;
  closing = true;
  let success = true;
  for (const { server, port } of servers.reverse()) {
    guard.revokeLoopbackPort(port);
    try {
      await server.shutdown("SIGTERM");
    } catch {
      success = false;
    }
  }
  for (const ledger of ledgers)
    try {
      await ledger.close();
    } catch {
      success = false;
    }
  try {
    require("@cubejs-backend/native").resetLogger("error");
    guard.assertClean();
  } catch {
    success = false;
  }
  finished = true;
  process.stdout.write(
    RESULT +
      JSON.stringify({
        schema_version: 1,
        status:
          !failed && success && counts.provider_errors === 0
            ? "passed"
            : "failed",
        counts,
      }) +
      "\n",
  );
  if (failed || !success || counts.provider_errors) process.exitCode = 1;
}
async function main() {
  assert.equal(
    process.env.GATEWAY_SOURCE_TEST_AUTHORIZATION,
    "private-synthetic-source-only",
  );
  const filename = process.env.GATEWAY_BQ_JOURNEY_CONFIG_FILE;
  assert.ok(path.isAbsolute(filename || ""));
  const raw = fs.readFileSync(filename);
  assert.ok(raw.length <= 65536);
  const config = JSON.parse(raw);
  assert.equal(config.schema_version, 1);
  assert.deepEqual(Object.keys(config).sort(), [
    "deployment_epoch",
    "profiles",
    "registry",
    "schema_version",
  ]);
  assert.deepEqual(Object.keys(config.registry).sort(), [
    "database",
    "host",
    "port",
  ]);
  assert.ok(
    ["127.0.0.1", "localhost", "registry-db"].includes(config.registry.host),
  );
  assert.match(
    config.registry.database,
    /^(gateway_test_[a-f0-9]{16}|gateway_source_test)$/,
  );
  assert.ok(
    Number.isInteger(config.registry.port) &&
      config.registry.port > 0 &&
      config.registry.port <= 65535,
  );
  assert.equal(config.profiles.length, 2);
  guard = require("./bigquery-serving-guard.cjs").installGuard({
    registry: config.registry,
    loopbackListeners: true,
    allowedSecretFiles: config.profiles.flatMap((p) => [
      p.runtime.password_file,
      p.signing_key_file,
    ]),
  });
  const root =
    process.env.GATEWAY_BIGQUERY_RUNTIME_DIR ||
    path.resolve(__dirname, "../../cube/bigquery-runtime");
  const C = require(path.join(root, "contract.cjs")),
    R = require(path.join(root, "config.cjs"));
  const { PostgresLedger } = require(path.join(root, "postgres-ledger.cjs")),
    { createCubeOptions, createServer } = require(
      path.join(root, "server.cjs"),
    );
  const { Pool } = require("pg"),
    { BigQuery } = require("@google-cloud/bigquery");
  C.text(config.deployment_epoch, /^[A-Za-z0-9_-]{1,128}$/);
  const profiles = config.profiles.map((p) => {
    C.fields(p, ["source", "bundle", "runtime", "signing_key_file"]);
    C.fields(p.bundle, ["root", "digest"]);
    C.fields(p.runtime, ["user", "password_file"]);
    C.text(p.runtime.user, /^[a-z][a-z0-9_]{0,62}$/);
    const source = C.immutable(R.validateProfile(p.source)),
      bundle = R.loadBundle(p.bundle.root, p.bundle.digest);
    assert.equal(bundle.digest, source.model_bundle_digest);
    const signingKey = R.regularFile(p.signing_key_file, 1024)
      .toString("utf8")
      .trim();
    assert.match(signingKey, /^[A-Za-z0-9_-]{43}$/);
    const password = R.regularFile(p.runtime.password_file, 1024)
      .toString("utf8")
      .trim();
    assert.match(password, /^[A-Za-z0-9_-]{1,128}$/);
    const pool = new Pool({
      ...config.registry,
      user: p.runtime.user,
      password,
      ssl: false,
      max: 2,
      connectionTimeoutMillis: 5000,
      query_timeout: 6000,
    });
    pool.on("error", () => {});
    const ledger = new PostgresLedger(pool, {
      deploymentEpoch: config.deployment_epoch,
    });
    ledgers.push(ledger);
    return {
      source,
      bundle,
      signingKey,
      ledger,
      login: p.runtime.user,
      keyFile: p.signing_key_file,
    };
  });
  assert.deepEqual(
    profiles.map((p) => p.source.context),
    ["consumer", "fixture_test"],
  );
  for (const get of [
    (p) => p.source.auth.expected_principal,
    (p) => p.source.cube_endpoint,
    (p) => p.source.model_bundle_digest,
    (p) => p.login,
    (p) => p.keyFile,
    (p) => p.signingKey,
  ])
    assert.equal(new Set(profiles.map(get)).size, 2);
  const jobs = new Map();
  BigQuery.prototype.request = () => {
    counts.provider_errors++;
    throw Error("Ungated synthetic provider operation");
  };
  function metadata(job) {
    const q = structuredClone(job.wire.configuration.query);
    q.parameterMode = "POSITIONAL";
    return {
      jobReference: job.wire.jobReference,
      user_email: job.profile.auth.expected_principal,
      configuration: {
        jobTimeoutMs: job.wire.configuration.jobTimeoutMs,
        query: q,
      },
      status: { state: "DONE" },
      statistics: { query: { statementType: "SELECT" } },
    };
  }
  const data = [
    {
      day: "2026-10-05T00:00:00.000",
      account: "acct-a",
      events: "120",
      group: "core",
      tenant: "north",
    },
    {
      day: "2026-10-06T00:00:00.000",
      account: "acct-a",
      events: "180",
      group: "core",
      tenant: "north",
    },
    {
      day: "2026-10-05T00:00:00.000",
      account: "acct-b",
      events: "50",
      group: "special",
      tenant: "north",
    },
    {
      day: "2026-10-05T00:00:00.000",
      account: "acct-a",
      events: "999",
      group: "core",
      tenant: "south",
    },
    {
      day: "2026-10-05T00:00:00.000",
      account: "acct-a",
      events: "999",
      group: "forbidden",
      tenant: "north",
    },
  ];
  function rows(job) {
    const q = job.wire.configuration.query;
    if (q.query.trim() === "SELECT 'stable' AS refresh_key")
      return {
        fields: [{ name: "refresh_key", type: "STRING" }],
        rows: [{ f: [{ v: "stable" }] }],
      };
    for (const name of [
      "usage__account",
      "usage__day_day",
      "usage__usage_events",
    ])
      assert.ok(q.query.includes(name));
    assert.ok(q.query.includes("TIMESTAMP(DATETIME_TRUNC("));
    assert.ok(
      q.query.includes(
        "`" +
          job.profile.data_project +
          "." +
          job.profile.dataset +
          "." +
          job.profile.tables[0] +
          "`",
      ),
    );
    const values = q.queryParameters.map((p) => {
      assert.deepEqual(p.parameterType, { type: "STRING" });
      assert.deepEqual(Object.keys(p.parameterValue), ["value"]);
      return p.parameterValue.value;
    });
    const accepted = predicateRows(q.query, values, data);
    return {
      fields: [
        { name: "usage__account", type: "STRING" },
        { name: "usage__day_day", type: "TIMESTAMP" },
        { name: "usage__usage_events", type: "STRING" },
      ],
      rows: accepted.map((r) => ({
        f: [
          { v: r.account },
          { v: String(BigInt(Date.parse(r.day + "Z")) * 1000n) },
          { v: r.events },
        ],
      })),
    };
  }
  function request(profile, context, req, callback) {
    try {
      if (req.method === "POST" && req.uri === "/jobs") {
        const wire = structuredClone(req.json);
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
        assert.equal(jobs.has(wire.jobReference.jobId), false);
        const job = { profile, wire, session: context.session_id };
        jobs.set(wire.jobReference.jobId, job);
        counts.inserts++;
        // Validate the known synthetic query shape even when result rows are empty.
        rows(job);
        return setImmediate(() => callback(null, metadata(job)));
      }
      const match = readJobPath(req);
      const job = jobs.get(match[2]);
      assert.ok(job);
      assert.equal(job.profile.source_id, profile.source_id);
      assert.equal(job.session, context.session_id);
      if (match[1] === "jobs") {
        counts.metadata++;
        return setImmediate(() => callback(null, metadata(job)));
      }
      assert.equal(req.qs["formatOptions.useInt64Timestamp"], true);
      counts.results++;
      const result = rows(job);
      return setImmediate(() =>
        callback(null, {
          jobReference: job.wire.jobReference,
          jobComplete: true,
          totalRows: String(result.rows.length),
          schema: { fields: result.fields },
          rows: result.rows,
        }),
      );
    } catch {
      counts.provider_errors++;
      setImmediate(() =>
        callback(Error("Synthetic provider contract mismatch")),
      );
    }
  }
  const listeners = [];
  for (const p of profiles) {
    // Production runs one server per process. This fixture hosts two independent
    // contexts and drops only the maintained package's first, internally owned
    // preaggregation pin before normal validation of the second server.
    if (servers.length) resetOwnedCubePin(servers.length === 1);
    const options = createCubeOptions({
        profile: p.source,
        bundle: p.bundle,
        signingKey: p.signingKey,
        ledger: p.ledger,
      }),
      factory = options.driverFactory;
    options.driverFactory = (ctx) => {
      const driver = factory(ctx);
      driver.bigquery.request = (req, callback) =>
        request(p.source, ctx.securityContext.source_attempt, req, callback);
      return driver;
    };
    process.env.PORT = "0";
    const server = createServer(options);
    const owned = { server, port: null };
    servers.push(owned);
    const started = await server.listen();
    // Binding an explicit host completes on the next turn in Node. Cube's
    // numeric-port listen returns the Server rather than a listening promise.
    if (!started.server.listening)
      await new Promise((resolve, reject) => {
        started.server.once("listening", resolve);
        started.server.once("error", reject);
      });
    const port = started.server.address().port;
    owned.port = port;
    assert.equal(started.server.address().address, "127.0.0.1");
    guard.allowLoopbackPort(port);
    listeners.push({
      context: p.source.context,
      origin: p.source.cube_endpoint,
      port,
    });
  }
  guard.assertClean();
  assert.equal(counts.inserts, 0);
  process.once("SIGTERM", () => {
    close().catch(() => {
      process.exitCode = 1;
    });
  });
  process.once("SIGINT", () => {
    close().catch(() => {
      process.exitCode = 1;
    });
  });
  process.stdout.write(
    READY + JSON.stringify({ schema_version: 1, listeners }) + "\n",
  );
}
// Deliberately only the reviewed weekly template, not a general SQL evaluator.
// Every actual '?' position must belong to a recognized predicate. Missing or
// misbound scope/date/account terms never receive assumed scoped fixture rows.
function predicateRows(sql, values, data) {
  assert.equal(typeof sql, "string");
  assert.ok(Array.isArray(values) && values.length >= 4 && values.length <= 20);
  const where =
    /\bWHERE\b([\s\S]*?)(?=\bGROUP BY\b|\bORDER BY\b|\bLIMIT\b|$)/.exec(sql);
  assert.ok(where);
  const offset = where.index + "WHERE".length;
  const predicates = [],
    fields = new Set();
  let used = 0;
  const atom =
    /`usage`\.(tenant_id|access_group|account_id|event_date)\s*(>=|<|=|IN)\s*(TIMESTAMP\(\?\)|\?|\(\s*\?(?:\s*,\s*\?)*\s*\))/g;
  const expression = where[1].replace(
    atom,
    (text, field, operator, rhs, index) => {
      const first = (sql.slice(0, offset + index).match(/\?/g) || []).length;
      const count = (text.match(/\?/g) || []).length;
      assert.equal(first, used);
      used += count;
      const bound = values.slice(first, first + count);
      assert.equal(bound.length, count);
      fields.add(field + ":" + operator);
      if (field === "event_date") {
        assert.ok([">=", "<"].includes(operator));
        assert.equal(rhs, "TIMESTAMP(?)");
        assert.equal(count, 1);
        const day = operator === ">=" ? "05" : "12";
        assert.equal(bound[0], "2026-10-" + day + "T00:00:00.000000Z");
        const date = "2026-10-" + day + "T00:00:00.000";
        predicates.push((row) =>
          operator === ">=" ? row.day >= date : row.day < date,
        );
      } else {
        assert.ok(
          operator === "=" || (field === "access_group" && operator === "IN"),
        );
        assert.ok(operator === "IN" ? /^\(/.test(rhs) : rhs === "?");
        const valid =
          field === "tenant_id"
            ? ["north"]
            : field === "access_group"
              ? ["core", "special"]
              : ["acct-a", "acct-b"];
        assert.ok(
          bound.every(
            (value) => typeof value === "string" && valid.includes(value),
          ),
        );
        const key = {
          tenant_id: "tenant",
          access_group: "group",
          account_id: "account",
        }[field];
        predicates.push((row) => bound.includes(row[key]));
      }
      return "P";
    },
  );
  assert.equal(used, values.length);
  assert.equal((sql.match(/\?/g) || []).length, used);
  assert.ok(
    fields.has("tenant_id:=") &&
      (fields.has("access_group:=") || fields.has("access_group:IN")) &&
      fields.has("event_date:>=") &&
      fields.has("event_date:<"),
  );
  assert.equal(expression.replace(/\bAND\b|P|[()\s]/g, ""), "");
  const tokens = expression.match(/AND|P|[()]/g) || [];
  let cursor = 0;
  function factor(depth) {
    assert.ok(depth < 32);
    if (tokens[cursor] === "P") {
      cursor++;
      return;
    }
    assert.equal(tokens[cursor++], "(");
    conjunction(depth + 1);
    assert.equal(tokens[cursor++], ")");
  }
  function conjunction(depth) {
    factor(depth);
    while (tokens[cursor] === "AND") {
      cursor++;
      factor(depth);
    }
  }
  conjunction(0);
  assert.equal(cursor, tokens.length);
  return data.filter((row) => predicates.every((predicate) => predicate(row)));
}
function resetOwnedCubePin(hasFirstServer) {
  assert.equal(hasFirstServer, true);
  const shared = require("@cubejs-backend/shared");
  assert.equal(shared.userPreAggregationsSchema(), undefined);
  assert.equal(typeof process.env.CUBEJS_PRE_AGGREGATIONS_SCHEMA, "string");
  shared.dropPreAggregationsSchemaPin();
  assert.equal(process.env.CUBEJS_PRE_AGGREGATIONS_SCHEMA, undefined);
}
function readJobPath(req) {
  // The maintained SDK omits method for these reads; its transport defaults to
  // GET. Null/empty/other methods and all other URI forms remain forbidden.
  assert.ok(req.method === undefined || req.method === "GET");
  assert.equal(typeof req.uri, "string");
  const match = /^(jobs|\/queries)\/(sp_[a-f0-9]{64})$/.exec(req.uri);
  assert.ok(match);
  return match;
}
module.exports = { resetOwnedCubePin, readJobPath, predicateRows };
if (require.main === module)
  main().catch(async () => {
    failed = true;
    if (guard) await close();
    if (!finished)
      process.stdout.write(
        RESULT +
          JSON.stringify({ schema_version: 1, status: "failed", counts }) +
          "\n",
      );
    process.exitCode = 1;
  });
