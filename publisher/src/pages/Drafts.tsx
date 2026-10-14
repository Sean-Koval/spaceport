import { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useGateway } from '../auth';
import { useAccess } from '../access';
import { ErrorNotice, Loading } from '../ui';
import { Icon } from '../Icon';

export default function Drafts() {
  const { client, generation } = useGateway();
  const [params, setParams] = useSearchParams();
  const q = params.get('q') ?? '';
  const cursor = params.get('cursor') ?? undefined;
  const [search, setSearch] = useState(q);
  useEffect(() => setSearch(q), [q]);
  const result = useQuery({
    queryKey: [generation, 'drafts', q, cursor],
    queryFn: ({ signal }) => client.drafts(q, cursor, signal),
  });
  useAccess(result.error);
  return (
    <>
      <div className="page-heading">
        <div>
          <p className="eyebrow">Authoring and review</p>
          <h1>Saved drafts</h1>
          <p>Resume a saved definition or review its current evidence.</p>
        </div>
        <Link className="button secondary" to="/drafts/new">
          <Icon name="plus" />
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
        <label htmlFor="draft-search">Search saved drafts</label>
        <div className="inline">
          <input
            id="draft-search"
            type="search"
            maxLength={256}
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
          <button>
            <Icon name="search" />
            Search
          </button>
          <button
            className="secondary"
            type="button"
            disabled={result.isFetching}
            onClick={() => void result.refetch()}
          >
            Refresh drafts
          </button>
        </div>
      </form>
      <p>
        Only your authorized latest saved revisions appear here. Published revisions are excluded.
        Open a review to check test evidence and publication requirements.
      </p>
      {result.isPending || result.isFetching ? (
        <Loading>Loading saved drafts…</Loading>
      ) : result.isError ? (
        <ErrorNotice error={result.error} retry={() => void result.refetch()} />
      ) : (
        <>
          {!result.data.items.length ? (
            <p>No saved drafts available for this account.</p>
          ) : (
            <ul className="catalog-grid">
              {result.data.items.map((item) => (
                <li className="panel product-card" key={item.id}>
                  <div className="card-meta">
                    <p className="eyebrow">Saved definition</p>
                    <span className="product-status">Revision {item.revision}</span>
                  </div>
                  <h2>
                    <Link to={`/drafts/${encodeURIComponent(item.id)}/review`}>
                      {item.name}
                      <Icon name="arrow-up-right" />
                    </Link>
                  </h2>
                  <p>
                    {item.product_id} · Version {item.version}
                  </p>
                  <div className="card-footer">
                    {item.editable_full_definition && (
                      <Link to={`/drafts/${encodeURIComponent(item.id)}/edit`}>Resume editing</Link>
                    )}
                    <Link to={`/drafts/${encodeURIComponent(item.id)}/review`}>Review draft</Link>
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
              Next drafts
            </button>
          )}
          {cursor && (
            <button className="secondary" onClick={() => setParams(q ? { q } : {})}>
              First drafts
            </button>
          )}
        </>
      )}
    </>
  );
}
