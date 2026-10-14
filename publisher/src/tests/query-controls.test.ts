import { expect, it } from 'vitest';
import type { Model } from '../api';
import {
  queryErrors,
  queryFingerprint,
  queryIntent,
  validValue,
  type QueryDraft,
} from '../query-controls';
import { product as fixture } from './fixtures';

const product = fixture as unknown as Model<'ProductDescription'>;
const empty: QueryDraft = { inputs: {}, filters: [], sort: [] };
const field = (
  type: Model<'FieldSchema'>['type'],
  extra: Partial<Model<'FieldSchema'>> = {},
): Model<'FieldSchema'> => ({ type, required: true, nullable: false, ...extra });

it.each([
  ['decimal', '9007199254740993.01', true],
  ['decimal', '0.00', true],
  ['decimal', '-0.01', true],
  ['decimal', 9007199254740993, false],
  ['decimal', '1e3', false],
  ['decimal', '01.00', false],
  ['decimal', '1.001', false],
  ['decimal', '+1.00', false],
  ['decimal', 'NaN', false],
  ['date', '2024-02-29', true],
  ['date', '2025-02-29', false],
  ['date', '2024-03-10', true],
  ['date', '2024-03-10T00:00:00Z', false],
  ['date', '0000-01-01', false],
  ['timestamp', '2024-03-10T01:30:00-05:00', true],
  ['timestamp', '2024-03-10T01:30:00.123456Z', true],
  ['timestamp', '2024-03-10T01:30:00', false],
  ['timestamp', '2024-03-10', false],
  ['timestamp', '2025-02-29T00:00:00Z', false],
  ['timestamp', '2024-03-10T24:00:00Z', false],
  ['integer', 9007199254740991, true],
  ['integer', 9007199254740992, false],
  ['integer', 1.5, false],
  ['integer', '2', false],
  ['integer', false, false],
  ['number', 1.5, true],
  ['number', Infinity, false],
  ['number', NaN, false],
  ['number', 9007199254740992, false],
  ['boolean', false, true],
  ['boolean', 'false', false],
  ['string', '', true],
  ['string', null, false],
  ['string', 'a'.repeat(4097), false],
] as const)('validates %s value %s as %s without coercion', (type, value, valid) => {
  expect(
    validValue(field(type, type === 'decimal' ? { precision: 38, scale: 2 } : {}), value),
  ).toBe(valid);
});

it('validates decimal precision and nullable enums without losing strings', () => {
  expect(validValue(field('decimal', { precision: 3, scale: 2 }), '9.99')).toBe(true);
  expect(validValue(field('decimal', { precision: 3, scale: 2 }), '10.00')).toBe(false);
  expect(validValue(field('decimal', { precision: 2, scale: 2 }), '-0.01')).toBe(true);
  expect(validValue(field('string', { nullable: true, enum: ['A'] }), null)).toBe(true);
  expect(validValue(field('integer', { enum: [0, 1] }), false)).toBe(false);
  expect(validValue(field('integer', { enum: [0, 1] }), '0')).toBe(false);
});

it.each([
  { inputs: { rogue: 'value' } },
  { filters: [{ field: 'revenue', operator: 'eq', value: '1.00' }] },
  { filters: [{ field: 'segment', operator: 'lt', value: 'A' }] },
  { filters: [{ field: 'segment', operator: 'in', value: [] }] },
  { filters: [{ field: 'segment', operator: 'in', value: Array(11).fill('A') }] },
  { filters: [{ field: 'segment', operator: 'in', value: 'A' }] },
  { filters: [{ field: 'segment', operator: 'eq', value: ['A'] }] },
  { filters: [{ field: 'segment', operator: 'is_null', value: false }] },
  { filters: Array(21).fill({ field: 'segment', operator: 'is_null' }) },
  {
    sort: [
      { field: 'day', direction: 'asc' },
      { field: 'day', direction: 'desc' },
    ],
  },
  { sort: [{ field: 'revenue', direction: 'asc' }] },
  { sort: [{ field: 'day', direction: 'up' }] },
  { sort: Array(3).fill({ field: 'day', direction: 'asc' }) },
])('rejects out-of-contract query %j', (patch) => {
  const draft = { ...empty, ...patch } as QueryDraft;
  expect(queryErrors(product, draft).length).toBeGreaterThan(0);
  expect(() => queryIntent(product, draft)).toThrow('Invalid query controls');
});

