import { useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useGateway } from '../auth';
import { useAccess } from '../access';
import { ErrorNotice, Loading } from '../ui';
export default function Catalog() {
  const { client, generation } = useGateway();
  const [params, setParams] = useSearchParams();
  const q = params.get('q') ?? '';
  const cursor = params.get('cursor') ?? undefined;
  const [search, setSearch] = useState(q);
  const result = useQuery({
    queryKey: [generation, 'catalog', q, cursor],
    queryFn: ({ signal }) => client.catalog(q, cursor, signal),
  });
  useAccess(result.error);
  return (
    <>
      <div className="page-heading">
        <div>
          <p className="eyebrow">Discover · understand · use</p>
          <h1>Catalog</h1>
          <p>Named data products with explicit meaning and reviewed versions.</p>
        </div>
        <Link className="button secondary" to="/drafts/new">
          Create draft
        </Link>
      </div>
      <form
        className="search"
        onSubmit={(event) => {
          event.preventDefault();
          setParams(search ? { q: search } : {});
        }}
      >
        <label htmlFor="catalog-search">Search available products</label>
        <div className="inline">
          <input
            id="catalog-search"
            type="search"
            maxLength={256}
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
          <button>Search</button>
        </div>
      </form>
      {result.isPending ? (
        <Loading>Loading products…</Loading>
      ) : result.isError ? (
        <ErrorNotice error={result.error} retry={() => void result.refetch()} />
      ) : (
        <>
          {!result.data.items.length ? (
            <p>No products available for this account.</p>
          ) : (
            <ul className="catalog-grid">
              {result.data.items.map((item) => (
                <li className="panel product-card" key={`${item.product_id}:${item.version}`}>
                  <p className="eyebrow">
                    {item.domain ?? 'Data product'} · {item.status}
                  </p>
                  <h2>
                    <Link
                      to={`/products/${encodeURIComponent(item.product_id)}?version=${encodeURIComponent(item.version)}`}
                    >
                      {item.name}
                    </Link>
                  </h2>
                  <p>{item.description ?? 'Description not provided in this view.'}</p>
                  <div className="card-footer">
                    <span>Version {item.version}</span>
                    <span>{item.grain ?? 'Authorized view'}</span>
                  </div>
                </li>
              ))}
            </ul>
          )}
          {result.data.next_cursor && (
            <button
              className="secondary"
              onClick={() => setParams({ q, cursor: result.data.next_cursor! })}
            >
              Next products
            </button>
          )}
          {cursor && (
            <button className="secondary" onClick={() => setParams(q ? { q } : {})}>
              First products
            </button>
          )}
        </>
      )}
    </>
  );
}
