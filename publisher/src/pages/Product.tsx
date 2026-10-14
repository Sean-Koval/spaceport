import { useRef, useState } from 'react';
import { useParams, useSearchParams, Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useGateway } from '../auth';
import { useAccess, useInvocation } from '../access';
import { GatewayError, type Model } from '../api';
import { Loading, ErrorNotice } from '../ui';
import { Rows, Value, SafeLinks, ConnectorStatus } from '../metadata';
type Scalar = string | number | boolean | null;
function Provenance({ value }: { value: Model<'Provenance'> }) {
  return (
    <details className="panel">
      <summary>Authorized provenance</summary>
      <dl className="facts">
        <dt>Binding</dt>
        <dd>
          {value.binding_id} · Revision {value.binding_revision}
        </dd>
        <dt>Adapter revision</dt>
        <dd>{value.adapter_revision}</dd>
        <dt>Model bundle digest</dt>
        <dd>{value.model_bundle_digest}</dd>
      </dl>
      <SafeLinks links={value.links ?? []} />
    </details>
  );
}
function InputField({
  name,
  field,
  value,
  onChange,
}: {
  name: string;
  field: Model<'FieldSchema'>;
  value: Scalar | undefined;
  onChange: (next: Scalar | undefined) => void;
}) {
  const mode = value === undefined ? 'omit' : value === null ? 'null' : 'value';
  return (
    <div className="input-field">
      <label htmlFor={`mode-${name}`}>{name} value mode</label>
      <select
        id={`mode-${name}`}
        value={mode}
        onChange={(event) =>
          onChange(
            event.target.value === 'omit'
              ? undefined
              : event.target.value === 'null'
                ? null
                : field.enum?.length
                  ? field.enum[0]
                  : field.type === 'boolean'
                    ? false
                    : field.type === 'number' || field.type === 'integer'
                      ? 0
                      : '',
          )
        }
      >
        <option value="omit">Omit (gateway default, if defined)</option>
        {(field.nullable || field.enum?.includes(null)) && <option value="null">null</option>}
        <option value="value">Enter value</option>
      </select>
      {(mode === 'value' || (mode === 'null' && field.enum?.includes(null))) && (
        <>
          <label htmlFor={`input-${name}`}>
            {name} ({field.type})
          </label>
          {field.enum ? (
            <select
              id={`input-${name}`}
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
              id={`input-${name}`}
              value={String(value)}
              onChange={(event) => onChange(event.target.value === 'true')}
            >
              <option value="false">false</option>
              <option value="true">true</option>
            </select>
          ) : (
            <input
              id={`input-${name}`}
              type={
                field.type === 'date'
                  ? 'date'
                  : field.type === 'number' || field.type === 'integer'
                    ? 'number'
                    : 'text'
              }
              step={field.type === 'integer' ? 1 : 'any'}
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
function ExecutionResult({ id, productId }: { id: string; productId: string }) {
  const { client, generation } = useGateway();
  const [cursor, setCursor] = useState<string | undefined>();
  const result = useQuery({
    queryKey: [generation, 'execution', id, cursor],
    queryFn: ({ signal }) => client.execution(id, cursor, signal),
    refetchInterval: (query) =>
      query.state.data && ['accepted', 'running'].includes(query.state.data.state) ? 2000 : false,
  });
  useAccess(result.error);
  if (result.isPending) return <Loading>Loading execution…</Loading>;
  if (result.isError)
    return (
      <section>
        <h2>Result unavailable</h2>
        <ErrorNotice error={result.error} retry={() => void result.refetch()} />
      </section>
    );
  const data = result.data;
  const envelope = 'product_id' in data ? data : null;
  if (envelope && envelope.product_id !== productId)
    return <ErrorNotice error={new GatewayError(404, 'NOT_FOUND_OR_FORBIDDEN')} />;
  return (
    <section className="result" aria-labelledby="execution-result">
      <div className="page-heading">
        <h2 id="execution-result">Execution result · {data.state}</h2>
        <button className="secondary" onClick={() => void result.refetch()}>
          Refresh result
        </button>
      </div>
      <p>Execution {data.execution_id}</p>
      <dl className="facts">
        <dt>Accepted</dt>
        <dd>{data.accepted_at}</dd>
        <dt>Started</dt>
        <dd>{data.started_at ?? 'Not started'}</dd>
        <dt>Completed</dt>
        <dd>{data.completed_at ?? 'Not completed'}</dd>
      </dl>
      {data.error && (
        <ErrorNotice error={new GatewayError(503, data.error.code, data.error.request_id)} />
      )}
      {data.state === 'expired' && <p>Result expired. Stored rows are no longer available.</p>}
      {envelope && data.state === 'succeeded' && (
        <>
          <h3>Result version {envelope.version}</h3>
          <p>Product digest: {envelope.product_digest}</p>
          <div
            className={`notice ${envelope.freshness?.status === 'fresh' ? 'success' : 'warning'}`}
          >
            <strong>Freshness: {envelope.freshness?.status ?? 'unknown'}</strong>
            <p>
              Source-load freshness describes when the source was loaded, not when business events
              occurred.
            </p>
          </div>
          <dl className="facts">
            <dt>Source loaded</dt>
            <dd>{envelope.source_watermark ?? 'Source-load evidence unavailable'}</dd>
            <dt>Freshness checked</dt>
            <dd>{envelope.freshness?.assessed_at ?? 'Not provided'}</dd>
            <dt>Source age (seconds)</dt>
            <dd>{envelope.freshness?.age_seconds ?? 'Unknown'}</dd>
            <dt>Maximum age (seconds)</dt>
            <dd>{envelope.freshness?.max_age_seconds ?? 'Unknown'}</dd>
            <dt>Grain / timezone</dt>
            <dd>
              {envelope.grain} / {envelope.timezone}
            </dd>
          </dl>
          {envelope.effective_range && (
            <p>
              Effective range: [{envelope.effective_range.start}, {envelope.effective_range.end}) ·{' '}
              {envelope.timezone}
            </p>
          )}
          <p>Cache: {envelope.cache_status ?? 'unknown'}</p>
          {(envelope.warnings ?? []).map((warning, index) => (
            <p className="notice warning" key={index}>
              {warning}
            </p>
          ))}
          <Rows rows={envelope.rows ?? []} caption="Authorized result rows" />
          <p>{envelope.row_count} total rows · complete, never silently truncated</p>
          <details>
            <summary>Typed result schema and units</summary>
            <Value value={envelope.typed_schema} />
            <Value value={envelope.units} />
          </details>
          <Provenance value={envelope.provenance} />
          {envelope.next_cursor && (
            <button
              className="secondary"
              onClick={() => setCursor(envelope.next_cursor ?? undefined)}
            >
              Next result page
            </button>
          )}
          {cursor && (
            <button className="secondary" onClick={() => setCursor(undefined)}>
              First result page
            </button>
          )}
        </>
      )}
    </section>
  );
}
function ProductDetail({ product }: { product: Model<'ProductDescription'> }) {
  const { client } = useGateway();
  const access = useAccess();
  const isCurrent = useInvocation();
  const [params, setParams] = useSearchParams();
  const [inputs, setInputs] = useState<Record<string, Scalar>>({});
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [uncertain, setUncertain] = useState(false);
  const guard = useRef(false);
  const execution = params.get('execution');
  const invalidEnum = Object.entries(inputs).some(([name, value]) => {
    const field = product.inputs[name];
    if (value === null && field?.nullable) return false;
    const allowed = field?.enum;
    return allowed && !allowed.includes(value);
  });
  async function execute() {
    if (guard.current || uncertain || invalidEnum) return;
    guard.current = true;
    setPending(true);
    setError(null);
    try {
      const accepted = await client.execute({
        product_id: product.product_id,
        version: product.version,
        inputs,
        filters: [],
        sort: [],
      });
      if (!isCurrent()) return;
      const next = new URLSearchParams(params);
      next.set('version', product.version);
      next.set('execution', accepted.execution_id);
      setParams(next);
    } catch (caught) {
      if (access.reject(caught) || !isCurrent()) return;
      setError(caught);
      if (!(caught instanceof GatewayError) || caught.status >= 500) setUncertain(true);
    } finally {
      guard.current = false;
      setPending(false);
    }
  }
  return (
    <>
      <p className="eyebrow">
        {product.domain ?? 'Authorized product'} · {product.status}
      </p>
      <h1>{product.name}</h1>
      <p className="lead">{product.description ?? 'Description not provided in this view.'}</p>
      <p>
        Version {product.version} · {product.product_id}
      </p>
      {product.partial && (
        <p className="notice">
          This is an authorized partial view. Only returned fields and metadata are shown.
        </p>
      )}
      {product.status === 'deprecated' && (
        <p className="notice warning">
          This version is deprecated. Existing consumers should review their use.
        </p>
      )}
      <section className="panel">
        <h2>Meaning and contract</h2>
        <dl className="facts">
          <dt>Owner</dt>
          <dd>{product.owner ?? 'Not provided'}</dd>
          <dt>Grain</dt>
          <dd>{product.grain ?? 'Not provided'}</dd>
          <dt>Aggregation</dt>
          <dd>{product.aggregation ?? 'Not provided'}</dd>
          <dt>Timezone</dt>
          <dd>{product.timezone}</dd>
          <dt>Date interval</dt>
          <dd>{product.date_boundaries?.interval ?? 'Not applicable'}</dd>
          <dt>Freshness meaning</dt>
          <dd>Source-load age · maximum {product.freshness.max_age_seconds} seconds</dd>
        </dl>
        <h3>Execution limits</h3>
        <p>
          At most {product.limits.max_rows} rows and {product.limits.max_bytes} bytes;{' '}
          {product.limits.page_size} rows per page.
        </p>
        <p>
          Maximum {product.limits.max_date_range_days} local calendar days;{' '}
          {product.limits.per_principal_concurrency} concurrent executions for this principal.
        </p>
        <details>
          <summary>Permitted fields, filters, sorting and units</summary>
          <h4>Inputs</h4>
          <Value value={product.inputs} />
          <h4>Outputs</h4>
          <Value value={product.outputs} />
          <h4>Filters</h4>
          <Value value={product.filters} />
          <h4>Sort</h4>
          <Value value={product.sort} />
          <h4>Units</h4>
          <Value value={product.units} />
        </details>
        <SafeLinks links={product.links ?? []} />
      </section>
      <Provenance value={product.provenance} />
      <section className="panel">
        <h2>Run this product</h2>
        <p>
          Every execution uses the pinned version {product.version} and your current server-managed
          access.
        </p>
        {(product.examples ?? []).map((example, index) => (
          <article className="example" key={index}>
            <p>{example.description}</p>
            <button
              className="secondary"
              disabled={pending}
              onClick={() => setInputs({ ...example.inputs })}
            >
              Use example {index + 1}
            </button>
          </article>
        ))}
        {!product.examples?.length && <p>No examples are provided in this view.</p>}
        {!!error && <ErrorNotice error={error} />}
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void execute();
          }}
        >
          <fieldset disabled={pending || uncertain}>
            <legend>Permitted inputs</legend>
            {Object.entries(product.inputs).map(([name, field]) => (
              <InputField
                key={name}
                name={name}
                field={field}
                value={inputs[name]}
                onChange={(next) =>
                  setInputs((old) => {
                    const copy = { ...old };
                    if (next === undefined) delete copy[name];
                    else copy[name] = next;
                    return copy;
                  })
                }
              />
            ))}
            <button disabled={pending || uncertain || invalidEnum}>
              {pending ? 'Starting execution…' : 'Run product'}
            </button>
          </fieldset>
        </form>
        {uncertain && (
          <p>
            Execution outcome not yet confirmed. No duplicate request will be sent automatically.
          </p>
        )}
      </section>
      {execution && (
        <ExecutionResult key={execution} id={execution} productId={product.product_id} />
      )}
      <ConnectorStatus />
    </>
  );
}
export default function Product() {
  const { productId } = useParams();
  const [params] = useSearchParams();
  const { client, generation } = useGateway();
  const version = params.get('version') ?? undefined;
  const detail = useQuery({
    queryKey: [generation, 'product', productId, version],
    queryFn: ({ signal }) => client.product(productId!, version, signal),
    enabled: !!productId,
  });
  useAccess(detail.error);
  if (detail.isPending) return <Loading>Loading product…</Loading>;
  if (detail.isError)
    return (
      <>
        <h1>Product unavailable</h1>
        <ErrorNotice error={detail.error} retry={() => void detail.refetch()} />
        <Link to="/catalog">Return to catalog</Link>
      </>
    );
  return (
    <ProductDetail key={`${detail.data.product_id}:${detail.data.version}`} product={detail.data} />
  );
}
