import { useLayoutEffect, useRef, useState } from 'react';
import { useParams, useSearchParams, Link, Navigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useGateway } from '../auth';
import { useAccess, useInvocation } from '../access';
import { GatewayError, type Model } from '../api';
import { Loading, ErrorNotice } from '../ui';
import { Rows, Value, SafeLinks, ConnectorStatus } from '../metadata';
import { InputField, QueryControls } from '../QueryControls';
import {
  queryErrors,
  queryFingerprint,
  queryIntent,
  productContext,
  type Scalar,
  type QueryIntent,
} from '../query-controls';
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
function ExecutionResult({ id, product }: { id: string; product: Model<'ProductDescription'> }) {
  const { client, generation } = useGateway();
  const [cursor, setCursor] = useState<string | undefined>();
  const result = useQuery({
    queryKey: [generation, 'execution', id, productContext(product), cursor],
    queryFn: async ({ signal }) => {
      const value = await client.execution(id, cursor, signal);
      if (
        value.execution_id !== id ||
        ('product_id' in value &&
          (value.product_id !== product.product_id ||
            value.version !== product.version ||
            value.product_digest !== product.digest))
      )
        throw new GatewayError(404, 'NOT_FOUND_OR_FORBIDDEN');
      return value;
    },
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
  const [filters, setFilters] = useState<Model<'QueryFilter'>[]>([]);
  const [sort, setSort] = useState<Model<'QuerySort'>[]>([]);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [uncertain, setUncertain] = useState(false);
  const [editing, setEditing] = useState(false);
  const [acceptedIntent, setAcceptedIntent] = useState(false);
  const intent = useRef<QueryIntent | undefined>(undefined);
  const guard = useRef(false);
  const active = useRef(true);
  useLayoutEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
      intent.current = undefined;
    };
  }, []);
  const execution = params.get('execution');
  const draft = { inputs, filters, sort };
  const validation = queryErrors(product, draft);
  const unchanged = intent.current?.fingerprint === queryFingerprint(product, draft);
  async function execute(retry = false, fresh = false) {
    if (guard.current || !active.current || !access.current()) return;
    if (!retry && (validation.length || (uncertain && (!editing || unchanged)))) return;
    if (retry && !intent.current) return;
    if (fresh && (!acceptedIntent || uncertain)) return;
    guard.current = true;
    setPending(true);
    setError(null);
    setAcceptedIntent(false);
    let submitted = false;
    try {
      const snapshot = retry
        ? intent.current!
        : queryIntent(product, draft, fresh ? undefined : intent.current);
      intent.current = snapshot;
      if (!retry) {
        setUncertain(false);
        setEditing(false);
      }
      submitted = true;
      const accepted = await client.execute(snapshot.request);
      if (!active.current || !isCurrent()) return;
      setUncertain(false);
      setEditing(false);
      setAcceptedIntent(true);
      const next = new URLSearchParams(params);
      next.set('version', product.version);
      next.set('execution', accepted.execution_id);
      setParams(next);
    } catch (caught) {
      if (!active.current || access.reject(caught) || !isCurrent()) return;
      setError(caught);
      if (submitted && (!(caught instanceof GatewayError) || caught.status >= 500)) {
        setUncertain(true);
        setEditing(false);
      }
    } finally {
      guard.current = false;
      if (active.current && access.current()) setPending(false);
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
              disabled={pending || (uncertain && !editing)}
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
          <fieldset disabled={pending || (uncertain && !editing)}>
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
            <QueryControls
              product={product}
              filters={filters}
              sort={sort}
              onFilters={setFilters}
              onSort={setSort}
            />
            {!!validation.length && (
              <div className="notice warning" role="status">
                <p>Check these values before running:</p>
                <ul>
                  {validation.map((message) => (
                    <li key={message}>{message}</li>
                  ))}
                </ul>
              </div>
            )}
            <button
              disabled={pending || (uncertain && (!editing || unchanged)) || !!validation.length}
            >
              {pending ? 'Starting execution…' : 'Run product'}
            </button>
            {acceptedIntent && !uncertain && (
              <>
                <p>
                  Unchanged values reuse the accepted execution. Start a new run to request a fresh
                  execution with the current values.
                </p>
                <button
                  type="button"
                  className="secondary"
                  disabled={pending || !!validation.length}
                  onClick={() => void execute(false, true)}
                >
                  Start new run
                </button>
              </>
            )}
          </fieldset>
        </form>
        {uncertain && (
          <div className="notice warning" role="status">
            <p>Execution outcome not yet confirmed. Nothing is resubmitted automatically.</p>
            <p>
              Retry reuses the original request and its key, even if you edit the controls. Changed
              values start a separate execution; the earlier execution may still complete.
            </p>
            <details>
              <summary>Original request for retry</summary>
              <Value value={intent.current?.request} />
            </details>
            <button type="button" disabled={pending} onClick={() => void execute(true)}>
              Retry same request
            </button>
            {!editing && (
              <button
                type="button"
                className="secondary"
                disabled={pending}
                onClick={() => setEditing(true)}
              >
                Edit as new request
              </button>
            )}
          </div>
        )}
      </section>
      {execution && <ExecutionResult key={execution} id={execution} product={product} />}
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
    queryFn: async ({ signal }) => {
      const product = await client.product(productId!, version, signal);
      if (
        product.product_id !== productId ||
        (version !== undefined && product.version !== version)
      )
        throw new GatewayError(404, 'NOT_FOUND_OR_FORBIDDEN');
      return product;
    },
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
  // Resolve “latest” once, before the user can create an intent or follow an execution.
  if (!version) {
    const pinned = new URLSearchParams(params);
    pinned.set('version', detail.data.version);
    return <Navigate replace to={{ search: pinned.toString() }} />;
  }
  return (
    <ProductDetail key={`${generation}:${productContext(detail.data)}`} product={detail.data} />
  );
}
