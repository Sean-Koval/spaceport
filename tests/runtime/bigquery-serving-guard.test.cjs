"use strict";
const test = require("node:test"),
  assert = require("node:assert/strict");
const guard = require("./bigquery-serving-guard.cjs").installGuard({
  allowedSecretFiles: ["/tmp/spaceport-guard-secrets/runtime"],
  loopbackListeners: true,
});
test("fixture guard rejects unrelated loopback TCP HTTP and fetch before any outbound connection", () => {
  for (const send of [
    () => require("node:net").connect(12345, "127.0.0.1"),
    () => require("node:http").get("http://127.0.0.1:12345/"),
    () => fetch("http://localhost:12345/"),
  ])
    assert.throws(send, /Unapproved external/);
  assert.deepEqual(guard.counts, {
    external: 3,
    authentication: 0,
    credentialRead: 0,
  });
});
test("closed server ports lose their outbound allowance", () => {
  guard.allowLoopbackPort(12346);
  guard.revokeLoopbackPort(12346);
  assert.throws(
    () => require("node:net").connect(12346, "127.0.0.1"),
    /Unapproved external/,
  );
  assert.equal(guard.counts.external, 4);
});

test("local fixture owner keys remain outside the serving secret allowlist", () => {
  assert.throws(
    () =>
      require("node:fs").readFileSync(
        "/tmp/spaceport-guard-secrets/registry_owner",
      ),
    /Unapproved external/,
  );
  assert.equal(guard.counts.credentialRead, 1);
});

test("optional listener containment forces only numeric ephemeral binds onto loopback", async () => {
  const net = require("node:net");
  for (const args of [
    [4000],
    [0, "0.0.0.0"],
    ["/tmp/forbidden-source.sock"],
    [{ port: 0, host: "127.0.0.1" }],
  ])
    assert.throws(
      () => net.createServer().listen(...args),
      /Unapproved external/,
    );
  const server = net.createServer();
  await new Promise((resolve) => server.listen(0, resolve));
  assert.equal(server.address().address, "127.0.0.1");
  await new Promise((resolve) => server.close(resolve));
});
