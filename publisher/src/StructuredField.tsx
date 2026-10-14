import { useId, useState } from 'react';
import { resolveSchema, schemaDefault, record, changeFieldType, type Schema } from './form-schema';
type Props = { label: string; schema: Schema; value: unknown; onChange: (value: unknown) => void };
/** Uses only the bundled OpenAPI shape. No remote schemas, source controls or code execution. */
export function StructuredField({ label, schema: original, value, onChange }: Props) {
  const id = useId();
  const [newKey, setNewKey] = useState('');
  const schema = resolveSchema(original);
  if (schema.anyOf) {
    const options = schema.anyOf.map(resolveSchema);
    const type =
      value === null
        ? 'null'
        : Array.isArray(value)
          ? 'array'
          : typeof value === 'number'
            ? Number.isInteger(value)
              ? 'integer'
              : 'number'
            : typeof value;
    let selected = options.findIndex((option) => option.type === type);
    if (selected < 0) selected = 0;
    return (
      <div className="field">
        <label htmlFor={id}>{label} value type</label>
        <select
          id={id}
          value={selected}
          onChange={(event) => onChange(schemaDefault(options[Number(event.target.value)]))}
        >
          {options.map((option, index) => (
            <option value={index} key={index}>
              {option.type === 'null' ? 'null / not set' : (option.title ?? option.type ?? 'value')}
            </option>
          ))}
        </select>
        {options[selected].type !== 'null' && (
          <StructuredField
            label={label}
            schema={options[selected]}
            value={value}
            onChange={onChange}
          />
        )}
      </div>
    );
  }
  if (schema.type === 'object') {
    const object = record(value) ? value : {};
    const properties = schema.properties;
    const update = (key: string, next: unknown) =>
      onChange(
        schema.title === 'FieldSchema' && key === 'type' && typeof next === 'string'
          ? changeFieldType(object, next)
          : { ...object, [key]: next },
      );
    const omit = (key: string) => {
      const { [key]: _removed, ...rest } = object;
      onChange(rest);
    };
    if (properties)
      return (
        <fieldset className="nested-fields">
          <legend>{label}</legend>
          {Object.entries(properties).map(([key, child]) => {
            const fieldLabel = `${label} / ${key}`;
            const required = schema.required?.includes(key);
            const present = key in object;
            return (
              <div key={key} className="field-group">
                {present ? (
                  <>
                    <StructuredField
                      label={fieldLabel}
                      schema={child}
                      value={object[key]}
                      onChange={(next) => update(key, next)}
                    />
                    {!required && (
                      <button type="button" className="text-button" onClick={() => omit(key)}>
                        Omit {fieldLabel}
                      </button>
                    )}
                  </>
                ) : required ? (
                  <StructuredField
                    label={fieldLabel}
                    schema={child}
                    value={schemaDefault(child)}
                    onChange={(next) => update(key, next)}
                  />
                ) : (
                  <button
                    type="button"
                    className="text-button"
                    onClick={() => update(key, schemaDefault(child))}
                  >
                    Add {fieldLabel}
                  </button>
                )}
              </div>
            );
          })}
        </fieldset>
      );
    const itemSchema =
      Object.values(schema.patternProperties ?? {})[0] ??
      (record(schema.additionalProperties) ? (schema.additionalProperties as Schema) : {});
    const valid =
      /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(newKey) &&
      !['__proto__', 'prototype', 'constructor'].includes(newKey) &&
      !(newKey in object);
    return (
      <fieldset className="nested-fields">
        <legend>{label}</legend>
        {Object.entries(object).map(([key, item]) => (
          <div key={key} className="field-group">
            <StructuredField
              label={`${label} / ${key}`}
              schema={itemSchema}
              value={item}
              onChange={(next) => update(key, next)}
            />
            <button type="button" className="text-button" onClick={() => omit(key)}>
              Remove {label} / {key}
            </button>
          </div>
        ))}
        <label htmlFor={id}>New field name in {label}</label>
        <div className="inline">
          <input
            id={id}
            maxLength={128}
            value={newKey}
            onChange={(event) => setNewKey(event.target.value)}
          />
          <button
            type="button"
            className="secondary"
            disabled={!valid || Object.keys(object).length >= 50}
            onClick={() => {
              update(newKey, schemaDefault(itemSchema));
              setNewKey('');
            }}
          >
            Add field to {label}
          </button>
        </div>
      </fieldset>
    );
  }
  if (schema.type === 'array') {
    const items = Array.isArray(value) ? value : [];
    return (
      <fieldset className="nested-fields">
        <legend>{label} (ordered)</legend>
        {items.map((item, index) => (
          <div className="field-group" key={index}>
            <StructuredField
              label={`${label} / ${index + 1}`}
              schema={schema.items ?? {}}
              value={item}
              onChange={(next) => onChange(items.map((old, i) => (i === index ? next : old)))}
            />
            <button
              type="button"
              className="text-button"
              onClick={() => onChange(items.filter((_, i) => i !== index))}
            >
              Remove {label} {index + 1}
            </button>
          </div>
        ))}
        <button
          type="button"
          className="secondary"
          disabled={items.length >= (schema.maxItems ?? schema.maxLength ?? 100)}
          onClick={() => onChange([...items, schemaDefault(schema.items ?? {})])}
        >
          Add {label} item
        </button>
      </fieldset>
    );
  }
  if (schema.enum)
    return (
      <div className="field">
        <label htmlFor={id}>{label}</label>
        <select
          id={id}
          value={String(value ?? '')}
          onChange={(event) =>
            onChange(schema.enum!.find((option) => String(option) === event.target.value))
          }
        >
          {schema.enum.map((option, index) => (
            <option key={index} value={String(option)}>
              {String(option)}
            </option>
          ))}
        </select>
      </div>
    );
  if ('const' in schema)
    return (
      <div className="field">
        <label htmlFor={id}>{label}</label>
        <input id={id} value={String(schema.const)} readOnly />
      </div>
    );
  if (schema.type === 'boolean')
    return (
      <div className="field checkbox">
        <label>
          <input
            type="checkbox"
            checked={value === true}
            onChange={(event) => onChange(event.target.checked)}
          />
          {label}
        </label>
      </div>
    );
  if (schema.type === 'null') return <p>{label}: null</p>;
  const numeric = schema.type === 'number' || schema.type === 'integer';
  const multiline = /description|aggregation/i.test(label);
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {multiline ? (
        <textarea
          id={id}
          value={typeof value === 'string' ? value : ''}
          maxLength={schema.maxLength ?? 16384}
          onChange={(event) => onChange(event.target.value)}
        />
      ) : (
        <input
          id={id}
          type={numeric ? 'number' : 'text'}
          step={schema.type === 'integer' ? 1 : 'any'}
          min={schema.minimum}
          max={schema.maximum}
          maxLength={schema.maxLength ?? 16384}
          value={typeof value === 'string' || typeof value === 'number' ? value : ''}
          onChange={(event) =>
            onChange(
              numeric && event.target.value !== ''
                ? Number(event.target.value)
                : event.target.value,
            )
          }
        />
      )}
    </div>
  );
}
