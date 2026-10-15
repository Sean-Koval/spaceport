"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const cp = require("node:child_process");
const {
  AdmissionLedger,
  initializeLedger,
} = require("../../cube/bigquery/admission-ledger.cjs");
const {
  canonical,
  digest,
} = require("../../cube/bigquery/runtime-contract.cjs");
const identity = {
  deploymentId: "fixture-deployment",
  ledgerId: "8e2f0371-447a-4c0b-b5a3-7769efeff389",
};
const limits = {
  maxJournalBytes: 262144,
  maxRecordBytes: 8192,
  maxExecutions: 8,
  maxJobsPerExecution: 8,
};
const permit = (overrides = {}) => ({
  executionId: "0dd0b6bb-6541-41be-80e9-ecb69a41ee6f",
  generation: 1,
  scopeDigest: "a".repeat(64),
  queryDigest: "b".repeat(64),
  bindingDigest: "c".repeat(64),
  bundleDigest: "d".repeat(64),
  sourceDigest: "e".repeat(64),
  deadlineMs: 200000,
  maxJobs: 2,
  perJobBytes: "100",
  totalBytes: "200",
  ...overrides,
});
const config = (overrides = {}) => ({
  projectId: "fixture-project",
  location: "US",
  queryHash: digest("SELECT ?"),
  parametersHash: digest(["north"]),
  maximumBytesBilled: "100",
  jobTimeoutMs: 25000,
  ...overrides,
});
function fixture(t, settings = {}) {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "bq-ledger-"));
  fs.chmodSync(parent, 0o700);
  const directory = path.join(parent, "state");
  initializeLedger(directory, {
    ...identity,
    limits: { ...limits, ...settings },
  });
  const ledger = new AdmissionLedger(directory, identity);
  t.after(() => {
    try {
      ledger.close();
    } catch {}
    fs.rmSync(parent, { recursive: true, force: true });
  });
  return { parent, directory, ledger };
}
const reserve = (ledger, key = "1".repeat(64), cfg = config(), now = 100000) =>
  ledger.admit(permit().executionId, 1, key, cfg, now);

