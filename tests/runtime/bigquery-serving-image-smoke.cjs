"use strict";
// Required only in the exact built image, --network none --read-only --tmpfs /tmp.
// This qualifies packaging/startup, never source authority or a provider query.
const guard = require("./bigquery-serving-guard.cjs").installGuard();
const assert = require("node:assert/strict"),
  fs = require("node:fs"),
  path = require("node:path"),
  { createRequire } = require("node:module"),
  { spawnSync } = require("node:child_process");
const PREFIX = "GATEWAY_BIGQUERY_IMAGE_RESULT ";
try {
  assert.ok(typeof process.getuid === "function" && process.getuid() !== 0);
  assert.equal(
    process.env.GATEWAY_BIGQUERY_RUNTIME_DIR,
    "/cube/bigquery-runtime",
  );
  assert.equal(process.env.CUBEJS_DOCKER_IMAGE_VERSION, undefined);
  assert.equal(process.env.CUBEJS_DOCKER_IMAGE_TAG, undefined);
  const driverRequire = createRequire(
    require.resolve("@cubejs-backend/bigquery-driver"),
  );
  const sdkRequire = createRequire(
    driverRequire.resolve("@google-cloud/bigquery"),
  );
  const commonRequire = createRequire(
    sdkRequire.resolve("@google-cloud/common"),
  );
  const versions = {
    cube_server: require("@cubejs-backend/server/package.json").version,
    cube_core: require("@cubejs-backend/server-core/package.json").version,
    cube_bigquery_driver: driverRequire(
      "@cubejs-backend/bigquery-driver/package.json",
    ).version,
    bigquery_sdk: driverRequire("@google-cloud/bigquery/package.json").version,
    compute_auth: require("google-auth-library/package.json").version,
    bigquery_auth: commonRequire("google-auth-library/package.json").version,
    pg: require("pg/package.json").version,
  };
  for (const name of ["cube_server", "cube_core", "cube_bigquery_driver"])
    assert.equal(versions[name], "1.8.5");
  assert.match(versions.bigquery_sdk, /^7\.\d+\.\d+$/);
  assert.equal(
    require("@google-cloud/bigquery/package.json").version,
    versions.bigquery_sdk,
  );
  assert.match(versions.compute_auth, /^(9|10)\.\d+\.\d+$/);
  assert.match(versions.bigquery_auth, /^(9|10)\.\d+\.\d+$/);
  assert.match(versions.pg, /^8\.\d+\.\d+$/);
  const root = process.env.GATEWAY_BIGQUERY_RUNTIME_DIR;
  for (const name of [
    "contract",
    "config",
    "postgres-ledger",
    "security",
    "driver",
    "server",
    "reconciler",
  ])
    require(path.join(root, name + ".cjs"));
  require(path.join(root, "server.cjs")).validateServerEnvironment();
  const auth = require(path.join(root, "driver.cjs")).explicitAuth(
    "executor@fixture-project.iam.gserviceaccount.com",
  );
  assert.equal(
    auth.serviceAccountEmail,
    "executor@fixture-project.iam.gserviceaccount.com",
  );
  new (require("@google-cloud/bigquery").BigQuery)({
    projectId: "fixture-project",
    authClient: auth,
    location: "US",
    autoRetry: false,
    maxRetries: 0,
  });
  const probe = "/cube/bigquery-runtime/.image-write-test";
  let writable = false;
  try {
    const fd = fs.openSync(probe, "wx");
    fs.closeSync(fd);
    writable = true;
    fs.unlinkSync(probe);
  } catch (error) {
    assert.ok(["EROFS", "EACCES"].includes(error.code));
  }
  assert.equal(writable, false);
  // Actual maintained HTTP server + compiler /meta, bounded fake read-only ledger,
  // no provider transport or auth. Its guard grants only its actual ephemeral port.
  const child = spawnSync(
    process.execPath,
    [
      "--test",
      "--test-reporter=spec",
      path.join(__dirname, "bigquery-serving-server.test.cjs"),
    ],
    {
      env: { ...process.env, PORT: "0" },
      encoding: "utf8",
      timeout: 20000,
      maxBuffer: 65536,
    },
  );
  assert.equal(child.status, 0);
  assert.equal(child.signal, null);
  guard.assertClean();
  require("@cubejs-backend/native").resetLogger("error");
  process.stdout.write(
    PREFIX +
      JSON.stringify({
        schema_version: 1,
        status: "passed",
        versions,
        node: process.version,
        uid: process.getuid(),
        checks: [
          "immutable_packages",
          "nonroot",
          "readonly_runtime",
          "lazy_imports",
          "http_health",
          "retained_compiler",
          "zero_provider_auth",
        ],
      }) +
      "\n",
  );
} catch {
  process.stdout.write(
    PREFIX + JSON.stringify({ schema_version: 1, status: "failed" }) + "\n",
  );
  process.exitCode = 1;
} finally {
  try {
    const native = require.resolve("@cubejs-backend/native");
    if (require.cache[native]) require(native).resetLogger("error");
  } catch {}
}
