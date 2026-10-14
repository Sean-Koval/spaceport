import openapi from '../../docs/compatibility/openapi.json';
/** Structural form assistance only. The gateway alone validates meaning, bindings and digests. */
export type Schema = {
  $ref?: string;
  type?: string;
  title?: string;
  properties?: Record<string, Schema>;
  patternProperties?: Record<string, Schema>;
  additionalProperties?: boolean | Schema;
  required?: string[];
  items?: Schema;
  anyOf?: Schema[];
  enum?: unknown[];
  const?: unknown;
  default?: unknown;
  minLength?: number;
  maxLength?: number;
  minItems?: number;
  maxItems?: number;
  minimum?: number;
  maximum?: number;
  pattern?: string;
};
const schemas: Record<string, Schema> = openapi.components.schemas as Record<string, Schema>;
export function resolveSchema(schema: Schema): Schema {
  if (!schema.$ref) return schema;
  const prefix = '#/components/schemas/';
  if (!schema.$ref.startsWith(prefix)) throw new Error('Unsupported local schema');
  const result = schemas[schema.$ref.slice(prefix.length)];
  if (!result) throw new Error('Unknown local schema');
  return result;
}
export function namedSchema(name: string): Schema {
  const value = schemas[name];
  if (!value) throw new Error('Unknown schema');
  return value;
}
export function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
export function matchesSchema(value: unknown, name: string): boolean {
  return shapeMatches(value, namedSchema(name), 0);
}
function shapeMatches(value: unknown, original: Schema, depth: number): boolean {
  if (depth > 24) return false;
  const schema = resolveSchema(original);
  if (schema.anyOf) return schema.anyOf.some((branch) => shapeMatches(value, branch, depth + 1));
  if ('const' in schema && value !== schema.const) return false;
  if (schema.enum && !schema.enum.includes(value)) return false;
  if (schema.type === 'null') return value === null;
  if (schema.type === 'array')
    return (
      Array.isArray(value) &&
      value.length <= (schema.maxItems ?? 1000) &&
      value.every((item) => shapeMatches(item, schema.items ?? {}, depth + 1))
    );
  if (schema.type === 'object') {
    if (!record(value) || Object.keys(value).length > 1000) return false;
    if (schema.required?.some((key) => !(key in value))) return false;
    return Object.entries(value).every(([key, item]) => {
      if (['__proto__', 'prototype', 'constructor'].includes(key)) return false;
      let child = schema.properties?.[key];
      if (!child && schema.patternProperties)
        child = Object.entries(schema.patternProperties).find(([pattern]) =>
          new RegExp(pattern).test(key),
        )?.[1];
      if (!child) {
        if (schema.additionalProperties === false || schema.patternProperties) return false;
        if (record(schema.additionalProperties)) child = schema.additionalProperties as Schema;
      }
      return child ? shapeMatches(item, child, depth + 1) : true;
    });
  }
  if (schema.type === 'integer') return typeof value === 'number' && Number.isSafeInteger(value);
  if (schema.type === 'number') return typeof value === 'number' && Number.isFinite(value);
  if (schema.type === 'string') return typeof value === 'string';
  if (schema.type === 'boolean') return typeof value === 'boolean';
  return value !== undefined;
}
export function changeFieldType(value: Record<string, unknown>, type: string) {
  const { precision: _precision, scale: _scale, default: _default, enum: _enum, ...rest } = value;
  return { ...rest, type, ...(type === 'decimal' ? { precision: 38, scale: 2 } : {}) };
}
export function schemaDefault(original: Schema): unknown {
  const schema = resolveSchema(original);
  if ('default' in schema) return structuredClone(schema.default);
  if ('const' in schema) return schema.const;
  if (schema.enum) return schema.enum[0];
  if (schema.anyOf)
    return schema.anyOf.some((s) => s.type === 'null') ? null : schemaDefault(schema.anyOf[0]);
  if (schema.type === 'object')
    return Object.fromEntries(
      Object.entries(schema.properties ?? {})
        .filter(([key, s]) => schema.required?.includes(key) || 'default' in s)
        .map(([key, s]) => [key, schemaDefault(s)]),
    );
  if (schema.type === 'array') return [];
  if (schema.type === 'boolean') return false;
  if (schema.type === 'integer' || schema.type === 'number') return schema.minimum ?? 0;
  return '';
}