test("durably reserves aggregate bytes and reuses first admitted timeout/config", (t) => {
  const { directory, ledger } = fixture(t);
  ledger.register(permit(), 100000);
  const first = reserve(ledger);
  const same = reserve(
    ledger,
    "1".repeat(64),
    config({ jobTimeoutMs: 1 }),
    150000,
  );
  assert.equal(first.submit, true);
  assert.equal(same.submit, false);
  assert.deepEqual(same.job, first.job);
  assert.equal(same.job.config.jobTimeoutMs, 25000);
  reserve(ledger, "2".repeat(64));
  assert.throws(() => reserve(ledger, "3".repeat(64)), /budget/);
  ledger.close();
  const reopened = new AdmissionLedger(directory, identity);
  assert.equal(reserve(reopened).submit, false);
  assert.equal(reopened.status(permit().executionId).reservedBytes, "200");
  reopened.close();
});
test("generation fencing and deadline tightening never reset accumulated work", (t) => {
  const { ledger } = fixture(t);
  ledger.register(permit(), 100000);
  reserve(ledger);
  ledger.register(permit({ generation: 2, deadlineMs: 150000 }), 110000);
  assert.throws(() => reserve(ledger), /attempt/);
  assert.throws(
    () => ledger.register(permit({ generation: 3, totalBytes: "300" }), 120000),
    /identity/,
  );
  const same = ledger.admit(
    permit().executionId,
    2,
    "1".repeat(64),
    config(),
    120000,
  );
  assert.equal(same.submit, false);
  assert.throws(
    () =>
      ledger.admit(permit().executionId, 2, "2".repeat(64), config(), 150000),
    /deadline/,
  );
});
test("seal is durable, blocks late admission and never mistakes cancellation for DONE", (t) => {
  const { directory, ledger } = fixture(t);
  ledger.register(permit(), 100000);
  const job = reserve(ledger).job;
  ledger.observe(permit().executionId, job.key, "cancel_requested");
  assert.deepEqual(ledger.seal(permit().executionId, 1), {
    sealed: true,
    allJobsDone: false,
    jobs: 1,
  });
  assert.throws(() => reserve(ledger, "2".repeat(64)), /sealed/);
  assert.equal(reserve(ledger).submit, false);
  ledger.close();
  const reopened = new AdmissionLedger(directory, identity);
  assert.equal(reopened.status(permit().executionId).allJobsDone, false);
  reopened.observe(permit().executionId, job.key, "done");
  assert.deepEqual(reopened.seal(permit().executionId, 1), {
    sealed: true,
    allJobsDone: true,
    jobs: 1,
  });
  reopened.close();
});
test("unchanged poll observations do not grow journal and terminal states cannot regress", (t) => {
  const { directory, ledger } = fixture(t);
  ledger.register(permit(), 100000);
  const job = reserve(ledger).job;
  ledger.observe(permit().executionId, job.key, "running");
  const size = fs.statSync(path.join(directory, "journal")).size;
  for (let i = 0; i < 100; i++)
    ledger.observe(permit().executionId, job.key, "running");
  assert.equal(fs.statSync(path.join(directory, "journal")).size, size);
  ledger.observe(permit().executionId, job.key, "done");
  assert.throws(
    () => ledger.observe(permit().executionId, job.key, "running"),
    /terminal/,
  );
});
test("actual independent processes cannot acquire the same ledger lock", (t) => {
  const { directory } = fixture(t);
  const modulePath = path.resolve("cube/bigquery/admission-ledger.cjs");
  const child = cp.spawnSync(
    process.execPath,
    [
      "-e",
      `const {AdmissionLedger}=require(${JSON.stringify(modulePath)}); new AdmissionLedger(${JSON.stringify(directory)},${JSON.stringify(identity)});`,
    ],
    { encoding: "utf8" },
  );
  assert.notEqual(child.status, 0);
  assert.match(child.stderr, /exclusive/);
});
test("a killed owner leaves a non-stealable lock and durable unknown submission", (t) => {
  const { directory, ledger } = fixture(t);
  ledger.close();
  const modulePath = path.resolve("cube/bigquery/admission-ledger.cjs");
  const child = cp.spawnSync(process.execPath, [
    "-e",
    `const {AdmissionLedger}=require(${JSON.stringify(modulePath)});const l=new AdmissionLedger(${JSON.stringify(directory)},${JSON.stringify(identity)});l.register(${JSON.stringify(permit())},100000);l.admit(${JSON.stringify(permit().executionId)},1,${JSON.stringify("1".repeat(64))},${JSON.stringify(config())},100000);process.kill(process.pid,'SIGKILL');`,
  ]);
  assert.equal(child.signal, "SIGKILL");
  assert.throws(() => new AdmissionLedger(directory, identity), /exclusive/);
  const journal = fs.readFileSync(path.join(directory, "journal"));
  assert.ok(journal.includes(Buffer.from("submit_intent")));
});
for (const fault of [
  "missing-head",
  "missing-journal",
  "partial-frame",
  "bad-hash",
  "wrong-id",
])
  test(`restart rejects ${fault} without resetting state`, (t) => {
    const { directory, ledger } = fixture(t);
    ledger.register(permit(), 100000);
    reserve(ledger);
    ledger.close();
    if (fault === "missing-head") fs.unlinkSync(path.join(directory, "head"));
    if (fault === "missing-journal")
      fs.unlinkSync(path.join(directory, "journal"));
    if (fault === "partial-frame")
      fs.appendFileSync(path.join(directory, "journal"), Buffer.from([0, 0]));
    if (fault === "bad-hash") {
      const p = path.join(directory, "journal");
      const b = fs.readFileSync(p);
      b[20] ^= 1;
      fs.writeFileSync(p, b);
    }
    assert.throws(
      () =>
        new AdmissionLedger(directory, {
          ...identity,
          ...(fault === "wrong-id"
            ? { ledgerId: "287105f0-206e-4419-aec4-64707f6521d4" }
            : {}),
        }),
      /unavailable|identity|corrupt/,
    );
  });
