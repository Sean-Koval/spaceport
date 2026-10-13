'use strict';
// Run only inside the exact pinned Cube image with its real dependencies and
// mounted synthetic configuration. No copied/mock HTTP-status mapper is used.
const assert = require('node:assert/strict');
const { test } = require('node:test');
const http = require('node:http');
const express = require('express');
const jwt = require('jsonwebtoken');
const { ApiGateway } = require('@cubejs-backend/api-gateway');
const config = require(process.env.GATEWAY_CUBE_CONFIG_MODULE || '../../cube/cube.js');
const unreachable = async () => { throw new Error('Upstream must not be called by authentication regression'); };
// Construct the real release gateway once. The compiler/orchestrator are never
// invoked: this regression targets its actual checkAuth HTTP-status wrapper.
const gateway = new ApiGateway(config.apiSecret, unreachable, unreachable, () => {}, {
  devServer: false, checkAuth: config.checkAuth, contextToApiScopes: config.contextToApiScopes,
});

function request(url, options = {}) {
  return fetch(url, { ...options, signal: AbortSignal.timeout(2000) });
}

function token(patch = {}) {
  const now = Math.floor(Date.now() / 1000);
  return jwt.sign({ iss: 'dpg-gateway', aud: 'dpg-cube', iat: now, exp: now + 60,
    principal_id: 'demo-north', tenant_id: 'north', policy_revision: 1,
    model_bundle_digest: process.env.GATEWAY_MODEL_BUNDLE_DIGEST,
    context: process.env.GATEWAY_CUBE_CONTEXT, row_constraints: [
      { member: 'Orders.tenant_id', operator: 'eq', values: ['north'] },
      { member: 'Orders.access_group', operator: 'in', values: ['north'] },
    ], ...patch }, config.apiSecret, { algorithm: 'HS256' });
}

async function withWrapper(check) {
  const app = express();
  app.use(express.json());
  app.use(gateway.checkAuth);
  app.use((_req, res) => res.status(200).json({ reached_handler: true }));
  const server = http.createServer(app);
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  try {
    await check('http://127.0.0.1:' + server.address().port);
  } finally {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
}

test('actual pinned auth wrapper returns redacted 403 for invalid contexts', { timeout: 15000 }, async () => {
  await withWrapper(async base => {
    const now = Math.floor(Date.now() / 1000);
    const authorizations = [undefined, 'not-a-valid-token',
      token({ iat: now - 62, exp: now - 2 }), token({ model_bundle_digest: '0'.repeat(64) }),
      token({ tenant_id: 'south' }), token({ row_constraints: [] })];
    for (const value of authorizations) {
      const response = await request(base + '/cubejs-api/v1/meta', {
        headers: value === undefined ? {} : { Authorization: 'Bearer ' + value },
      });
      assert.equal(response.status, 403);
      assert.deepEqual(await response.json(), { error: 'Invalid gateway context' });
    }
  });
});

test('actual pinned auth wrapper permits valid context and rejects paths/cache/query', { timeout: 15000 }, async () => {
  await withWrapper(async base => {
    const headers = { Authorization: 'Bearer ' + token(), 'Content-Type': 'application/json' };
    const valid = await request(base + '/cubejs-api/v1/meta', { headers });
    assert.equal(valid.status, 200);
    assert.deepEqual(await valid.json(), { reached_handler: true });
    for (const [path, body] of [
      ['/cubejs-api/v1/sql', { query: { dimensions: ['Orders.id'] } }],
      ['/cubejs-api/v1/load', { query: { dimensions: ['Orders.id'] } }],
      ['/cubejs-api/v1/load', { cache: 'stale-if-slow', query: { dimensions: ['Orders.id'] } }],
      ['/cubejs-api/v1/load', { cache: 'must-revalidate', query: { dimensions: ['Orders.unapproved'] } }],
    ]) {
      const response = await request(base + path, { method: 'POST', headers, body: JSON.stringify(body) });
      assert.equal(response.status, 403);
      assert.deepEqual(await response.json(), { error: 'Invalid gateway context' });
    }
  });
});
