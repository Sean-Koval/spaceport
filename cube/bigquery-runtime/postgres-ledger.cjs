"use strict";
const C = require("./contract.cjs");
// No in-memory authority or fallback. Every method is one bounded transaction.
// Private pool: callers cannot replace SQL, enumerate rows, or retain a transaction
// across a provider call through this API. SQL grants remain the security boundary.
class PostgresLedger {
  #pool;
  #epoch;
  constructor(pool, { deploymentEpoch } = {}) {
    C.text(deploymentEpoch, /^[A-Za-z0-9_-]{1,128}$/);
    this.#epoch = deploymentEpoch;
    if (!pool || typeof pool.connect !== "function") C.fail();
    this.#pool = pool;
  }
  async #call(sql, params) {
    let client,
      failed = false;
    try {
      client = await this.#pool.connect();
      await client.query("BEGIN");
      await client.query("SET LOCAL statement_timeout = 5000");
      await client.query("SET LOCAL synchronous_commit = on");
      const epoch = await client.query(
        "SELECT gateway.source_check_epoch($1::text) AS value",
        [this.#epoch],
      );
      if (epoch.rows.length !== 1 || epoch.rows[0].value !== true) C.fail();
      const result = await client.query(sql, params);
      if (result.rows.length !== 1 || !Object.hasOwn(result.rows[0], "value"))
        C.fail();
      // Never return submit_once until COMMIT was acknowledged. An interrupted
      // COMMIT yields only a safe failure; a later lookup has no submit permit.
      await client.query("COMMIT");
      return result.rows[0].value;
    } catch (_) {
      failed = true;
      if (client) {
        try {
          await client.query("ROLLBACK");
        } catch {}
      }
      C.fail();
    } finally {
      if (client) client.release(failed);
    }
  }
  checkAttempt(context) {
    return this.#call(
      "SELECT gateway.source_check_attempt($1::jsonb) AS value",
      [C.canonical(context)],
    );
  }
  lookupJob(context, key) {
    return this.#call(
      "SELECT gateway.source_bq_lookup($1::jsonb,$2::text) AS value",
      [C.canonical(context), key],
    );
  }
  admitJob(context, key, intent) {
    return this.#call(
      "SELECT gateway.source_bq_admit($1::jsonb,$2::text,$3::jsonb) AS value",
      [C.canonical(context), key, C.canonical(intent)],
    );
  }
  checkDataRpc(context, jobId) {
    return this.#call(
      "SELECT gateway.source_bq_check_data($1::jsonb,$2::text) AS value",
      [C.canonical(context), jobId],
    );
  }
  observeJob(jobId, evidence) {
    return this.#call(
      "SELECT gateway.source_bq_observe($1::text,$2::jsonb) AS value",
      [jobId, C.canonical(evidence)],
    );
  }
  async claimObservations(maxJobs = 8) {
    C.integer(maxJobs, 1, 8);
    return this.#call(
      "SELECT gateway.source_bq_reconcile_claim($1::integer) AS value",
      [maxJobs],
    );
  }
  checkObservation(jobId, token) {
    return this.#call(
      "SELECT gateway.source_bq_check_observation($1::text,$2::text) AS value",
      [jobId, token],
    );
  }
  async expireGroups(maxGroups = 32) {
    C.integer(maxGroups, 1, 32);
    return this.#call(
      "SELECT gateway.source_expire_groups($1::integer) AS value",
      [maxGroups],
    );
  }
  sealAttempt(context, reason) {
    return this.#call(
      "SELECT gateway.source_seal_attempt($1::jsonb,$2::text) AS value",
      [C.canonical(context), reason],
    );
  }
  status(sessionId) {
    return this.#call("SELECT gateway.source_status($1::uuid) AS value", [
      sessionId,
    ]);
  }
  settleGroup(groupId) {
    return this.#call("SELECT gateway.source_settle_group($1::uuid) AS value", [
      groupId,
    ]);
  }
  async close() {
    await this.#pool.end();
  }
}
module.exports = { PostgresLedger };