test("missing volume, symlinks and permissive storage are rejected", (t) => {
  const { parent, directory, ledger } = fixture(t);
  ledger.close();
  assert.throws(
    () => new AdmissionLedger(path.join(parent, "absent"), identity),
    /unavailable/,
  );
  const linked = path.join(parent, "link");
  fs.symlinkSync(directory, linked);
  assert.throws(() => new AdmissionLedger(linked, identity), /unavailable/);
  fs.chmodSync(directory, 0o755);
  assert.throws(() => new AdmissionLedger(directory, identity), /unavailable/);
});
test("fixed storage bounds fail before mutation; unknown work is never evicted", (t) => {
  const { directory, ledger } = fixture(t, {
    maxExecutions: 1,
    maxJournalBytes: 4096,
    maxRecordBytes: 2048,
  });
  ledger.register(permit(), 100000);
  assert.throws(
    () =>
      ledger.register(
        permit({ executionId: "5068c6bb-d4c7-490d-b824-d14baa2aa451" }),
        100000,
      ),
    /capacity/,
  );
  reserve(ledger);
  const before = fs.statSync(path.join(directory, "journal")).size;
  assert.throws(
    () => ledger.register(permit({ scopeDigest: "f".repeat(64) }), 100000),
    /identity/,
  );
  assert.equal(fs.statSync(path.join(directory, "journal")).size, before);
});
test("live file replacement poisons mutations rather than writing an untracked inode", (t) => {
  const { directory, ledger } = fixture(t);
  ledger.register(permit(), 100000);
  fs.renameSync(path.join(directory, "journal"), path.join(directory, "old"));
  fs.writeFileSync(path.join(directory, "journal"), "");
  assert.throws(() => reserve(ledger), /unavailable/);
  assert.throws(() => ledger.seal(permit().executionId, 1), /unavailable/);
});
test("journal stores no raw query, parameter, or provider error", (t) => {
  const { directory, ledger } = fixture(t);
  ledger.register(permit(), 100000);
  reserve(ledger);
  const text = fs.readFileSync(path.join(directory, "journal")).toString();
  assert.ok(!text.includes("SELECT"));
  assert.ok(!text.includes("north"));
});
test("registration rejects authority beyond 120 seconds and malformed budgets", (t) => {
  const { ledger } = fixture(t);
  for (const bad of [
    permit({ deadlineMs: 220001 }),
    permit({ perJobBytes: "1e3" }),
    permit({ totalBytes: 200 }),
    permit({ generation: 1.5 }),
  ])
    assert.throws(() => ledger.register(bad, 100000));
  assert.throws(() => ledger.status(permit().executionId));
});
test("actual journal ceiling rejects new records without truncating retained work", (t) => {
  const { directory, ledger } = fixture(t, {
    maxJournalBytes: 4096,
    maxRecordBytes: 2048,
  });
  ledger.register(permit(), 100000);
  reserve(ledger);
  let reached = false;
  for (let generation = 2; generation < 100; generation++)
    try {
      ledger.register(permit({ generation }), 100000);
    } catch (e) {
      assert.match(e.message, /capacity/);
      reached = true;
      break;
    }
  assert.equal(reached, true);
  assert.ok(fs.statSync(path.join(directory, "journal")).size <= 4096);
  assert.equal(ledger.status(permit().executionId).reservedBytes, "100");
});
test("concurrent scheduled admission and seal have one durable ordering", async (t) => {
  const { ledger } = fixture(t);
  ledger.register(permit(), 100000);
  await Promise.all([
    Promise.resolve().then(() => reserve(ledger)),
    Promise.resolve().then(() => ledger.seal(permit().executionId, 1)),
  ]);
  assert.equal(ledger.status(permit().executionId).jobs, 1);
  assert.throws(() => reserve(ledger, "2".repeat(64)), /sealed/);
});
test("unsafe permissions acquired during a run poison subsequent admission", (t) => {
  const { directory, ledger } = fixture(t);
  ledger.register(permit(), 100000);
  fs.chmodSync(path.join(directory, "journal"), 0o644);
  assert.throws(() => reserve(ledger), /unavailable/);
});
test("manifest loss or same-size journal corruption during a run poisons mutation", (t) => {
  const one = fixture(t);
  one.ledger.register(permit(), 100000);
  fs.unlinkSync(path.join(one.directory, "manifest"));
  assert.throws(() => reserve(one.ledger), /unavailable/);
  const two = fixture(t);
  two.ledger.register(permit(), 100000);
  const file = path.join(two.directory, "journal");
  const data = fs.readFileSync(file);
  data[20] ^= 1;
  fs.writeFileSync(file, data);
  assert.throws(() => reserve(two.ledger), /unavailable/);
});
