"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const {
  failureEvidence,
  STAGES,
} = require("../runtime/bigquery-probe-evidence.cjs");

test("package probe exposes only fixed checkpoint failure receipts", () => {
  const lines = [];
  const evidence = failureEvidence((line) => lines.push(line));
  for (const stage of STAGES) {
    evidence.stage(stage);
    evidence.emit();
  }
  assert.equal(lines.length, 11);
  for (let i = 0; i < lines.length; i++) {
    assert.equal(
      lines[i],
      `GATEWAY_FAILURE_EVIDENCE ${JSON.stringify({ kind: "checkpoint", checkpoint: STAGES[i], exit_status: 1 })}\n`,
    );
  }
});

test("unknown stage fails without copying arbitrary content into receipts", () => {
  const lines = [];
  const evidence = failureEvidence((line) => lines.push(line));
  assert.throws(() => evidence.stage("SELECT secret FROM credentials"));
  evidence.emit(new Error("password=fixture-secret"));
  assert.equal(lines.length, 1);
  assert.equal(
    lines[0],
    'GATEWAY_FAILURE_EVIDENCE {"kind":"checkpoint","checkpoint":"cube-bigquery-package","exit_status":1}\n',
  );
  assert.equal(lines.join("").includes("secret"), false);
});
