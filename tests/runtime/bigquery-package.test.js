"use strict";
// Mandatory actual-image test. Missing packages are failures, never skips.
// This script permits only real local ledger filesystem I/O; provider replies are synthetic.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const net = require("node:net");
const http = require("node:http");
const https = require("node:https");
const dns = require("node:dns");
const { failureEvidence } = require("./bigquery-probe-evidence.cjs");
const controls =
  process.env.GATEWAY_BIGQUERY_CONTROLS_DIR || "/cube/conf/bigquery";
const keyFilename = "/run/secrets/bq_package_fixture_never_exists";
const denied = { external: 0, authentication: 0, credentialRead: 0 };
function denial(kind) {
  return () => {
    denied[kind]++;
    throw new Error("External I/O is forbidden by the package contract test.");
  };
}
const deny = denial("external");
const denyAuthentication = denial("authentication");
const denyCredentialRead = denial("credentialRead");
// Install guards before loading Cube/Google packages, including module initialization.
net.Socket.prototype.connect = deny;
net.connect = deny;
net.createConnection = deny;
http.request = deny;
http.get = deny;
https.request = deny;
https.get = deny;
require("node:tls").connect = deny;
require("node:http2").connect = deny;
const dgram = require("node:dgram");
dgram.createSocket = deny;
dgram.Socket.prototype.connect = deny;
dgram.Socket.prototype.send = deny;
for (const api of [
  dns,
  dns.promises,
  dns.Resolver.prototype,
  dns.promises.Resolver.prototype,
]) {
  for (const name of Object.getOwnPropertyNames(api)) {
    if (/^(?:lookup|resolve)/.test(name) && typeof api[name] === "function")
      api[name] = deny;
  }
}
globalThis.fetch = deny;
for (const [api, names] of [
  [fs, ["readFileSync", "readFile", "openSync", "open", "createReadStream"]],
  [fs.promises, ["readFile", "open"]],
]) {
  for (const name of names) {
    const original = api[name];
    api[name] = function (filename, ...args) {
      const value =
        filename instanceof URL
          ? require("node:url").fileURLToPath(filename)
          : String(filename);
      if (value === keyFilename || value.startsWith("/run/secrets/"))
        return denyCredentialRead();
      return original.call(this, filename, ...args);
    };
  }
}