it('enforces hard collection caps even for an over-broad local definition', () => {
  const broad = {
    ...product,
    filters: { segment: { operators: ['in'] as const, max_values: 200 } },
    sort: { fields: Array.from({ length: 6 }, (_, i) => `field-${i}`), max_fields: 6 },
  } as unknown as Model<'ProductDescription'>;
  expect(
    queryErrors(broad, {
      ...empty,
      filters: [{ field: 'segment', operator: 'in', value: Array(101).fill('A') }],
    }),
  ).not.toEqual([]);
  expect(
    queryErrors(broad, {
      ...empty,
      sort: broad.sort.fields.map((field) => ({ field, direction: 'asc' })),
    }),
  ).not.toEqual([]);
});

it('accepts exact bound values, nulls and no-value is_null', () => {
  const draft: QueryDraft = {
    ...empty,
    filters: [
      { field: 'segment', operator: 'in', value: [...Array(9).fill('A'), null] },
      { field: 'segment', operator: 'is_null' },
    ],
    sort: [
      { field: 'day', direction: 'desc' },
      { field: 'segment', direction: 'asc' },
    ],
  };
  expect(queryErrors(product, draft)).toEqual([]);
  expect(
    queryErrors(product, {
      ...empty,
      filters: Array(20).fill({ field: 'segment', operator: 'is_null' }),
    }),
  ).toEqual([]);
});

it('clones and freezes the complete request before live controls can change', () => {
  const draft: QueryDraft = {
    inputs: { start: '2024-03-09', end: '2024-03-12' },
    filters: [{ field: 'segment', operator: 'in', value: ['A', null] }],
    sort: [{ field: 'day', direction: 'asc' }],
  };
  const intent = queryIntent(product, draft);
  draft.inputs.start = '2024-03-10';
  (draft.filters[0].value as unknown[])[0] = 'B';
  draft.sort[0].direction = 'desc';
  expect(intent.request.inputs!.start).toBe('2024-03-09');
  expect(intent.request.filters[0].value).toEqual(['A', null]);
  expect(intent.request.sort[0].direction).toBe('asc');
  expect(Object.isFrozen(intent)).toBe(true);
  expect(Object.isFrozen(intent.request.inputs)).toBe(true);
  expect(Object.isFrozen(intent.request.filters[0].value)).toBe(true);
  expect(() => {
    intent.request.sort[0].direction = 'desc';
  }).toThrow(TypeError);
});

it('reuses key for unchanged values regardless of input key insertion order', () => {
  const first = { ...empty, inputs: { start: '2024-03-09', end: '2024-03-12' } };
  const second = { ...empty, inputs: { end: '2024-03-12', start: '2024-03-09' } };
  const intent = queryIntent(product, first);
  expect(queryFingerprint(product, first)).toBe(queryFingerprint(product, second));
  expect(queryIntent(product, second, intent)).toBe(intent);
  expect(queryIntent(product, first).request.idempotency_key).not.toBe(
    intent.request.idempotency_key,
  );
});

it.each(['inputs', 'filters', 'sort', 'version', 'digest'])(
  'creates a new intent when %s changes',
  (changed) => {
    const intent = queryIntent(product, empty);
    const draft: QueryDraft = {
      ...empty,
      ...(changed === 'inputs'
        ? { inputs: { segment: 'A' } }
        : changed === 'filters'
          ? { filters: [{ field: 'segment', operator: 'is_null' }] }
          : changed === 'sort'
            ? { sort: [{ field: 'day', direction: 'asc' }] }
            : {}),
    };
    const context = {
      ...product,
      ...(changed === 'version'
        ? { version: '2.0.0' }
        : changed === 'digest'
          ? { digest: 'c'.repeat(64) }
          : {}),
    };
    expect(queryIntent(context, draft, intent).request.idempotency_key).not.toBe(
      intent.request.idempotency_key,
    );
  },
);

it('separates narrowed policy projections even when the immutable digest is unchanged', () => {
  const intent = queryIntent(product, empty);
  const narrowed = { ...product, filters: {}, partial: true };
  expect(queryIntent(narrowed, empty, intent).request.idempotency_key).not.toBe(
    intent.request.idempotency_key,
  );
});

it('counts scalar text bounds in Unicode code points like the gateway', () => {
  expect(validValue(field('string'), '𝄞'.repeat(4096))).toBe(true);
  expect(validValue(field('string'), '𝄞'.repeat(4096) + 'a')).toBe(false);
});
