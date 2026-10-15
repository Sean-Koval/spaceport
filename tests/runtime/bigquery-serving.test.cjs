"use strict";
const test = require("node:test"),
  assert = require("node:assert/strict"),
  path = require("node:path"),
  { spawn } = require("node:child_process");
const OPERATIONS = ["revoke_queued", "reclaim", "close_main", "close_zero"];
const CASES = ["main", "revocation", "reclaim", "zero_job", "unknown"];
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
const CHECKPOINTS = [
  "source-config",
  "source-startup",
  "source-main",
  "source-revocation",
  "source-reclaim",
  "source-zero",
  "source-unknown",
  "source-cleanup",
];
test(
  "actual Cube HTTP compiler and SDK with real PostgreSQL source ownership and a fake provider",
  { timeout: 118000 },
  async () => {
    let resultSeen = false;
    const controls = [];
    function result(value) {
      assert.equal(resultSeen, false);
      assert.equal(value.schema_version, 1);
      if (value.status === "passed")
        assert.deepEqual(value, {
          schema_version: 1,
          status: "passed",
          operations: OPERATIONS,
          cases: CASES,
          checks: CHECKS,
          provider: "fake",
          registry: "postgresql",
        });
      else {
        assert.deepEqual(Object.keys(value).sort(), [
          "checkpoint",
          "schema_version",
          "status",
        ]);
        assert.equal(value.status, "failed");
        assert.ok(CHECKPOINTS.includes(value.checkpoint));
      }
      resultSeen = true;
      process.stdout.write(
        "GATEWAY_SOURCE_RUNTIME_RESULT " + JSON.stringify(value) + "\n",
      );
    }
    const env = {
      PATH: process.env.PATH || "/usr/bin:/bin",
      HOME: "/tmp/spaceport-serving-no-adc",
      NODE_ENV: "production",
      TZ: "UTC",
      CUBEJS_DEV_MODE: "false",
      CUBEJS_DB_TYPE: "bigquery",
      PORT: "0",
      CUBEJS_EXTERNAL_DEFAULT: "false",
      GATEWAY_SOURCE_TEST_AUTHORIZATION:
        process.env.GATEWAY_SOURCE_TEST_AUTHORIZATION || "",
      GATEWAY_SOURCE_FIXTURE_FILE:
        process.env.GATEWAY_SOURCE_FIXTURE_FILE || "",
      GATEWAY_BIGQUERY_RUNTIME_DIR:
        process.env.GATEWAY_BIGQUERY_RUNTIME_DIR ||
        path.resolve(__dirname, "../../cube/bigquery-runtime"),
    };
    if (process.env.NODE_PATH) env.NODE_PATH = process.env.NODE_PATH;
    async function child(args, forward) {
      await new Promise((resolve, reject) => {
        const processChild = spawn(process.execPath, args, {
          env,
          cwd: path.resolve(__dirname, "../.."),
          stdio: ["ignore", "pipe", "pipe"],
        });
        let pending = "",
          bytes = 0,
          failure;
        const timer = setTimeout(
          () => {
            failure = new Error("Serving fixture exceeded its process budget.");
            processChild.kill("SIGTERM");
          },
          forward ? 100000 : 15000,
        );
        const stop = () => {
          failure = new Error("Invalid serving fixture output.");
          processChild.kill("SIGTERM");
        };
        processChild.stdout.on("data", (chunk) => {
          bytes += chunk.length;
          if (bytes > 131072) return stop();
          pending += chunk.toString("utf8");
          let newline;
          while ((newline = pending.indexOf("\n")) >= 0) {
            const line = pending.slice(0, newline);
            pending = pending.slice(newline + 1);
            if (!forward) continue;
            try {
              if (line.startsWith("GATEWAY_SOURCE_CONTROL ")) {
                assert.ok(line.length <= 256);
                const value = JSON.parse(
                  line.slice("GATEWAY_SOURCE_CONTROL ".length),
                );
                assert.deepEqual(Object.keys(value), ["operation"]);
                assert.equal(value.operation, OPERATIONS[controls.length]);
                controls.push(value.operation);
                process.stdout.write(line + "\n");
              } else if (line.startsWith("GATEWAY_SOURCE_RUNTIME_RESULT ")) {
                assert.ok(line.length <= 4096);
                const value = JSON.parse(
                  line.slice("GATEWAY_SOURCE_RUNTIME_RESULT ".length),
                );
                if (value.status === "passed")
                  assert.deepEqual(controls, OPERATIONS);
                result(value);
              }
            } catch {
              stop();
            }
          }
        });
        processChild.stderr.on("data", (chunk) => {
          bytes += chunk.length;
          if (bytes > 131072) stop();
        });
        processChild.on("error", () => {
          clearTimeout(timer);
          reject(new Error("Serving fixture child failed to start."));
        });
        processChild.on("close", (code) => {
          clearTimeout(timer);
          if (failure || code !== 0)
            return reject(
              failure || new Error("Serving fixture child failed."),
            );
          resolve();
        });
      });
    }
    try {
      assert.equal(
        env.GATEWAY_SOURCE_TEST_AUTHORIZATION,
        "private-synthetic-source-only",
      );
      assert.ok(path.isAbsolute(env.GATEWAY_SOURCE_FIXTURE_FILE));
      // A new child has an explicit environment; inherited legacy cube-reviewed
      // PostgreSQL/SQL-port settings never alter this source process or its parent.
      await child(
        [
          "--test",
          "--test-reporter=spec",
          path.join(__dirname, "bigquery-journey-child.test.cjs"),
          ...["guard", "driver", "server", "reconciler"].map((name) =>
            path.join(__dirname, "bigquery-serving-" + name + ".test.cjs"),
          ),
        ],
        false,
      );
      await child([path.join(__dirname, "bigquery-serving-worker.cjs")], true);
      assert.equal(resultSeen, true);
    } catch (error) {
      if (!resultSeen)
        result({
          schema_version: 1,
          status: "failed",
          checkpoint: "source-startup",
        });
      throw new Error(
        "Real-registry BigQuery serving acceptance did not pass.",
      );
    }
  },
);
