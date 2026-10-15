"use strict";
// Install before all Cube/Google/pg imports. Only the test's loopback HTTP and
// exact disposable registry destination may open sockets. No cloud auth exists.
function installGuard({
  registry,
  allowedSecretFiles = [],
  loopbackListeners = false,
} = {}) {
  const marker = Symbol.for("spaceport.bigquery.serving.test.guard");
  if (globalThis[marker]) return globalThis[marker];
  const assert = require("node:assert/strict"),
    fs = require("node:fs"),
    net = require("node:net"),
    dns = require("node:dns"),
    http = require("node:http"),
    https = require("node:https");
  const counts = { external: 0, authentication: 0, credentialRead: 0 };
  const secretDirectories = new Set(
    allowedSecretFiles.map((file) => require("node:path").dirname(file)),
  );
  const secretFiles = new Set(allowedSecretFiles),
    loopbackPorts = new Set();
  const denied = (kind) => () => {
    counts[kind]++;
    throw new Error("Unapproved external I/O in serving fixture.");
  };
  const deny = denied("external"),
    denyAuth = denied("authentication");
  if (loopbackListeners) {
    const listen = net.Server.prototype.listen;
    net.Server.prototype.listen = function (...args) {
      if (
        args[0] !== 0 ||
        args.length > 2 ||
        (args[1] !== undefined && typeof args[1] !== "function")
      )
        return deny();
      return listen.call(
        this,
        { port: 0, host: "127.0.0.1" },
        ...(args[1] ? [args[1]] : []),
      );
    };
  }
  function allowed(host, port) {
    host = String(host || "localhost");
    return (
      (["127.0.0.1", "::1", "localhost"].includes(host) &&
        loopbackPorts.has(Number(port))) ||
      (registry && host === registry.host && Number(port) === registry.port)
    );
  }
  function connectArguments(args) {
    let first = args[0];
    if (Array.isArray(first)) first = first[0];
    if (first && typeof first === "object")
      return [first.host || first.hostname, first.port];
    if (typeof first === "number")
      return [typeof args[1] === "string" ? args[1] : "localhost", first];
    return [null, null];
  }
  const connect = net.Socket.prototype.connect;
  net.Socket.prototype.connect = function (...args) {
    const [host, port] = connectArguments(args);
    if (port === undefined || port === null || !allowed(host, port))
      return deny();
    return connect.apply(this, args);
  };
  function httpAllowed(args) {
    try {
      const v = args[0];
      if (typeof v === "string" || v instanceof URL) {
        const url = new URL(v);
        return (
          url.protocol === "http:" && allowed(url.hostname, url.port || 80)
        );
      }
      return v && allowed(v.hostname || v.host, v.port || 80);
    } catch {
      return false;
    }
  }
  for (const name of ["request", "get"]) {
    const original = http[name];
    http[name] = function (...args) {
      if (!httpAllowed(args)) return deny();
      return original.apply(this, args);
    };
    https[name] = deny;
  }
  require("node:tls").connect = deny;
  require("node:http2").connect = deny;
  const dgram = require("node:dgram");
  dgram.createSocket = deny;
  dgram.Socket.prototype.connect = deny;
  dgram.Socket.prototype.send = deny;
  const originalFetch = globalThis.fetch;
  if (originalFetch)
    globalThis.fetch = (...args) => {
      if (!httpAllowed(args)) return deny();
      return originalFetch(...args);
    };
  for (const api of [
    dns,
    dns.promises,
    dns.Resolver.prototype,
    dns.promises.Resolver.prototype,
  ])
    for (const name of Object.getOwnPropertyNames(api))
      if (/^(lookup|resolve)/.test(name) && typeof api[name] === "function") {
        const original = api[name];
        api[name] = function (host, ...args) {
          if (!["localhost", "127.0.0.1", "::1", registry?.host].includes(host))
            return deny();
          return original.call(this, host, ...args);
        };
      }
  const denyRead = denied("credentialRead");
  for (const [api, names] of [
    [fs, ["readFileSync", "readFile", "openSync", "open", "createReadStream"]],
    [fs.promises, ["readFile", "open"]],
  ])
    for (const name of names) {
      const original = api[name];
      api[name] = function (filename, ...args) {
        const v =
          filename instanceof URL
            ? require("node:url").fileURLToPath(filename)
            : String(filename);
        if (
          (secretDirectories.has(require("node:path").dirname(v)) ||
            v.startsWith("/run/secrets/") ||
            v.startsWith("/run/source/secrets/") ||
            v.includes("/.config/gcloud/") ||
            v.endsWith("/application_default_credentials.json")) &&
          !secretFiles.has(v)
        )
          return denyRead();
        return original.call(this, filename, ...args);
      };
    }
  // All imports above have already been guarded. A constructor may create only
  // explicit lazy Compute credentials; any actual credential lookup is a failure.
  const { Compute, GoogleAuth } = require("google-auth-library");
  for (const prototype of [Compute.prototype, GoogleAuth.prototype])
    for (const name of [
      "getClient",
      "getCredentials",
      "getRequestHeaders",
      "getAccessToken",
      "authorizeRequest",
      "refreshTokenNoCache",
      "fetchIdToken",
    ])
      if (typeof prototype[name] === "function") prototype[name] = denyAuth;
  const guard = Object.freeze({
    counts,
    allowLoopbackPort(port) {
      assert.ok(Number.isInteger(port) && port > 0 && port <= 65535);
      loopbackPorts.add(port);
    },
    revokeLoopbackPort(port) {
      loopbackPorts.delete(port);
    },
    assertClean() {
      assert.deepEqual(counts, {
        external: 0,
        authentication: 0,
        credentialRead: 0,
      });
    },
  });
  globalThis[marker] = guard;
  return guard;
}
module.exports = { installGuard };
