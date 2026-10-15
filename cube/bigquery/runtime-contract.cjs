"use strict";
// Internal offline source-control contract. None of these objects authorize a live query.
const crypto = require("node:crypto");
const HEX = /^[a-f0-9]{64}$/;
const UUID =
  /^[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/;
function fail(message = "BigQuery control input is invalid.") {
  throw new Error(message);
}
function plain(value) {
  return (
    value !== null &&
    typeof value === "object" &&
    Object.getPrototypeOf(value) === Object.prototype
  );
}
function exact(value, keys) {
  if (
    !plain(value) ||
    Object.keys(value).length !== keys.length ||
    keys.some((k) => !Object.hasOwn(value, k))
  )
    fail();
}
function integer(value, min, max) {
  if (!Number.isSafeInteger(value) || value < min || value > max) fail();
  return value;
}
function text(value, pattern, max = 128) {
  if (typeof value !== "string" || value.length > max || !pattern.test(value))
    fail();
  return value;
}
function bytes(value) {
  text(value, /^[1-9][0-9]{0,18}$/, 19);
  if (BigInt(value) > 9223372036854775807n) fail();
  return value;
}
function canonical(value) {
  function sort(item, depth) {
    if (depth > 16) fail();
    if (item === null || typeof item === "boolean" || typeof item === "string")
      return item;
    if (typeof item === "number" && Number.isSafeInteger(item)) return item;
    if (Array.isArray(item)) return item.map((v) => sort(v, depth + 1));
    if (!plain(item)) fail();
    return Object.fromEntries(
      Object.keys(item)
        .sort()
        .map((k) => [k, sort(item[k], depth + 1)]),
    );
  }
  return JSON.stringify(sort(value, 0));
}
const digest = (value) =>
  crypto.createHash("sha256").update(canonical(value)).digest("hex");
const copy = (value) => JSON.parse(canonical(value));
function identity(value) {
  exact(value, ["deploymentId", "ledgerId"]);
  text(value.deploymentId, /^[a-z][a-z0-9_-]{0,63}$/);
  text(value.ledgerId, UUID);
  return copy(value);
}
function limits(value) {
  exact(value, [
    "maxJournalBytes",
    "maxRecordBytes",
    "maxExecutions",
    "maxJobsPerExecution",
  ]);
  integer(value.maxJournalBytes, 4096, 8388608);
  integer(value.maxRecordBytes, 1024, 65536);
  integer(value.maxExecutions, 1, 1000);
  integer(value.maxJobsPerExecution, 1, 8);
  if (value.maxRecordBytes > value.maxJournalBytes) fail();
  return copy(value);
}
function permit(value) {
  exact(value, [
    "executionId",
    "generation",
    "scopeDigest",
    "queryDigest",
    "bindingDigest",
    "bundleDigest",
    "sourceDigest",
    "deadlineMs",
    "maxJobs",
    "perJobBytes",
    "totalBytes",
  ]);
  text(value.executionId, UUID);
  integer(value.generation, 1, 2147483647);
  integer(value.deadlineMs, 1, 8640000000000000);
  for (const key of [
    "scopeDigest",
    "queryDigest",
    "bindingDigest",
    "bundleDigest",
    "sourceDigest",
  ])
    text(value[key], HEX);
  integer(value.maxJobs, 1, 8);
  bytes(value.perJobBytes);
  bytes(value.totalBytes);
  if (BigInt(value.perJobBytes) > BigInt(value.totalBytes)) fail();
  return copy(value);
}
function jobConfig(value) {
  exact(value, [
    "projectId",
    "location",
    "queryHash",
    "parametersHash",
    "maximumBytesBilled",
    "jobTimeoutMs",
  ]);
  text(value.projectId, /^[a-z][a-z0-9-]{4,28}[a-z0-9]$/);
  text(value.location, /^(?:US|EU|[a-z]+(?:-[a-z]+)+[1-9][0-9]?)$/, 64);
  text(value.queryHash, HEX);
  text(value.parametersHash, HEX);
  bytes(value.maximumBytesBilled);
  integer(value.jobTimeoutMs, 1, 120000);
  return copy(value);
}
module.exports = {
  fail,
  plain,
  exact,
  integer,
  text,
  bytes,
  canonical,
  digest,
  copy,
  identity,
  limits,
  permit,
  jobConfig,
  HEX,
  UUID,
};
