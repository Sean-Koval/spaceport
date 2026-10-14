'use strict';
// Reviewed server code. No request or manifest can replace this module or models.
const crypto = require('node:crypto');
const identifier = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const digestPattern = /^[a-f0-9]{64}$/;
const constraintMembers = new Set(['Orders.tenant_id', 'Orders.access_group', 'Orders.segment']);
const contextKeys = new Set(['iss', 'aud', 'iat', 'exp', 'principal_id', 'tenant_id',
  'policy_revision', 'model_bundle_digest', 'context', 'row_constraints']);
const dimensionNames = new Set(['id', 'tenant_id', 'access_group', 'segment', 'ordered_at']);
const filterOperators = new Set(['equals', 'notEquals', 'gt', 'gte', 'lt', 'lte', 'set',
  'notSet', 'afterOrOnDate', 'beforeDate']);
const queryKeys = new Set(['measures', 'dimensions', 'timeDimensions', 'filters', 'order',
  'limit', 'offset', 'timezone', 'ungrouped', 'renewQuery', 'cache', 'cacheMode', 'maskedMembers']);
function plain(value) { return value !== null && typeof value === 'object' &&
  Object.getPrototypeOf(value) === Object.prototype; }
function failContext() { throw new Error('Invalid gateway context'); }
function failQuery() { throw new Error('Invalid gateway query'); }
function fields(object, allowed, fail) {
  if (!plain(object) || Object.keys(object).some(key => !allowed.has(key))) fail();
}
function validateContext(value, { now, bundleDigest, endpointContext }) {
  fields(value, contextKeys, failContext);
  if (value.iss !== 'dpg-gateway' || value.aud !== 'dpg-cube' ||
      typeof value.principal_id !== 'string' || typeof value.tenant_id !== 'string' ||
      !identifier.test(value.principal_id) || !identifier.test(value.tenant_id) ||
      !Number.isSafeInteger(value.policy_revision) || value.policy_revision < 1 ||
      !Number.isSafeInteger(value.iat) || !Number.isSafeInteger(value.exp) ||
      value.iat > now || value.exp <= now || value.exp <= value.iat ||
      value.exp - value.iat > 60 || now - value.iat > 60 ||
      !digestPattern.test(value.model_bundle_digest || '') || value.model_bundle_digest !== bundleDigest ||
      value.context !== endpointContext || !['consumer', 'fixture_test'].includes(endpointContext) ||
      !Array.isArray(value.row_constraints) || value.row_constraints.length < 2 ||
      value.row_constraints.length > 3) failContext();
  const seen = new Set();
  for (const constraint of value.row_constraints) {
    fields(constraint, new Set(['member', 'operator', 'values']), failContext);
    if (!constraintMembers.has(constraint.member) || seen.has(constraint.member) ||
        !['eq', 'in'].includes(constraint.operator) || !Array.isArray(constraint.values) ||
        constraint.values.length < 1 || constraint.values.length > 100 ||
        (constraint.operator === 'eq' && constraint.values.length !== 1) ||
        constraint.values.some(item => typeof item !== 'string' || !identifier.test(item)) ||
        new Set(constraint.values).size !== constraint.values.length) failContext();
    seen.add(constraint.member);
  }
  const tenant = value.row_constraints.find(item => item.member === 'Orders.tenant_id');
  if (!tenant || tenant.operator !== 'eq' || tenant.values[0] !== value.tenant_id ||
      !seen.has('Orders.access_group')) failContext();
  return value;
}
function scopeId(context, options) {
  validateContext(context, options);
  const constraints = context.row_constraints.map(item => ({ member: item.member,
    operator: item.operator, values: [...item.values].sort() })).sort((a, b) => a.member.localeCompare(b.member));
  // Stable per trusted scope, never per request/time. Different same-tenant
  // entitlements partition actual pools, queues and result caches independently.
  return 'dpg_' + crypto.createHash('sha256').update(JSON.stringify([
    context.model_bundle_digest, context.context, context.principal_id,
    context.tenant_id, context.policy_revision, constraints])).digest('hex');
}
function rewrite(query, context, options) {
  validateContext(context, options);
  fields(query, queryKeys, failQuery);
  const cubes = options.endpointContext === 'fixture_test' ?
    new Set(['Orders', 'SlowOrders', 'TimeoutOrders', 'BoundaryOrders', 'Watermark']) :
    new Set(['Orders', 'Watermark']);
  const selected = new Set();
  function member(value, kind) {
    if (typeof value !== 'string') failQuery();
    const [cube, name, extra] = value.split('.');
    if (extra || !cubes.has(cube) || (cube === 'Watermark' ?
        !new Set(['loaded_at', 'tenant_id']).has(name) || kind === 'measure' :
        kind === 'measure' ? name !== 'revenue' : !dimensionNames.has(name))) failQuery();
    selected.add(cube);
    return value;
  }
  for (const [key, kind] of [['measures', 'measure'], ['dimensions', 'dimension']]) {
    if (query[key] !== undefined && (!Array.isArray(query[key]) || query[key].length > 20)) failQuery();
    for (const value of query[key] || []) member(value, kind);
  }
  if (query.timeDimensions !== undefined && (!Array.isArray(query.timeDimensions) || query.timeDimensions.length > 1)) failQuery();
  for (const td of query.timeDimensions || []) {
    fields(td, new Set(['dimension', 'granularity', 'dateRange']), failQuery);
    if (td.dateRange !== undefined) failQuery();
    member(td.dimension, 'dimension');
    if (!td.dimension.endsWith('.ordered_at') || td.granularity !== 'day') failQuery();
  }
  if (query.order !== undefined) {
    const order = Array.isArray(query.order) ? query.order : plain(query.order) ? Object.entries(query.order) : null;
    if (!order || order.length > 3) failQuery();
    for (const pair of order) {
      if (!Array.isArray(pair) || pair.length !== 2) failQuery();
      const [key, direction] = pair;
      member(key, key.endsWith('.revenue') ? 'measure' : 'dimension');
      if (!['asc', 'desc'].includes(direction)) failQuery();
    }
  }
  if (!selected.size || selected.size !== 1) failQuery();
  const cube = [...selected][0];
  let count = 0;
  function filter(value, depth = 0) {
    if (!plain(value) || depth > 5 || ++count > 100) failQuery();
    if ('and' in value || 'or' in value) {
      const keys = Object.keys(value);
      if (keys.length !== 1 || !['and', 'or'].includes(keys[0]) ||
          !Array.isArray(value[keys[0]]) || !value[keys[0]].length) failQuery();
      value[keys[0]].forEach(item => filter(item, depth + 1));
    } else {
      fields(value, new Set(['member', 'operator', 'values']), failQuery);
      member(value.member, 'dimension');
      if (!value.member.startsWith(cube + '.') || !filterOperators.has(value.operator)) failQuery();
      if (['set', 'notSet'].includes(value.operator)) {
        if (value.values !== undefined) failQuery();
      } else if (!Array.isArray(value.values) || !value.values.length || value.values.length > 100 ||
          value.values.some(item => typeof item !== 'string' || item.length > 128)) failQuery();
    }
  }
  if (query.filters !== undefined && (!Array.isArray(query.filters) || query.filters.length > 100)) failQuery();
  (query.filters || []).forEach(item => filter(item));
  if ((query.cache !== undefined) || (query.cacheMode !== undefined &&
      !['must-revalidate', 'no-cache'].includes(query.cacheMode)) ||
      (query.maskedMembers !== undefined) || selected.size !== 1 || (query.limit !== undefined &&
      (!Number.isSafeInteger(query.limit) || query.limit < 1 || query.limit > 1001)) ||
      (query.offset !== undefined && query.offset !== 0) ||
      (query.timezone !== undefined && !['UTC', 'America/New_York'].includes(query.timezone)) ||
      (query.ungrouped !== undefined && typeof query.ungrouped !== 'boolean') ||
      (query.renewQuery !== undefined && typeof query.renewQuery !== 'boolean')) failQuery();
  const mandatory = cube === 'Watermark' ? [{ member: 'Watermark.tenant_id',
    operator: 'equals', values: [context.tenant_id] }] :
    context.row_constraints.map(item => ({ member: item.member.replace(/^Orders\./, cube + '.'),
      operator: 'equals', values: [...item.values] }));
  return { ...query, limit: query.limit || 1001,
    filters: [{ and: [...(query.filters || []), ...mandatory] }] };
}
module.exports = { validateContext, scopeId, rewrite };
