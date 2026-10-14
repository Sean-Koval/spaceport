import { describe, it, expect } from 'vitest';
import { matchesSchema, changeFieldType } from '../form-schema';
import revenue from '../../../fixtures/products/revenue.json';
describe('schema-driven structured form boundary', () => {
  it('accepts the manifest shape but rejects unknown fields recursively', () => {
    expect(matchesSchema(revenue, 'Manifest')).toBe(true);
    expect(matchesSchema({ ...revenue, sql: 'select 1' }, 'Manifest')).toBe(false);
    expect(
      matchesSchema(
        {
          ...revenue,
          outputs: { revenue: { type: 'decimal', precision: 38, scale: 2, source_url: 'hidden' } },
        },
        'Manifest',
      ),
    ).toBe(false);
  });
  it('preserves nullable defaults, empty strings, booleans, zero, and exact decimal strings', () => {
    for (const value of [null, '', false, 0, '9007199254741023.31'])
      expect(matchesSchema({ value }, 'DefaultValue')).toBe(true);
    expect(matchesSchema({ value: { sql: 'bad' } }, 'DefaultValue')).toBe(false);
  });
  it('removes incompatible field settings on type changes', () => {
    expect(
      changeFieldType(
        {
          type: 'decimal',
          precision: 38,
          scale: 2,
          default: { value: '1.00' },
          enum: ['1.00'],
          nullable: false,
        },
        'boolean',
      ),
    ).toEqual({ type: 'boolean', nullable: false });
    expect(changeFieldType({ type: 'boolean', nullable: false }, 'decimal')).toEqual({
      type: 'decimal',
      nullable: false,
      precision: 38,
      scale: 2,
    });
  });
});
