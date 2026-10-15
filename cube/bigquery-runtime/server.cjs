"use strict";
const C = require("./contract.cjs");
const S = require("./security.cjs");
const R = require("./config.cjs");
const { createServingDriver } = require("./driver.cjs");
const { PostgresLedger } = require("./postgres-ledger.cjs");
function validateServerEnvironment() {
  C.validateEnvironment();
  const allowed = new Set([
    "CUBEJS_DEV_MODE",
    "CUBEJS_DB_TYPE",
    "CUBEJS_EXTERNAL_DEFAULT",
  ]);
  for (const name of Object.keys(process.env))
    if (
      (name.startsWith("CUBEJS_") && !allowed.has(name)) ||
      name.startsWith("CUBE_JS_")
    )
      C.fail();
  if (
    process.env.PORT !== undefined &&
    (!/^(0|[1-9][0-9]{0,4})$/.test(process.env.PORT) ||
      Number(process.env.PORT) > 65535)
  )
    C.fail();
  if (
    process.env.NODE_ENV !== "production" ||
    process.env.CUBEJS_DEV_MODE !== "false" ||
    (process.env.CUBEJS_DB_TYPE !== undefined &&
      process.env.CUBEJS_DB_TYPE !== "bigquery") ||
    (process.env.CUBEJS_EXTERNAL_DEFAULT !== undefined &&
      process.env.CUBEJS_EXTERNAL_DEFAULT !== "false")
  )
    C.fail();
}
function createCubeOptions({ profile, bundle, signingKey, ledger }) {
  profile = C.immutable(profile);
  bundle = R.validateBundle(bundle);
  validateServerEnvironment();
  R.validateProfile(profile);
  S.validateContract(bundle.contract);
  C.text(signingKey, /^[A-Za-z0-9_-]{43}$/);
  if (
    !(ledger instanceof PostgresLedger) ||
    bundle.digest !== profile.model_bundle_digest ||
    process.env.NODE_ENV !== "production" ||
    process.env.CUBEJS_DEV_MODE !== "false"
  )
    C.fail();
  const source = R.driverSource(profile),
    definitionDigest = C.digest(profile);
  const opts = () => ({
    now: Math.floor(Date.now() / 1000),
    bundleDigest: bundle.digest,
    endpointContext: profile.context,
    contract: bundle.contract,
    sourceId: profile.source_id,
    sourceRevision: profile.revision,
    definitionDigest,
    capacityKey: R.capacityKey(profile),
  });
  return {
    apiSecret: signingKey,
    devServer: false,
    telemetry: false,
    scheduledRefreshTimer: false,
    scheduledRefreshContexts: async () => [],
    webSockets: false,
    schemaPath: "model",
    repositoryFactory: () =>
      Object.freeze({ dataSchemaFiles: async () => C.copy(bundle.files) }),
    cacheAndQueueDriver: "memory",
    allowNodeRequire: false,
    livePreview: false,
    compilerCacheSize: 32,
    maxCompilerCacheKeepAlive: 120000,
    updateCompilerCacheKeepAlive: false,
    contextToAppId: ({ securityContext }) =>
      S.compilerId(securityContext, opts()),
    contextToOrchestratorId: ({ securityContext }) =>
      S.orchestratorId(securityContext, opts()),
    contextToApiScopes: async () => ["meta", "data"],
    checkAuth: async (req, authorization) => {
      try {
        const jwt = require("jsonwebtoken");
        const context = jwt.verify(authorization, signingKey, {
          algorithms: ["HS256"],
          issuer: "dpg-gateway",
          audience: "dpg-cube-source-v1",
          clockTolerance: 0,
          maxAge: "60s",
        });
        S.validateContext(context, opts());
        S.validateRequestId(req, context);
        const pathname = (req.originalUrl || req.url || "").split("?")[0];
        if (pathname === "/cubejs-api/v1/load") {
          if (req.method !== "POST") C.fail();
          C.fields(req.body, ["query", "cache"]);
          if (!["must-revalidate", "no-cache"].includes(req.body.cache))
            C.fail();
          S.validateRawQuery(req.body.query, context, opts());
        } else if (pathname !== "/cubejs-api/v1/meta" || req.method !== "GET")
          C.fail();
        const current = await ledger.checkAttempt(context.source_attempt);
        if (
          current.allowed !== true ||
          current.context_digest !== context.source_attempt.context_digest ||
          Date.parse(current.deadline_at) <= Date.now()
        )
          C.fail();
        req.securityContext = C.copy(context);
      } catch (_) {
        const { CubejsHandlerError } = require("@cubejs-backend/api-gateway");
        throw new CubejsHandlerError(
          403,
          "Forbidden",
          "Invalid source context",
        );
      }
    },
    queryRewrite: (query, { securityContext }) =>
      S.rewrite(query, securityContext, opts()),
    driverFactory: ({ securityContext }) => {
      S.validateContext(securityContext, opts());
      return createServingDriver({
        source,
        context: securityContext.source_attempt,
        ledger,
      });
    },
    externalDriverFactory: () => C.fail(),
    orchestratorOptions: {
      continueWaitTimeout: 1,
      skipExternalCacheAndQueue: true,
      queryCacheOptions: {
        backgroundRenew: false,
        refreshKeyRenewalThreshold: 1,
        queueOptions: {
          concurrency: 2,
          continueWaitTimeout: 1,
          executionTimeout: 120,
          orphanedTimeout: 120,
          heartBeatInterval: 5,
        },
      },
    },
    // Upstream params contain SQL, query values, tokens, and provider errors.
    // No general logger/sink or user-selected event payload is forwarded.
    logger: () => {},
  };
}
function createServer(options) {
  validateServerEnvironment();
  if (
    process.env.CUBEJS_DB_TYPE !== undefined &&
    process.env.CUBEJS_DB_TYPE !== "bigquery"
  )
    C.fail();
  process.env.CUBEJS_DB_TYPE = "bigquery";
  process.env.CUBEJS_EXTERNAL_DEFAULT = "false";
  if (
    require("@cubejs-backend/server/package.json").version !== "1.8.5" ||
    require("@cubejs-backend/server-core/package.json").version !== "1.8.5"
  )
    C.fail();
  const CubejsServer =
    require("@cubejs-backend/server").default ||
    require("@cubejs-backend/server");
  // The maintained package can load .env during import. Recheck after import.
  validateServerEnvironment();
  return new CubejsServer(options);
}
async function main() {
  const { config, bundle, signingKey, ledger } = R.loadRuntime("server");
  const server = createServer(
    createCubeOptions({ profile: config.profile, bundle, signingKey, ledger }),
  );
  await server.listen();
  let closing = false;
  const close = async () => {
    if (closing) return;
    closing = true;
    try {
      await server.shutdown("SIGTERM");
      await ledger.close();
    } finally {
      process.exit(0);
    }
  };
  process.once("SIGTERM", close);
  process.once("SIGINT", close);
}
if (require.main === module)
  main().catch(() => {
    process.stderr.write("BigQuery runtime failed.\n");
    process.exitCode = 1;
  });
module.exports = {
  validateServerEnvironment,
  createCubeOptions,
  createServer,
  main,
};
