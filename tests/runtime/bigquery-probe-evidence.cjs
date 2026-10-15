"use strict";
// This closed diagnostic vocabulary must match the private evidence parser.
const STAGES = Object.freeze([
  "cube-bigquery-package",
  "cube-bigquery-module-resolution",
  "cube-bigquery-package-versions",
  "cube-bigquery-ledger",
  "cube-bigquery-driver-construction",
  "cube-bigquery-query",
  "cube-bigquery-serialization",
  "cube-bigquery-result-hydration",
  "cube-bigquery-replay",
  "cube-bigquery-io-guard",
  "cube-bigquery-cleanup",
]);
function failureEvidence(write = (line) => process.stderr.write(line)) {
  let stage = STAGES[0];
  return Object.freeze({
    stage(value) {
      if (!STAGES.includes(value))
        throw new Error("Unknown package probe stage.");
      stage = value;
    },
    emit() {
      write(
        `GATEWAY_FAILURE_EVIDENCE ${JSON.stringify({ kind: "checkpoint", checkpoint: stage, exit_status: 1 })}\n`,
      );
    },
  });
}
module.exports = { STAGES, failureEvidence };
