'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const security = require('./security');
const { CubejsHandlerError } = require('@cubejs-backend/api-gateway');
function secret(filename) {
  const value = fs.readFileSync('/run/secrets/' + filename, 'utf8').trim();
  if (!/^[A-Za-z0-9_-]{43}$/.test(value)) throw new Error('Invalid mounted synthetic secret');
  return value;
}
function bundleDigest() {
  const files = ['cube.js', 'security.js', 'security.json', ...fs.readdirSync(path.join(__dirname, 'model')).sort().map(name => 'model/' + name)];
  const hash = crypto.createHash('sha256');
  for (const name of files) {
    const contents = fs.readFileSync(path.join(__dirname, name));
    hash.update(name + '\0' + contents.length + '\0');
    hash.update(contents);
  }
  return hash.digest('hex');
}
const contract = security.validateContract(JSON.parse(fs.readFileSync(path.join(__dirname, 'security.json'), 'utf8')));
const digest = bundleDigest();
if (process.env.GATEWAY_MODEL_BUNDLE_DIGEST !== digest) throw new Error('Pinned model bundle changed');
const endpointContext = process.env.GATEWAY_CUBE_CONTEXT;
if (!['consumer', 'fixture_test'].includes(endpointContext) || process.env.CUBEJS_DEV_MODE !== 'false' ||
    process.env.NODE_ENV !== 'production') throw new Error('Private production-mode Cube is required');
const signingKey = secret(endpointContext === 'fixture_test' ? 'cube_test_signing_key' : 'cube_signing_key');
const options = () => ({ now: Math.floor(Date.now() / 1000), bundleDigest: digest, endpointContext, contract });
module.exports = {
  apiSecret: signingKey,
  devServer: false,
  telemetry: false,
  scheduledRefreshTimer: false,
  scheduledRefreshContexts: async () => [],
  webSockets: false,
  schemaPath: 'model',
  cacheAndQueueDriver: 'memory',
  contextToAppId: () => 'dpg_bundle_' + digest,
  contextToOrchestratorId: ({ securityContext }) => security.scopeId(securityContext, options()),
  contextToApiScopes: async () => ['meta', 'data'],
  checkAuth: async (req, authorization) => {
    try {
      // Use the exact image's maintained JWT implementation; no hand-rolled JWT.
      const jwt = require('jsonwebtoken');
      const value = jwt.verify(authorization, signingKey, { algorithms: ['HS256'],
        issuer: 'dpg-gateway', audience: 'dpg-cube', clockTolerance: 0, maxAge: '60s' });
      security.validateContext(value, options());
      const pathname = (req.originalUrl || req.url || '').split('?')[0];
      if (!['/cubejs-api/v1/meta', '/cubejs-api/v1/load'].includes(pathname)) throw new Error();
      if (pathname.endsWith('/load') && !['must-revalidate', 'no-cache'].includes(
          req.method === 'POST' ? req.body?.cache : req.query?.cache)) throw new Error();
      if (pathname.endsWith('/load')) {
        const query = req.method === 'POST' ? req.body?.query : JSON.parse(req.query?.query);
        // Check before Cube's expression evaluation/normalization, then repeat
        // mandatory row enforcement through its independent queryRewrite hook.
        security.rewrite(query, value, options());
      }
      req.securityContext = value;
    } catch (_) {
      // The gateway must never surface tokens, raw verifier errors or queries.
      throw new CubejsHandlerError(403, 'Forbidden', 'Invalid gateway context');
    }
  },
  queryRewrite: (query, { securityContext }) => security.rewrite(query, securityContext, options()),
  driverFactory: () => ({ type: 'postgres', host: 'demo-db', port: 5432,
    database: 'gateway_demo', user: endpointContext === 'fixture_test' ? 'fixture_reader' : 'demo_reader',
    password: secret(endpointContext === 'fixture_test' ? 'fixture_reader' : 'demo_reader'),
    readOnly: true, executionTimeout: 25, maxPoolSize: 2, storeTimezone: 'UTC' }),
  // Disable sensitive SQL/error diagnostic payloads regardless of upstream defaults.
  logger: (message) => {
    if (['Query started', 'Query completed', 'Auth Error', 'Load Request Success',
      'Continue wait', 'Query error'].includes(message)) console.log(JSON.stringify({ event: message }));
  },
};
