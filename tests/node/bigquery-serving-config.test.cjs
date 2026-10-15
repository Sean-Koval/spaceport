"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const file = path.resolve(__dirname, "../../cube/bigquery-runtime/config.cjs");
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
test("profile parser mirrors exact Python descriptor with computed physical capacity and fixed limits", () => {
  assert.equal(fs.existsSync(file), true);
  const R = require(file);
  assert.deepEqual(R.validateProfile(profile), profile);
  assert.equal(
    R.driverSource(profile).principal,
    profile.auth.expected_principal,
  );
  assert.equal(R.driverSource(profile).maxMetadataBytes, 65536);
  assert.equal(R.driverSource(profile).maxResultBytes, 1048576);
  assert.equal(
    R.capacityKey(profile),
    R.capacityKey({
      ...profile,
      source_id: "alias",
      revision: 2,
      dataset: "other",
      auth: {
        ...profile.auth,
        expected_principal: "rotated@fixture-project.iam.gserviceaccount.com",
      },
    }),
  );
  for (const mutate of [
    (p) => (p.auth.mode = "adc"),
    (p) => (p.auth.key_file = "/tmp/key"),
    (p) => (p.location = "EU"),
    (p) => p.tables.push("usage"),
    (p) => (p.unknown = true),
    (p) => (p.total_execution_bytes = 1),
    (p) => (p.cube_endpoint = "http://cube.example.test"),
  ]) {
    const p = structuredClone(profile);
    mutate(p);
    assert.throws(() => R.validateProfile(p));
  }
});
test("bundle hashing pins static file bytes and rejects symlinks, executable models and context interpolation", () => {
  const R = require(file);
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "bq-bundle-"));
  try {
    fs.mkdirSync(path.join(root, "model"));
    fs.writeFileSync(path.join(root, "security.json"), "{}");
    fs.writeFileSync(path.join(root, "model", "usage.yml"), "cubes: []\n");
    const first = R.bundleDigest(root);
    fs.appendFileSync(path.join(root, "model", "usage.yml"), "# comment\n");
    assert.notEqual(R.bundleDigest(root), first);
    fs.writeFileSync(path.join(root, "model", "evil.js"), "process.exit()");
    assert.throws(() => R.bundleDigest(root));
    fs.unlinkSync(path.join(root, "model", "evil.js"));
    fs.writeFileSync(
      path.join(root, "model", "usage.yml"),
      "cubes: [] # COMPILE_CONTEXT\n",
    );
    assert.throws(() => R.bundleDigest(root));
    fs.unlinkSync(path.join(root, "model", "usage.yml"));
    fs.symlinkSync("/etc/hosts", path.join(root, "model", "usage.yml"));
    assert.throws(() => R.bundleDigest(root));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
test("server and observer envelopes separate signing/materialized models from metadata-only credentials", () => {
  const R = require(file);
  const base = {
    schema_version: 1,
    profile,
    deployment_epoch: "fixture-epoch",
    bundle_ref: {
      digest: profile.model_bundle_digest,
      context: "consumer",
      root: "/run/source/bundle",
    },
    signing_key_file: "/run/source/secrets/cube_signing_key",
    ledger: {
      host: "registry-db",
      port: 5432,
      database: "gateway",
      user: "bq_runtime",
      password_file: "/run/source/secrets/bq_runtime",
      ca_file: "/run/source/secrets/registry_ca",
    },
  };
  assert.doesNotThrow(() => R.validateEnvelope(base, "server"));
  const observer = {
    ...base,
    observer_auth: {
      mode: "gke-workload-identity",
      expected_principal: "observer@fixture-project.iam.gserviceaccount.com",
    },
    bundle_ref: { digest: profile.model_bundle_digest, context: "consumer" },
  };
  delete observer.signing_key_file;
  assert.doesNotThrow(() => R.validateEnvelope(observer, "observer"));
  assert.throws(() => R.validateEnvelope(observer, "server"));
  assert.throws(() =>
    R.validateEnvelope(
      { ...observer, signing_key_file: base.signing_key_file },
      "observer",
    ),
  );
  assert.throws(() =>
    R.validateEnvelope(
      { ...base, ledger: { ...base.ledger, ssl: false } },
      "server",
    ),
  );
  assert.throws(() =>
    R.validateEnvelope({ ...base, deployment_epoch: "" }, "server"),
  );
});
test("retained bundle snapshot is compiled from its checked bytes after on-disk replacement", async () => {
  const R = require(file);
  assert.equal(typeof R.loadBundle, "function");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "bq-frozen-bundle-"));
  try {
    fs.cpSync(
      path.resolve(__dirname, "../runtime/bigquery-serving-bundle"),
      root,
      { recursive: true },
    );
    const expected = R.bundleDigest(root);
    const bundle = R.loadBundle(root, expected);
    const before = bundle.files[0].content;
    fs.writeFileSync(
      path.join(root, "model", "usage.yml"),
      "cubes: [] # hostile replacement\n",
    );
    fs.writeFileSync(path.join(root, "security.json"), "{}");
    assert.equal(bundle.digest, expected);
    assert.equal(R.validateBundle(bundle).files[0].content, before);
    assert.throws(() => R.loadBundle(root, expected));
    const modified = structuredClone(bundle);
    modified.files[0].content += "\n# changed";
    assert.throws(() => R.validateBundle(modified));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
test('observer identity must be distinct from the execution principal',()=>{
 const R=require(file);const observer={schema_version:1,profile,deployment_epoch:'fixture-epoch',bundle_ref:{digest:profile.model_bundle_digest,context:'consumer'},ledger:{host:'registry-db',port:5432,database:'gateway',user:'bq_observer',password_file:'/run/source/secrets/bq_observer',ca_file:'/run/source/secrets/registry_ca'},observer_auth:profile.auth};
 assert.throws(()=>R.validateEnvelope(observer,'observer'));
});
