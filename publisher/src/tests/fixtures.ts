import revenue from '../../../fixtures/products/revenue.json';
export const manifest = {
  ...revenue,
  limits: {
    max_rows: 1000,
    max_bytes: 1048576,
    page_size: 200,
    per_principal_concurrency: 2,
    deployment_concurrency: 10,
    max_date_range_days: 366,
    sync_wait_seconds: 5,
    execution_timeout_seconds: 120,
  },
};
export const product = {
  product_id: 'revenue',
  version: '1.0.0',
  digest: 'a'.repeat(64),
  name: 'Daily revenue',
  status: 'published',
  description: 'Daily sum of order amounts',
  owner: 'analytics',
  domain: 'finance',
  examples: revenue.examples,
  links: [],
  inputs: revenue.inputs,
  outputs: revenue.outputs,
  filters: revenue.filters,
  sort: revenue.sort,
  units: revenue.units,
  grain: revenue.grain,
  aggregation: revenue.aggregation,
  timezone: revenue.timezone,
  date_boundaries: revenue.date_boundaries,
  freshness: revenue.freshness,
  provenance: {
    binding_id: 'revenue-orders',
    binding_revision: 1,
    adapter_revision: 'cube-rest-v1',
    model_bundle_digest: 'b'.repeat(64),
    links: [],
  },
  partial: false,
  limits: manifest.limits,
};
export function json(value: unknown, status = 200) {
  return Response.json(value, { status });
}
