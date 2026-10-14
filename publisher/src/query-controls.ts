import type { Model } from './api';

export type Scalar = string | number | boolean | null;
export type QueryDraft = {
  inputs: Record<string, Scalar>;
  filters: Model<'QueryFilter'>[];
  sort: Model<'QuerySort'>[];
};
export type QueryIntent = {
  readonly fingerprint: string;
  readonly request: Model<'QueryRequest'>;
};
// QueryRequest / FilterDefinition bounds. The gateway remains authoritative.
export const MAX_FILTERS = 20;
export const MAX_LIST_VALUES = 100;
export const MAX_SORT_FIELDS = 5;

export function initialValue(field: Model<'FieldSchema'>): Scalar {
  if (field.enum?.length) return field.enum[0];
  if (field.type === 'boolean') return false;
  if (field.type === 'number' || field.type === 'integer') return 0;
  return '';
}

export function newFilter(
  product: Model<'ProductDescription'>,
  field: string,
  operator = product.filters[field].operators[0],
): Model<'QueryFilter'> {
  if (operator === 'is_null') return { field, operator };
  const value = initialValue(product.outputs[field]);
  return { field, operator, value: operator === 'in' || operator === 'not_in' ? [value] : value };
}

/** Form assistance for supplied values only; omitted defaults and access rules belong to the server. */
export function queryErrors(product: Model<'ProductDescription'>, draft: QueryDraft): string[] {
  const errors: string[] = [];
  if (Object.keys(draft.inputs).length > 50) errors.push('Use at most 50 inputs.');
  for (const [name, value] of Object.entries(draft.inputs)) {
    const field = Object.hasOwn(product.inputs, name) ? product.inputs[name] : undefined;
    if (!field || !validValue(field, value)) errors.push(`Check the value for input ${name}.`);
  }
  if (draft.filters.length > MAX_FILTERS) errors.push(`Use at most ${MAX_FILTERS} filters.`);
  draft.filters.forEach((filter, index) => {
    const definition = Object.hasOwn(product.filters, filter.field)
      ? product.filters[filter.field]
      : undefined;
    const field = Object.hasOwn(product.outputs, filter.field)
      ? product.outputs[filter.field]
      : undefined;
    let valid = !!definition && !!field && definition.operators.includes(filter.operator);
    if (valid && filter.operator === 'is_null')
      valid = filter.value === undefined || filter.value === null;
    else if (valid && (filter.operator === 'in' || filter.operator === 'not_in'))
      valid =
        Array.isArray(filter.value) &&
        filter.value.length >= 1 &&
        filter.value.length <=
          Math.min(definition!.max_values ?? MAX_LIST_VALUES, MAX_LIST_VALUES) &&
        filter.value.every((value) => validValue(field!, value));
    else if (valid) valid = !Array.isArray(filter.value) && validValue(field!, filter.value);
    if (!valid)
      errors.push(`Check the permitted field, operator and typed values for filter ${index + 1}.`);
  });
  if (draft.sort.length > Math.min(product.sort.max_fields, MAX_SORT_FIELDS))
    errors.push(`Use at most ${Math.min(product.sort.max_fields, MAX_SORT_FIELDS)} sort fields.`);
  if (
    draft.sort.some(
      (sort) =>
        !product.sort.fields.includes(sort.field) || !['asc', 'desc'].includes(sort.direction),
    )
  )
    errors.push('Choose only permitted sort fields and directions.');
  if (new Set(draft.sort.map((sort) => sort.field)).size !== draft.sort.length)
    errors.push('Each sort field can appear only once.');
  return errors;
}

function validDate(value: string) {
  if (!/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(value) || value.startsWith('0000')) return false;
  const date = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(date.valueOf()) && date.toISOString().slice(0, 10) === value;
}

export function validValue(field: Model<'FieldSchema'>, value: unknown): boolean {
  if (value === null) return !!field.nullable;
  if (field.enum && !field.enum.includes(value as Scalar)) return false;
  if (typeof value === 'string' && Array.from(value).length > 4096) return false;
  switch (field.type) {
    case 'boolean':
      return typeof value === 'boolean';
    case 'integer':
      return typeof value === 'number' && Number.isSafeInteger(value);
    case 'number':
      return (
        typeof value === 'number' &&
        Number.isFinite(value) &&
        (!Number.isInteger(value) || Number.isSafeInteger(value))
      );
    case 'string':
      return typeof value === 'string';
    case 'date':
      return typeof value === 'string' && validDate(value);
    case 'timestamp': {
      if (
        typeof value !== 'string' ||
        !/^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})$/.test(
          value,
        )
      )
        return false;
      return (
        validDate(value.slice(0, 10)) &&
        Number(value.slice(11, 13)) < 24 &&
        Number(value.slice(14, 16)) < 60 &&
        Number(value.slice(17, 19)) < 60 &&
        Number.isFinite(Date.parse(value))
      );
    }
    case 'decimal': {
      if (
        typeof value !== 'string' ||
        !/^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/.test(value) ||
        field.precision == null ||
        field.scale == null
      )
        return false;
      const [whole, fraction = ''] = value.replace(/^-/, '').split('.');
      return (
        fraction.length <= field.scale &&
        (whole === '0' ? 0 : whole.length) <= field.precision - field.scale
      );
    }
  }
}

export function productContext(product: Model<'ProductDescription'>) {
  // An authorization projection can change independently of the immutable manifest digest.
  return JSON.stringify(product, (_key, value: unknown) =>
    value !== null && typeof value === 'object' && !Array.isArray(value)
      ? Object.fromEntries(Object.entries(value).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)))
      : value,
  );
}

export function queryFingerprint(product: Model<'ProductDescription'>, draft: QueryDraft) {
  return JSON.stringify([
    productContext(product),
    {
      inputs: Object.fromEntries(
        Object.entries(draft.inputs).sort(([a], [b]) => a.localeCompare(b)),
      ),
      filters: draft.filters.map(({ field, operator, value }) => ({
        field,
        operator,
        ...(operator === 'is_null' ? {} : { value }),
      })),
      sort: draft.sort.map(({ field, direction }) => ({ field, direction })),
    },
  ]);
}

function freeze<T>(value: T): T {
  if (value !== null && typeof value === 'object') {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}

/** A single in-memory intent. A retry never reads live controls or creates a second key. */
export function queryIntent(
  product: Model<'ProductDescription'>,
  draft: QueryDraft,
  previous?: QueryIntent,
): QueryIntent {
  if (queryErrors(product, draft).length) throw new Error('Invalid query controls');
  const fingerprint = queryFingerprint(product, draft);
  if (previous?.fingerprint === fingerprint) return previous;
  const request: Model<'QueryRequest'> = {
    product_id: product.product_id,
    version: product.version,
    inputs: { ...draft.inputs },
    filters: draft.filters.map(({ field, operator, value }) => ({
      field,
      operator,
      ...(operator === 'is_null' ? {} : { value: Array.isArray(value) ? [...value] : value }),
    })),
    sort: draft.sort.map((sort) => ({ ...sort })),
    idempotency_key: crypto.randomUUID(),
  };
  return freeze({ fingerprint, request });
}
