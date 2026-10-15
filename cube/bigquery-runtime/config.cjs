"use strict";
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const C = require("./contract.cjs");
const S = require("./security.cjs");
const { PostgresLedger } = require("./postgres-ledger.cjs");
function validateAuth(a) {
  C.fields(a, ["mode", "expected_principal"]);
  if (a.mode !== "gke-workload-identity") C.fail();
  C.text(a.expected_principal, C.PRINCIPAL, 254);
}
function validateProfile(p) {
  C.fields(p, [
    "provider",
    "source_id",
    "revision",
    "context",
    "auth",
    "billing_project",
    "data_project",
    "dataset",
    "tables",
    "location",
    "dataset_location",
    "cube_endpoint",
    "model_bundle_digest",
    "source_policy_digest",
    "max_jobs_per_execution",
    "per_job_maximum_bytes_billed",
    "total_execution_bytes",
    "max_active_jobs",
    "max_global_active_jobs",
    "job_timeout_ms",
  ]);
  if (
    p.provider !== "bigquery" ||
    !["consumer", "fixture_test"].includes(p.context)
  )
    C.fail();
  C.text(p.source_id, /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/);
  C.integer(p.revision, 1, 2147483647);
  validateAuth(p.auth);
  C.text(p.billing_project, C.PROJECT);
  C.text(p.data_project, C.PROJECT);
  C.text(p.dataset, /^[A-Za-z_][A-Za-z0-9_]{0,127}$/);
  C.text(p.location, C.LOCATION);
  if (p.location !== p.dataset_location) C.fail();
  if (
    !Array.isArray(p.tables) ||
    !p.tables.length ||
    p.tables.length > 50 ||
    new Set(p.tables).size !== p.tables.length
  )
    C.fail();
  p.tables.forEach((t) => C.text(t, /^[A-Za-z_][A-Za-z0-9_]{0,127}$/));
  C.text(p.cube_endpoint, /^https:\/\/[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$/, 2048);
  const u = new URL(p.cube_endpoint);
  if (
    u.origin !== p.cube_endpoint ||
    u.hostname === "localhost" ||
    !u.hostname.includes(".") ||
    /^\d+\.\d+\.\d+\.\d+$/.test(u.hostname)
  )
    C.fail();
  C.text(p.model_bundle_digest, C.HEX);
  C.text(p.source_policy_digest, C.HEX);
  C.integer(p.max_jobs_per_execution, 1, 8);
  C.integer(p.per_job_maximum_bytes_billed, 1, Number.MAX_SAFE_INTEGER);
  C.integer(
    p.total_execution_bytes,
    p.per_job_maximum_bytes_billed,
    Number.MAX_SAFE_INTEGER,
  );
  C.integer(p.max_active_jobs, 1, 100);
  C.integer(p.max_global_active_jobs, p.max_active_jobs, 100);
  C.integer(p.job_timeout_ms, 1, 120000);
  return C.copy(p);
}
function capacityKey(p) {
  validateProfile(p);
  return (
    "bigquery-" +
    C.digest({
      provider: "bigquery",
      billing_project: p.billing_project,
      location: p.location,
    })
  );
}
function driverSource(p) {
  validateProfile(p);
  return {
    projectId: p.billing_project,
    location: p.location,
    principal: p.auth.expected_principal,
    inputTables: p.tables.map((t) => `${p.data_project}.${p.dataset}.${t}`),
    jobTimeoutMs: p.job_timeout_ms,
    pollIntervalMs: 100,
    maxRows: 1000,
    maxResultBytes: 1048576,
    maxMetadataBytes: 65536,
  };
}
function regularFile(filename, maximum = 65536) {
  let fd;
  try {
    if (!path.isAbsolute(filename) || path.normalize(filename) !== filename)
      C.fail();
    let parent = path.dirname(filename);
    while (parent !== path.dirname(parent)) {
      const st = fs.lstatSync(parent);
      if (!st.isDirectory() || st.isSymbolicLink()) C.fail();
      parent = path.dirname(parent);
    }
    fd = fs.openSync(filename, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
    const st = fs.fstatSync(fd);
    if (!st.isFile() || st.size < 1 || st.size > maximum) C.fail();
    const data = fs.readFileSync(fd);
    if (data.length !== st.size) C.fail();
    return data;
  } catch (_) {
    C.fail();
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}
function bundleBytes(root) {
  if (
    !path.isAbsolute(root) ||
    path.normalize(root) !== root ||
    !fs.lstatSync(root).isDirectory() ||
    fs.lstatSync(root).isSymbolicLink()
  )
    C.fail();
  if (
    C.canonical(fs.readdirSync(root).sort()) !==
    C.canonical(["model", "security.json"])
  )
    C.fail();
  const model = path.join(root, "model");
  if (
    !fs.lstatSync(model).isDirectory() ||
    fs.lstatSync(model).isSymbolicLink()
  )
    C.fail();
  const names = fs.readdirSync(model).sort();
  if (
    !names.length ||
    names.length > 16 ||
    names.some((n) => !/^[A-Za-z][A-Za-z0-9_-]{0,63}\.yml$/.test(n))
  )
    C.fail();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  return {
    securityContent: decoder.decode(
      regularFile(path.join(root, "security.json")),
    ),
    files: names.map((fileName) => ({
      fileName,
      content: decoder.decode(regularFile(path.join(model, fileName))),
    })),
  };
}
function hashBundle(securityContent, files) {
  if (
    typeof securityContent !== "string" ||
    Buffer.byteLength(securityContent) > 65536 ||
    !securityContent.length ||
    !Array.isArray(files) ||
    !files.length ||
    files.length > 16
  )
    C.fail();
  const contents = { "security.json": securityContent };
  for (const item of files) {
    C.fields(item, ["fileName", "content"]);
    C.text(item.fileName, /^[A-Za-z][A-Za-z0-9_-]{0,63}\.yml$/);
    if (
      typeof item.content !== "string" ||
      !item.content.length ||
      Buffer.byteLength(item.content) > 65536 ||
      Object.hasOwn(contents, "model/" + item.fileName) ||
      /COMPILE_CONTEXT|securityContext|security_context|USER_CONTEXT|\{\{|\{%|\$\{|FILTER_PARAMS|FILTER_GROUP/.test(
        item.content,
      )
    )
      C.fail();
    contents["model/" + item.fileName] = item.content;
  }
  const hash = crypto
    .createHash("sha256")
    .update("spaceport-cube-bigquery-bundle-v1\0");
  for (const name of Object.keys(contents).sort()) {
    const bytes = Buffer.from(contents[name], "utf8");
    hash.update(name + "\0" + bytes.length + "\0");
    hash.update(bytes);
  }
  return hash.digest("hex");
}
function bundleDigest(root) {
  const { securityContent, files } = bundleBytes(root);
  return hashBundle(securityContent, files);
}
function validateBundle(bundle) {
  C.fields(bundle, ["digest", "securityContent", "contract", "files"]);
  C.text(bundle.digest, C.HEX);
  if (hashBundle(bundle.securityContent, bundle.files) !== bundle.digest)
    C.fail();
  const contract = S.validateContract(JSON.parse(bundle.securityContent));
  if (C.digest(contract) !== C.digest(bundle.contract)) C.fail();
  return C.immutable(bundle);
}
function loadBundle(root, expectedDigest) {
  const { securityContent, files } = bundleBytes(root),
    digest = hashBundle(securityContent, files);
  if (expectedDigest !== undefined && digest !== expectedDigest) C.fail();
  return validateBundle({
    digest,
    securityContent,
    files,
    contract: S.validateContract(JSON.parse(securityContent)),
  });
}
function mountedFile(value) {
  C.text(
    value,
    /^\/run\/source\/(?:secrets|config)\/[A-Za-z][A-Za-z0-9_.-]{0,127}$/,
  );
}
function validateEnvelope(v, mode) {
  if (!["server", "observer"].includes(mode)) C.fail();
  C.fields(v, [
    "schema_version",
    "profile",
    "deployment_epoch",
    "bundle_ref",
    "ledger",
    ...(mode === "server" ? ["signing_key_file"] : ["observer_auth"]),
  ]);
  if (v.schema_version !== 1) C.fail();
  validateProfile(v.profile);
  C.text(v.deployment_epoch, /^[A-Za-z0-9_-]{1,128}$/);
  C.fields(v.bundle_ref, [
    "digest",
    "context",
    ...(mode === "server" ? ["root"] : []),
  ]);
  if (
    v.bundle_ref.digest !== v.profile.model_bundle_digest ||
    v.bundle_ref.context !== v.profile.context
  )
    C.fail();
  if (mode === "server") {
    if (v.bundle_ref.root !== "/run/source/bundle") C.fail();
    mountedFile(v.signing_key_file);
  } else {validateAuth(v.observer_auth);if(v.observer_auth.expected_principal===v.profile.auth.expected_principal)C.fail();}
  const l = v.ledger;
  C.fields(l, ["host", "port", "database", "user", "password_file", "ca_file"]);
  C.text(l.host, /^[a-z0-9][a-z0-9.-]{0,252}$/);
  C.integer(l.port, 1, 65535);
  for (const name of ["database", "user"])
    C.text(l[name], /^[a-z][a-z0-9_]{0,62}$/);
  mountedFile(l.password_file);
  mountedFile(l.ca_file);
  return C.copy(v);
}
function loadRuntime(mode) {
  C.validateEnvironment();
  const filename = process.env.GATEWAY_BIGQUERY_RUNTIME_CONFIG_FILE;
  mountedFile(filename);
  const config = validateEnvelope(
    JSON.parse(regularFile(filename).toString("utf8")),
    mode,
  );
  const l = config.ledger,
    password = regularFile(l.password_file, 4096).toString("utf8").trim(),
    ca = regularFile(l.ca_file, 65536).toString("utf8");
  if (
    !password ||
    /[\x00-\x1f\x7f]/.test(password) ||
    !ca.includes("-----BEGIN CERTIFICATE-----")
  )
    C.fail();
  const { Pool } = require("pg");
  const pool = new Pool({
    host: l.host,
    port: l.port,
    database: l.database,
    user: l.user,
    password,
    ssl: { ca, rejectUnauthorized: true },
    max: 2,
    connectionTimeoutMillis: 5000,
    idleTimeoutMillis: 10000,
    query_timeout: 6000,
    application_name:
      mode === "server" ? "spaceport-bq-execution" : "spaceport-bq-observer",
  });
  // Event errors cannot log raw database messages or credentials.
  pool.on("error", () => {});
  const ledger = new PostgresLedger(pool, {
    deploymentEpoch: config.deployment_epoch,
  });
  if (mode === "observer") return { config, ledger };
  const bundle = loadBundle(config.bundle_ref.root, config.bundle_ref.digest);
  const signingKey = regularFile(config.signing_key_file, 1024)
    .toString("utf8")
    .trim();
  C.text(signingKey, /^[A-Za-z0-9_-]{43}$/);
  return {
    config,
    ledger,
    signingKey,
    bundle,
  };
}
module.exports = {
  validateProfile,
  capacityKey,
  driverSource,
  regularFile,
  bundleDigest,
  loadBundle,
  validateBundle,
  validateEnvelope,
  loadRuntime,
};
