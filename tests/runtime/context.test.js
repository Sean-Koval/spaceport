'use strict';
const assert = require('node:assert/strict');
const { test } = require('node:test');
const runtime = require(process.env.GATEWAY_SECURITY_MODULE || '../../cube/security.js');
const digest = 'a'.repeat(64);
const now = 1700000000;
function context(patch = {}) {
  return { iss: 'dpg-gateway', aud: 'dpg-cube', iat: now, exp: now + 60,
    principal_id: 'demo-north', tenant_id: 'north', policy_revision: 1,
    model_bundle_digest: digest, context: 'consumer', row_constraints: [
      { member: 'Orders.tenant_id', operator: 'eq', values: ['north'] },
      { member: 'Orders.access_group', operator: 'in', values: ['north'] },
    ], ...patch };
}
const options = { now, bundleDigest: digest, endpointContext: 'consumer' };
test('accepts only a signed-context shape with matching tenant and short expiry', () => {
  assert.equal(runtime.validateContext(context(), options).principal_id, 'demo-north');
});
test('rejects absent, expired, future and overlong contexts', () => {
  for (const value of [undefined, {}, context({ exp: now }), context({ iat: now + 1 }),
    context({ exp: now + 61 }), context({ exp: 'tomorrow' }), context({ iat: now - 100 })]) {
    assert.throws(() => runtime.validateContext(value, options), /Invalid gateway context/);
  }
});
test('rejects context without independent tenant and entitlement constraints', () => {
  for (const row_constraints of [[], context().row_constraints.slice(0, 1),
    [{ member: 'Orders.tenant_id', operator: 'eq', values: ['south'] }, context().row_constraints[1]],
    [...context().row_constraints, { member: 'Orders.segment', operator: 'eq', values: [null] }],
    [...context().row_constraints, { member: 'Orders.segment', operator: 'gt', values: ['A'] }]]) {
    assert.throws(() => runtime.validateContext(context({ row_constraints }), options), /Invalid gateway context/);
  }
});
test('rejects other issuer audience bundle endpoint unknown fields and prototype-shaped input', () => {
  for (const value of [context({ iss: 'other' }), context({ aud: 'other' }),
    context({ model_bundle_digest: 'b'.repeat(64) }), context({ context: 'fixture_test' }),
    context({ roles: ['admin'] }), context({ policy_revision: 1.5 }),
    JSON.parse(JSON.stringify(context()).replace('"principal_id"', '"__proto__"'))]) {
    assert.throws(() => runtime.validateContext(value, options), /Invalid gateway context/);
  }
});
test('mandatory predicates remain ANDed outside hostile client OR filters', () => {
  const query = { dimensions: ['Orders.id'], filters: [{ or: [
    { member: 'Orders.tenant_id', operator: 'equals', values: ['south'] },
    { member: 'Orders.segment', operator: 'set' },
  ] }] };
  const rewritten = runtime.rewrite(query, context(), options);
  assert.deepEqual(rewritten.filters[0].and.slice(-2), [
    { member: 'Orders.tenant_id', operator: 'equals', values: ['north'] },
    { member: 'Orders.access_group', operator: 'equals', values: ['north'] },
  ]);
  assert.equal(query.filters.length, 1);
  assert.equal(query.filters[0].or.length, 2);
});
test('watermark query independently enforces tenant and cannot join Orders', () => {
  assert.deepEqual(runtime.rewrite({ dimensions: ['Watermark.loaded_at'] }, context(), options).filters,
    [{ and: [{ member: 'Watermark.tenant_id', operator: 'equals', values: ['north'] }] }]);
  assert.throws(() => runtime.rewrite({ dimensions: ['Watermark.loaded_at', 'Orders.id'] }, context(), options));
});
test('cache isolation distinguishes tenant principal policy and differing entitlements', () => {
  const baseline = runtime.scopeId(context(), options);
  const other = [context({ principal_id: 'restricted-north' }), context({ policy_revision: 2 }),
    context({ row_constraints: [...context().row_constraints,
      { member: 'Orders.segment', operator: 'eq', values: ['A'] }] })];
  for (const value of other) assert.notEqual(runtime.scopeId(value, options), baseline);
  assert.equal(runtime.scopeId(context({ row_constraints: [...context().row_constraints].reverse() }), options), baseline);
  assert.equal(runtime.scopeId(context({ iat: now - 1, exp: now + 59 }), options), baseline);
});
test('rejects expressions unknown members SQL joins and unsafe query controls', () => {
  const invalid = [{ measures: [{ expression: 'select 1' }] }, { dimensions: ['Orders.missing'] },
    { dimensions: ['Orders.id'], filters: [{ member: 'Orders.id', operator: 'sql', values: ['x'] }] },
    { dimensions: ['Orders.id'], limit: 1002 }, { dimensions: ['Orders.id'], subqueryJoins: [{}] },
    { dimensions: ['Orders.id'], filters: [{ or: [], and: [] }] },
    { dimensions: ['Orders.id'], timeDimensions: [{ dimension: 'Orders.ordered_at', granularity: 'minute' }] }];
  for (const query of invalid) assert.throws(() => runtime.rewrite(query, context(), options), /Invalid gateway query/);
});
test('accepts the exact release-normalized query with safe internal defaults', () => {
  const query = { measures: ['Orders.revenue'], dimensions: ['Orders.segment'],
    timeDimensions: [{ dimension: 'Orders.ordered_at', granularity: 'day', dateRange: undefined }],
    filters: [], order: [['Orders.ordered_at', 'asc']], limit: 1001, timezone: 'UTC',
    cache: undefined, cacheMode: 'must-revalidate', maskedMembers: undefined };
  assert.equal(runtime.rewrite(query, context(), options).filters[0].and.length, 2);
  for (const change of [{ cacheMode: 'stale-if-slow' }, { maskedMembers: [{ member: 'Orders.id' }] },
    { timeDimensions: [{ dimension: 'Orders.ordered_at', granularity: 'day', dateRange: ['yesterday'] }] }]) {
    assert.throws(() => runtime.rewrite({ ...query, ...change }, context(), options), /Invalid gateway query/);
  }
});
test('numeric and overlong identity claims cannot pass identifier validation', () => {
  for (const value of [context({ principal_id: 123 }), context({ tenant_id: 123 }),
    context({ principal_id: 'x'.repeat(129) })]) {
    assert.throws(() => runtime.validateContext(value, options), /Invalid gateway context/);
  }
});
test('the same tenant with a different valid entitlement group has its own scope', () => {
  const full = context();
  const narrow = context({ row_constraints: [full.row_constraints[0],
    { member: 'Orders.access_group', operator: 'in', values: ['north-A'] }] });
  assert.notEqual(runtime.scopeId(full, options), runtime.scopeId(narrow, options));
});