test(
  "actual Cube 1.8.5 subclass and packaged SDK enforce serialized job controls without cloud access",
  { timeout: 10000 },
  async (t) => {
    const evidence = failureEvidence();
    t.after(() => {
      try {
        assert.deepEqual(
          denied,
          { external: 0, authentication: 0, credentialRead: 0 },
          "No swallowed authentication or external I/O attempt is permitted",
        );
      } catch (error) {
        evidence.stage("cube-bigquery-io-guard");
        evidence.emit();
        throw error;
      }
    });
    try {
      evidence.stage("cube-bigquery-module-resolution");
      const { createRequire } = require("node:module");
      // Resolve with the same image location that imports the actual upstream driver.
      const upstreamPath = require.resolve("@cubejs-backend/bigquery-driver");
      const upstreamRequire = createRequire(upstreamPath);
      evidence.stage("cube-bigquery-package-versions");
      assert.equal(
        upstreamRequire("@cubejs-backend/bigquery-driver/package.json").version,
        "1.8.5",
      );
      const { BigQueryDriver } = upstreamRequire(
        "@cubejs-backend/bigquery-driver",
      );
      const sdkVersion = upstreamRequire(
        "@google-cloud/bigquery/package.json",
      ).version;
      assert.match(sdkVersion, /^7\./);
      evidence.stage("cube-bigquery-module-resolution");
      const { boundedDriverClass } = require(
        path.join(controls, "bounded-driver.cjs"),
      );
      const { AdmissionLedger, initializeLedger } = require(
        path.join(controls, "admission-ledger.cjs"),
      );
      const C = require(path.join(controls, "runtime-contract.cjs"));
      evidence.stage("cube-bigquery-ledger");
      const parent = fs.mkdtempSync(
        path.join(os.tmpdir(), "spaceport-bq-package-"),
      );
      fs.chmodSync(parent, 0o700);
      const directory = path.join(parent, "ledger");
      const identity = {
        deploymentId: "package-fixture",
        ledgerId: "13f7fc08-265b-4ec4-a841-2db459534049",
      };
      initializeLedger(directory, {
        ...identity,
        limits: {
          maxJournalBytes: 65536,
          maxRecordBytes: 8192,
          maxExecutions: 1,
          maxJobsPerExecution: 2,
        },
      });
      const ledger = new AdmissionLedger(directory, identity);
      t.after(() => {
        try {
          ledger.close();
          fs.rmSync(parent, { recursive: true, force: true });
        } catch (error) {
          evidence.stage("cube-bigquery-cleanup");
          evidence.emit();
          throw error;
        }
      });
      const source = {
        projectId: "fixture-project",
        datasetId: "fixture_dataset",
        location: "US",
        keyFilename,
        jobTimeoutMs: 25000,
        maxRows: 1000,
        maxResultBytes: 1048576,
        maxMetadataBytes: 65536,
        pollIntervalMs: 1,
      };
      const permit = {
        executionId: "ba3ec574-dad0-409e-bb05-024fcd01468d",
        generation: 1,
        scopeDigest: "a".repeat(64),
        queryDigest: "b".repeat(64),
        bindingDigest: "c".repeat(64),
        bundleDigest: "d".repeat(64),
        sourceDigest: C.digest({
          projectId: source.projectId,
          datasetId: source.datasetId,
          location: source.location,
        }),
        deadlineMs: Date.now() + 60000,
        maxJobs: 2,
        perJobBytes: "10485760",
        totalBytes: "20971520",
      };
      ledger.register(permit, Date.now());
      evidence.stage("cube-bigquery-driver-construction");
      const Driver = boundedDriverClass(BigQueryDriver);
      const driver = new Driver({
        source,
        ledger,
        scopeDigest: permit.scopeDigest,
      });
      assert.ok(driver instanceof BigQueryDriver);
      // Bypass only the provider transport boundary, after the real SDK serializes requests.
      for (const auth of [
        driver.bigquery.authClient,
        driver.bigquery.auth,
        driver.bigquery.googleAuth,
      ]) {
        if (auth)
          for (const method of [
            "getClient",
            "getCredentials",
            "getRequestHeaders",
            "getAccessToken",
            "authorizeRequest",
          ])
            if (typeof auth[method] === "function")
              auth[method] = denyAuthentication;
      }
      let insert,
        metadata,
        calls = 0;
      driver.bigquery.request = (request, callback) => {
        calls++;
        if (request.method === "POST" && request.uri === "/jobs") {
          evidence.stage("cube-bigquery-serialization");
          assert.equal(insert, undefined, "No duplicate source job submission");
          insert = structuredClone(request.json);
          assert.equal(insert.configuration.jobTimeoutMs, "25000");
          assert.equal(
            insert.configuration.query.maximumBytesBilled,
            "10485760",
          );
          assert.equal(insert.configuration.query.jobTimeoutMs, undefined);
          assert.equal(insert.configuration.query.useLegacySql, false);
          assert.equal(insert.configuration.query.parameterMode, "positional");
          assert.equal(insert.configuration.query.useQueryCache, false);
          assert.equal(insert.jobReference.projectId, "fixture-project");
          assert.equal(insert.jobReference.location, "US");
          assert.match(insert.jobReference.jobId, /^sp_[a-f0-9]{64}$/);
          const q = insert.configuration.query;
          metadata = {
            jobReference: insert.jobReference,
            configuration: {
              jobTimeoutMs: "25000",
              query: {
                query: q.query,
                queryParameters: q.queryParameters,
                // Synthetic API metadata uses the reviewed canonical enum.
                parameterMode: "POSITIONAL",
                maximumBytesBilled: q.maximumBytesBilled,
                useLegacySql: false,
                useQueryCache: false,
              },
            },
            status: { state: "DONE" },
          };
          evidence.stage("cube-bigquery-query");
          return setImmediate(() => callback(null, structuredClone(metadata)));
        }
        if (request.uri === `jobs/${insert?.jobReference.jobId}`)
          return setImmediate(() => callback(null, structuredClone(metadata)));
        if (request.uri === `/queries/${insert?.jobReference.jobId}`) {
          evidence.stage("cube-bigquery-result-hydration");
          return setImmediate(() =>
            callback(null, {
              jobReference: insert.jobReference,
              jobComplete: true,
              totalRows: "1",
              schema: {
                fields: [
                  { name: "total", type: "NUMERIC" },
                  { name: "segment", type: "STRING" },
                  { name: "loaded_at", type: "STRING" },
                ],
              },
              rows: [
                {
                  f: [
                    { v: "9007199254740993.01" },
                    { v: null },
                    { v: "2024-03-10T00:00:00.000001Z" },
                  ],
                },
              ],
            }),
          );
        }
        throw new Error("Unreviewed provider RPC in package contract test.");
      };
      const query =
        "SELECT ? AS total, CAST(NULL AS STRING) AS segment, ? AS loaded_at";
      const params = ["9007199254740993.01", "2024-03-10T00:00:00.000001Z"];
      const context = { requestId: permit.executionId + ".g1" };
      evidence.stage("cube-bigquery-query");
      const rows = await driver.query(query, params, context);
      evidence.stage("cube-bigquery-result-hydration");
      assert.deepEqual(rows, [
        {
          total: "9007199254740993.01",
          segment: null,
          loaded_at: "2024-03-10T00:00:00.000001Z",
        },
      ]);
      assert.ok(calls >= 3);
      assert.equal(ledger.seal(permit.executionId, 1).allJobsDone, true);
      evidence.stage("cube-bigquery-replay");
      assert.deepEqual(await driver.query(query, params, context), rows);
      assert.equal(ledger.status(permit.executionId).jobs, 1);
      // Versions only; never SQL, parameters, credentials, or provider errors.
      t.diagnostic(
        `Actual packaged Cube 1.8.5 / Google BigQuery SDK ${sdkVersion}; synthetic provider transport only.`,
      );
    } catch (error) {
      evidence.emit();
      throw error;
    }
  },
);
