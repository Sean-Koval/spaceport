"use strict";
const test = require("node:test"),
  assert = require("node:assert/strict");
const { firstJobTimeout } = require("../../cube/bigquery-runtime/driver.cjs");
test("first intent leaves six seconds of wall and monotonic admission headroom", () => {
  assert.equal(firstJobTimeout(30000, 31000, 30100, 1000, 100), 24000);
  assert.equal(firstJobTimeout(25000, 121000, 120100, 1000, 100), 25000);
  assert.equal(firstJobTimeout(30000, 31000, 20100, 1000, 100), 14000);
  assert.equal(firstJobTimeout(30000, 21000, 30100, 1000, 100), 14000);
});
test("first intent rejects insufficient retained time instead of admitting a refreshed timeout", () => {
  for (const remaining of [-1, 0, 1, 6000, 6000.99])
    assert.throws(() =>
      firstJobTimeout(30000, 1000 + remaining, 100 + remaining, 1000, 100),
    );
  assert.equal(firstJobTimeout(30000, 7001, 6101, 1000, 100), 1);
});
