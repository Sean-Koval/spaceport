import { useId } from 'react';
import type { Model } from './api';
import {
  initialValue,
  newFilter,
  MAX_FILTERS,
  MAX_LIST_VALUES,
  MAX_SORT_FIELDS,
  type Scalar,
} from './query-controls';

export function InputField({
  name,
  field,
  value,
  onChange,
  allowOmit = true,
  modeLabel = `${name} value mode`,
}: {
  name: string;
  field: Model<'FieldSchema'>;
  value: Scalar | undefined;
  onChange: (next: Scalar | undefined) => void;
  allowOmit?: boolean;
  modeLabel?: string;
}) {
  const id = useId();
  const mode = value === undefined ? 'omit' : value === null ? 'null' : 'value';
  return (
    <div className="input-field">
      <label htmlFor={`mode-${id}`}>{modeLabel}</label>
      <select
        id={`mode-${id}`}
        value={mode}
        onChange={(event) =>
          onChange(
            event.target.value === 'omit'
              ? undefined
              : event.target.value === 'null'
                ? null
                : initialValue(field),
          )
        }
      >
        {allowOmit && <option value="omit">Omit (gateway default, if defined)</option>}
        {(field.nullable || field.enum?.includes(null)) && <option value="null">null</option>}
        <option value="value">Enter value</option>
      </select>
      {(mode === 'value' || (mode === 'null' && field.enum?.includes(null))) && (
        <>
          <label htmlFor={`input-${id}`}>
            {name} ({field.type})
          </label>
          {field.enum ? (
            <select
              id={`input-${id}`}
              value={field.enum.findIndex((item) => item === value)}
              onChange={(event) => onChange(field.enum![Number(event.target.value)])}
            >
              {!field.enum.includes(value ?? null) && (
                <option value={-1} disabled>
                  Choose an allowed value
                </option>
              )}
              {field.enum.map((item, index) => (
                <option key={index} value={index}>
                  {JSON.stringify(item)}
                </option>
              ))}
            </select>
          ) : field.type === 'boolean' ? (
            <select
              id={`input-${id}`}
              value={String(value)}
              onChange={(event) => onChange(event.target.value === 'true')}
            >
              <option value="false">false</option>
              <option value="true">true</option>
            </select>
          ) : (
            <input
              id={`input-${id}`}
              type={
                field.type === 'date'
                  ? 'date'
                  : field.type === 'number' || field.type === 'integer'
                    ? 'number'
                    : 'text'
              }
              step={field.type === 'integer' ? 1 : 'any'}
              maxLength={8192}
              inputMode={field.type === 'decimal' ? 'decimal' : undefined}
              placeholder={field.type === 'timestamp' ? 'YYYY-MM-DDTHH:mm:ssZ' : undefined}
              value={typeof value === 'string' || typeof value === 'number' ? value : ''}
              onChange={(event) =>
                onChange(
                  field.type === 'number' || field.type === 'integer'
                    ? event.target.value === ''
                      ? ''
                      : Number(event.target.value)
                    : event.target.value,
                )
              }
            />
          )}
        </>
      )}
    </div>
  );
}
export function QueryControls({
  product,
  filters,
  sort,
  onFilters,
  onSort,
}: {
  product: Model<'ProductDescription'>;
  filters: Model<'QueryFilter'>[];
  sort: Model<'QuerySort'>[];
  onFilters: (value: Model<'QueryFilter'>[]) => void;
  onSort: (value: Model<'QuerySort'>[]) => void;
}) {
  const id = useId();
  const fields = Object.keys(product.filters).filter((field) =>
    Object.hasOwn(product.outputs, field),
  );
  const availableSort = product.sort.fields.filter(
    (field) => !sort.some((item) => item.field === field),
  );
  const updateFilter = (index: number, filter: Model<'QueryFilter'>) =>
    onFilters(filters.map((old, i) => (i === index ? filter : old)));
  return (
    <>
      <fieldset className="nested-fields">
        <legend>Permitted filters</legend>
        <p>All filters are combined. The gateway also applies your required access constraints.</p>
        {!fields.length && <p>No filters are permitted for this product.</p>}
        {filters.map((filter, index) => {
          const label = `Filter ${index + 1}`;
          const field = product.outputs[filter.field];
          const definition = product.filters[filter.field];
          const list = filter.operator === 'in' || filter.operator === 'not_in';
          const values = Array.isArray(filter.value) ? filter.value : [];
          return (
            <fieldset className="field-group" key={index}>
              <legend>{label}</legend>
              <label htmlFor={`${id}-filter-${index}`}>{label} field</label>
              <select
                id={`${id}-filter-${index}`}
                value={filter.field}
                onChange={(event) => updateFilter(index, newFilter(product, event.target.value))}
              >
                {fields.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
              <label htmlFor={`${id}-operator-${index}`}>{label} operator</label>
              <select
                id={`${id}-operator-${index}`}
                value={filter.operator}
                onChange={(event) =>
                  updateFilter(
                    index,
                    newFilter(
                      product,
                      filter.field,
                      event.target.value as Model<'QueryFilter'>['operator'],
                    ),
                  )
                }
              >
                {definition.operators.map((operator) => (
                  <option key={operator} value={operator}>
                    {operator}
                  </option>
                ))}
              </select>
              {filter.operator === 'is_null' ? (
                <p>No value is sent for is_null.</p>
              ) : list ? (
                <>
                  <p>
                    Between 1 and{' '}
                    {Math.min(definition.max_values ?? MAX_LIST_VALUES, MAX_LIST_VALUES)} typed
                    values.
                  </p>
                  {values.map((value, valueIndex) => (
                    <div className="field-group" key={valueIndex}>
                      <InputField
                        name={`${label} value ${valueIndex + 1}`}
                        modeLabel={`${label} value ${valueIndex + 1} mode`}
                        field={field}
                        value={value}
                        allowOmit={false}
                        onChange={(next) =>
                          updateFilter(index, {
                            ...filter,
                            value: values.map((old, i) => (i === valueIndex ? next! : old)),
                          })
                        }
                      />
                      <button
                        type="button"
                        className="text-button"
                        disabled={values.length <= 1}
                        onClick={() =>
                          updateFilter(index, {
                            ...filter,
                            value: values.filter((_, i) => i !== valueIndex),
                          })
                        }
                      >
                        Remove value {valueIndex + 1} from filter {index + 1}
                      </button>
                    </div>
                  ))}
                  <button
                    type="button"
                    className="secondary"
                    disabled={
                      values.length >=
                      Math.min(definition.max_values ?? MAX_LIST_VALUES, MAX_LIST_VALUES)
                    }
                    onClick={() =>
                      updateFilter(index, { ...filter, value: [...values, initialValue(field)] })
                    }
                  >
                    Add value to filter {index + 1}
                  </button>
                </>
              ) : (
                <InputField
                  name={`${label} value`}
                  modeLabel={`${label} value mode`}
                  field={field}
                  value={filter.value as Scalar}
                  allowOmit={false}
                  onChange={(value) => updateFilter(index, { ...filter, value })}
                />
              )}
              <button
                type="button"
                className="text-button"
                onClick={() => onFilters(filters.filter((_, i) => i !== index))}
              >
                Remove filter {index + 1}
              </button>
            </fieldset>
          );
        })}
        <button
          type="button"
          className="secondary"
          disabled={!fields.length || filters.length >= MAX_FILTERS}
          onClick={() => onFilters([...filters, newFilter(product, fields[0])])}
        >
          Add filter
        </button>
      </fieldset>
      <fieldset className="nested-fields">
        <legend>Permitted sorting (priority order)</legend>
        {!product.sort.fields.length && <p>No sorting is permitted for this product.</p>}
        {sort.map((item, index) => (
          <fieldset className="field-group" key={index}>
            <legend>Sort {index + 1}</legend>
            <label htmlFor={`${id}-sort-${index}`}>Sort {index + 1} field</label>
            <select
              id={`${id}-sort-${index}`}
              value={item.field}
              onChange={(event) =>
                onSort(
                  sort.map((old, i) => (i === index ? { ...old, field: event.target.value } : old)),
                )
              }
            >
              {product.sort.fields
                .filter((field) => field === item.field || availableSort.includes(field))
                .map((field) => (
                  <option key={field} value={field}>
                    {field}
                  </option>
                ))}
            </select>
            <label htmlFor={`${id}-direction-${index}`}>Sort {index + 1} direction</label>
            <select
              id={`${id}-direction-${index}`}
              value={item.direction}
              onChange={(event) =>
                onSort(
                  sort.map((old, i) =>
                    i === index ? { ...old, direction: event.target.value as 'asc' | 'desc' } : old,
                  ),
                )
              }
            >
              <option value="asc">Ascending</option>
              <option value="desc">Descending</option>
            </select>
            <button
              type="button"
              className="text-button"
              onClick={() => onSort(sort.filter((_, i) => i !== index))}
            >
              Remove sort {index + 1}
            </button>
          </fieldset>
        ))}
        <button
          type="button"
          className="secondary"
          disabled={
            !availableSort.length ||
            sort.length >= Math.min(product.sort.max_fields, MAX_SORT_FIELDS)
          }
          onClick={() => onSort([...sort, { field: availableSort[0], direction: 'asc' }])}
        >
          Add sort
        </button>
      </fieldset>
    </>
  );
}
