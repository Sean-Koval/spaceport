"use strict";
const { performance } = require("node:perf_hooks");
const C = require("./contract.cjs");
const R = require("./config.cjs");
const { explicitAuth, bounded } = require("./driver.cjs");
const { PostgresLedger } = require("./postgres-ledger.cjs");
class MetadataClient {
  #source;
  #auth;
  #client;
  constructor(profile, observerAuth) {
    this.#source = C.immutable(R.validateProfile(profile));
    C.fields(observerAuth, ["mode", "expected_principal"]);
    if (observerAuth.mode !== "gke-workload-identity") C.fail();
    C.text(observerAuth.expected_principal, C.PRINCIPAL, 254);
    if(observerAuth.expected_principal===profile.auth.expected_principal)C.fail();
    this.#auth = C.immutable(observerAuth);
  }
  async read(job) {
    C.validateEnvironment();
    C.text(job.job_id, /^sp_[a-f0-9]{64}$/);
    if (
      job.project_id !== this.#source.billing_project ||
      job.location !== this.#source.location
    )
      C.fail();
    if (!this.#client) {
      const { BigQuery } = require("@google-cloud/bigquery");
      this.#client = new BigQuery({
        projectId: this.#source.billing_project,
        location: this.#source.location,
        authClient: explicitAuth(this.#auth.expected_principal),
        apiEndpoint: "https://bigquery.googleapis.com",
        autoRetry: false,
        maxRetries: 0,
        timeout: 5000,
      });
    }
    const [metadata] = await bounded(
      () =>
        this.#client.job(job.job_id, { location: job.location }).getMetadata(),
      Date.now() + 5000,
      performance.now() + 5000,
    );
    return metadata;
  }
}
async function reconcileOnce({ ledger, profile, observerAuth }) {
  if (!(ledger instanceof PostgresLedger)) C.fail();
  profile = C.immutable(R.validateProfile(profile));
  observerAuth = C.immutable(observerAuth);
  await ledger.expireGroups(32);
  const items = await ledger.claimObservations(8);
  if (
    !Array.isArray(items) ||
    items.length > 8 ||
    new Set(items.map((i) => i?.job?.job_id)).size !== items.length
  )
    C.fail();
  const result = { claimed: items.length, observed: 0, unknown: 0 };
  let index = 0;
  const worker = async () => {
    while (index < items.length) {
      const item = items[index++];
      try {
        C.fields(item, ["job", "observation_token", "source"]);
        C.text(item.observation_token, /^[A-Za-z0-9_-]{1,256}$/);
        const source = R.validateProfile(item.source),
          job = C.immutable(item.job);
        if (
          source.billing_project !== profile.billing_project ||
          source.location !== profile.location ||
          job.project_id !== source.billing_project ||
          job.location !== source.location ||
          job.principal !== source.auth.expected_principal
        )
          C.fail();
        const client = new MetadataClient(source, observerAuth);
        // This is observation scheduling authority only. Never check/renew data
        // authority here, and never obtain a submit permit or getQueryResults handle.
        const current = await ledger.checkObservation(
          job.job_id,
          item.observation_token,
        );
        if (
          current.intent_digest !== job.intent_digest ||
          current.job_id !== job.job_id
        )
          C.fail();
        const metadata = await client.read(job),
          evidence = C.validateMetadata(metadata, job, R.driverSource(source));
        await ledger.observeJob(job.job_id, evidence);
        await ledger.settleGroup(job.group_id);
        result.observed++;
      } catch (_) {
        result.unknown++; /* Persisted claim backoff and held obligations remain. */
      }
    }
  };
  await Promise.all([worker(), worker()]);
  return result;
}
async function main() {
  const { config, ledger } = R.loadRuntime("observer");
  let stopping = false;
  process.once("SIGINT", () => {
    stopping = true;
  });
  process.once("SIGTERM", () => {
    stopping = true;
  });
  try {
    while (!stopping) {
      try {
        await reconcileOnce({
          ledger,
          profile: config.profile,
          observerAuth: config.observer_auth,
        });
      } catch {
        /* No raw DB/provider diagnostics. */
      }
      if (!stopping) await new Promise((resolve) => setTimeout(resolve, 1000));
    }
  } finally {
    await ledger.close();
  }
}
if (require.main === module)
  main().catch(() => {
    process.stderr.write("BigQuery observer failed.\n");
    process.exitCode = 1;
  });
module.exports = { MetadataClient, reconcileOnce, main };
