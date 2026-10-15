"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const file = "../../cube/bigquery-runtime/postgres-ledger.cjs";
function transaction({ failure } = {}) {
  const calls = [];
  const client = {
    async query(sql, params) {
      calls.push({ sql, params });
      if (sql === failure) throw new Error("sensitive backend diagnostic");
      if (sql.includes("source_check_epoch"))
        return { rows: [{ value: true }] };
      return {
        rows: [{ value: { submit_once: true, job: { job_id: "test" } } }],
      };
    },
    release(discard) {
      calls.push({ release: discard });
    },
  };
  return { calls, pool: { connect: async () => client, end: async () => {} } };
}
test("SQL adapter exposes only fixed mapped function names and confirms COMMIT before returning permits", async () => {
  assert.equal(
    require("node:fs").existsSync(
      require("node:path").resolve(__dirname, file),
    ),
    true,
  );
  const { PostgresLedger } = require(file);
  const tx = transaction();
  const ledger = new PostgresLedger(tx.pool, {
    deploymentEpoch: "fixture-epoch",
  });
  const context = { session_id: "exact" };
  const intent = { query_digest: "a" };
  assert.equal(
    (await ledger.admitJob(context, "key", intent)).submit_once,
    true,
  );
  assert.deepEqual(tx.calls.map((c) => c.sql).filter(Boolean), [
    "BEGIN",
    "SET LOCAL statement_timeout = 5000",
    "SET LOCAL synchronous_commit = on",
    "SELECT gateway.source_check_epoch($1::text) AS value",
    "SELECT gateway.source_bq_admit($1::jsonb,$2::text,$3::jsonb) AS value",
    "COMMIT",
  ]);
  assert.deepEqual(tx.calls[4].params, [
    JSON.stringify(context),
    "key",
    JSON.stringify(intent),
  ]);
  assert.equal(typeof ledger.register, "undefined");
  assert.equal(typeof ledger.query, "undefined");
});
test("ambiguous COMMIT never hands out an admission permit or retries a mutation", async () => {
  const { PostgresLedger } = require(file);
  const tx = transaction({ failure: "COMMIT" });
  const ledger = new PostgresLedger(tx.pool, {
    deploymentEpoch: "fixture-epoch",
  });
  await assert.rejects(
    ledger.admitJob({}, "key", {}),
    (e) => e.message === "BigQuery source is unavailable.",
  );
  assert.equal(
    tx.calls.filter((c) => c.sql?.includes("source_bq_admit")).length,
    1,
  );
  assert.equal(tx.calls.at(-1).release, true);
});
test("normal and observation authority use distinct fixed SQL functions", async () => {
  const { PostgresLedger } = require(file);
  const tx = transaction();
  const ledger = new PostgresLedger(tx.pool, {
    deploymentEpoch: "fixture-epoch",
  });
  await ledger.checkAttempt({});
  await ledger.lookupJob({}, "key");
  await ledger.checkDataRpc({}, "job");
  await ledger.observeJob("job", { state: "done" });
  await ledger.claimObservations(8);
  await ledger.checkObservation("job", "token");
  await ledger.expireGroups(32);
  await ledger.settleGroup("group");
  const names = tx.calls
    .filter(
      (c) =>
        c.sql?.startsWith("SELECT") && !c.sql.includes("source_check_epoch"),
    )
    .map((c) => c.sql.split("(")[0]);
  assert.deepEqual(names, [
    "SELECT gateway.source_check_attempt",
    "SELECT gateway.source_bq_lookup",
    "SELECT gateway.source_bq_check_data",
    "SELECT gateway.source_bq_observe",
    "SELECT gateway.source_bq_reconcile_claim",
    "SELECT gateway.source_bq_check_observation",
    "SELECT gateway.source_expire_groups",
    "SELECT gateway.source_settle_group",
  ]);
  await assert.rejects(ledger.claimObservations(9));
  await assert.rejects(ledger.expireGroups(33));
});
test("runtime error sealing carries the exact attempt rather than an unguarded session ID", async () => {
  const { PostgresLedger } = require(file);
  const tx = transaction();
  const ledger = new PostgresLedger(tx.pool, {
    deploymentEpoch: "fixture-epoch",
  });
  assert.equal(typeof ledger.sealSession, "undefined");
  const context = {
    session_id: "exact-session",
    generation: 7,
    context_digest: "retained",
  };
  await ledger.sealAttempt(context, "runtime_error");
  const call = tx.calls.find((c) => c.sql?.includes("source_seal_attempt"));
  assert.deepEqual(JSON.parse(call.params[0]), context);
  assert.equal(call.params[1], "runtime_error");
});

test("failed durable-commit pin never invokes an authority or admission function", async () => {
  const { PostgresLedger } = require(file);
  const tx = transaction({ failure: "SET LOCAL synchronous_commit = on" });
  const ledger = new PostgresLedger(tx.pool, {
    deploymentEpoch: "fixture-epoch",
  });
  await assert.rejects(ledger.admitJob({}, "key", {}));
  assert.equal(
    tx.calls.some((c) => c.sql?.startsWith("SELECT")),
    false,
  );
  assert.equal(tx.calls.at(-1).release, true);
});
