"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const modulePath = path.join(
  __dirname,
  "../../cube/bigquery-runtime/contract.cjs",
);

test("serving contract provides separate versioned strict validators", () => {
  assert.equal(
    fs.existsSync(modulePath),
    true,
    "New serving contract must exist without editing legacy controls",
  );
  const C = require(modulePath);
  assert.equal(typeof C.validateMetadata, "function");
  assert.equal(typeof C.validateRows, "function");
  assert.equal(typeof C.validateEnvironment, "function");
});
const hash = (value) =>
  require("node:crypto")
    .createHash("sha256")
    .update(JSON.stringify(value))
    .digest("hex");
function fixture() {
  const config = {
    project_id: "fixture-project",
    location: "US",
    submitting_principal: "executor@fixture-project.iam.gserviceaccount.com",
    query_digest: hash("SELECT ? AS total"),
    parameters_digest: hash(["9007199254740993"]),
    maximum_bytes_billed: "1048576",
    job_timeout_ms: 25000,
    submitted_destination: false,
    submitted_create_disposition: false,
    submitted_write_disposition: false,
  };
  const intent = {
    query_digest: config.query_digest,
    parameters_digest: config.parameters_digest,
    parameter_types_digest: hash(["STRING"]),
    maximum_bytes_billed: config.maximum_bytes_billed,
    job_timeout_ms: config.job_timeout_ms,
  };
  const job = {
    job_id: "sp_" + "a".repeat(64),
    project_id: config.project_id,
    location: config.location,
    principal: config.submitting_principal,
    intent,
    intent_digest: "b".repeat(64),
  };
  const metadata = {
    jobReference: {
      jobId: job.job_id,
      projectId: config.project_id,
      location: "US",
    },
    user_email: config.submitting_principal,
    configuration: {
      jobTimeoutMs: "25000",
      query: {
        query: "SELECT ? AS total",
        queryParameters: [
          {
            parameterType: { type: "STRING" },
            parameterValue: { value: "9007199254740993" },
          },
        ],
        parameterMode: "POSITIONAL",
        useLegacySql: false,
        useQueryCache: false,
        maximumBytesBilled: "1048576",
      },
    },
    status: { state: "DONE" },
    statistics: { query: { statementType: "SELECT" } },
  };
  return { job, metadata };
}
test("metadata admits documented defaults and response-only anonymous destination with exact provenance", () => {
  const C = require(modulePath);
  const { job, metadata } = fixture();
  job.intent.configuration_digest = C.configurationDigest(job);
  metadata.configuration.jobType = "QUERY";
  metadata.configuration.dryRun = false;
  Object.assign(metadata.configuration.query, {
    priority: "INTERACTIVE",
    createSession: false,
    allowLargeResults: false,
    flattenResults: true,
    destinationTable: {
      projectId: "fixture-project",
      datasetId: "_anonymous_123",
      tableId: "anonymous_result_123",
    },
    createDisposition: "CREATE_IF_NEEDED",
    writeDisposition: "WRITE_EMPTY",
  });
  assert.deepEqual(
    C.validateMetadata(metadata, job, {
      maxMetadataBytes: 65536,
      inputTables: ["fixture-project.inputs.usage"],
    }),
    { state: "done", terminal_error: false, intent_digest: job.intent_digest },
  );
});
test("metadata mismatches never produce terminal evidence including arbitrary hidden permanent destinations", () => {
  const C = require(modulePath);
  for (const mutate of [
    (m) => {
      m.user_email = "attacker@fixture-project.iam.gserviceaccount.com";
    },
    (m) => {
      m.jobReference.jobId = "sp_" + "b".repeat(64);
    },
    (m) => {
      m.configuration.query.query += " WHERE false";
    },
    (m) => {
      m.configuration.query.queryParameters[0].parameterType.type = "INT64";
    },
    (m) => {
      m.configuration.query.maximumBytesBilled = "99999999";
    },
    (m) => {
      m.configuration.jobTimeoutMs = "30000";
    },
    (m) => {
      m.configuration.query.destinationTable = {
        projectId: "other-project",
        datasetId: "_fake",
        tableId: "permanent",
      };
    },
    (m) => {
      m.configuration.query.destinationTable = {
        projectId: "fixture-project",
        datasetId: "inputs",
        tableId: "usage",
      };
    },
    (m) => {
      m.configuration.query.writeDisposition = "WRITE_TRUNCATE";
    },
    (m) => {
      m.configuration.query.createDisposition = "CREATE_IF_NEEDED";
    },
    (m) => {
      m.configuration.query.connectionProperties = [];
    },
    (m) => {
      m.configuration.query.defaultDataset = {};
    },
    (m) => {
      m.configuration.query.labels = { x: "y" };
    },
    (m) => {
      m.configuration.query.scriptOptions = {};
    },
    (m) => {
      m.statistics.query.statementType = "CREATE_TABLE_AS_SELECT";
    },
    (m) => {
      m.statistics.parentJobId = "parent";
    },
    (m) => {
      m.configuration.query.parameterMode = "positional";
    },
    (m) => {
      m.configuration.unknown = true;
    },
  ]) {
    const { job, metadata } = fixture();
    job.intent.configuration_digest = C.configurationDigest(job);
    mutate(metadata);
    assert.throws(() =>
      C.validateMetadata(metadata, job, {
        maxMetadataBytes: 65536,
        inputTables: ["fixture-project.inputs.usage"],
      }),
    );
  }
  const { job, metadata } = fixture();
  job.intent.configuration_digest = "c".repeat(64);
  metadata.configuration.query.destinationTable = {
    projectId: "fixture-project",
    datasetId: "_fake",
    tableId: "permanent",
  };
  assert.throws(() =>
    C.validateMetadata(metadata, job, {
      maxMetadataBytes: 65536,
      inputTables: [],
    }),
  );
});
test("metadata redacts diagnostics, distinguishes pending and error terminal, bounds parsed input", () => {
  const C = require(modulePath);
  const { job, metadata } = fixture();
  job.intent.configuration_digest = C.configurationDigest(job);
  metadata.status = {
    state: "DONE",
    errorResult: { message: "secret sql", reason: "invalidQuery" },
  };
  delete metadata.statistics.query.statementType;
  assert.equal(
    C.validateMetadata(metadata, job, {
      maxMetadataBytes: 65536,
      inputTables: [],
    }).terminal_error,
    true,
  );
  metadata.status = { state: "RUNNING" };
  assert.equal(
    C.validateMetadata(metadata, job, {
      maxMetadataBytes: 65536,
      inputTables: [],
    }).state,
    "running",
  );
  metadata.diagnostic = "x".repeat(65536);
  assert.throws(() =>
    C.validateMetadata(metadata, job, {
      maxMetadataBytes: 65536,
      inputTables: [],
    }),
  );
});
test("rows preserve exact strings and forbid partial, oversize and unsupported values", () => {
  const C = require(modulePath);
  const limits = { maxRows: 1, maxResultBytes: 1024 };
  assert.deepEqual(
    C.validateRows([[{ v: "9007199254740993", n: null }], null, {}], limits),
    [[{ v: "9007199254740993", n: null }], null, {}],
  );
  for (const result of [
    [[{ v: 1 }]],
    [[{ v: { value: "1" } }]],
    [[{ v: "ok" }], { pageToken: "x" }],
    [[{ v: "ok" }], null, { pageToken: "x" }],
    [[{}, {}]],
    [[{ v: "x".repeat(1024) }]],
  ])
    assert.throws(() => C.validateRows(result, limits));
});
test("environment refuses ambient credentials, alternate metadata or proxy endpoints even when empty", () => {
  const C = require(modulePath);
  assert.doesNotThrow(() => C.validateEnvironment({ NODE_ENV: "production" }));
  for (const name of [
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_AUTH_TOKEN",
    "CUBEJS_DB_BQ_KEY_FILE",
    "CUBEJS_DS_OTHER_DB_BQ_CREDENTIALS",
    "CUBEJS_DB_EXPORT_BUCKET",
    "CUBEJS_DS_OTHER_DB_BQ_PROJECT_ID",
    "GCE_METADATA_HOST",
    "GCE_METADATA_IP",
    "GCE_METADATA_ROOT",
    "GOOGLE_CLOUD_UNIVERSE_DOMAIN",
    "HTTP_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "BIGQUERY_EMULATOR_HOST",
  ])
    assert.throws(() => C.validateEnvironment({ [name]: "" }), name);
});
test("immutable snapshots detach and recursively freeze callback-visible configuration", () => {
  const C = require(modulePath);
  const original = { auth: { principal: "one" }, tables: ["approved"] };
  const snapshot = C.immutable(original);
  original.auth.principal = "other";
  original.tables.push("unreviewed");
  assert.equal(snapshot.auth.principal, "one");
  assert.deepEqual(snapshot.tables, ["approved"]);
  assert.throws(() => snapshot.tables.push("x"));
});
