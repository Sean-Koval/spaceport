import { useEffect, useRef, useState } from 'react';
import { Link, useBlocker, useNavigate, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useForm } from 'react-hook-form';
import { useGateway } from '../auth';
import { useAccess, useInvocation } from '../access';
import { GatewayError, type Model } from '../api';
import { StructuredField } from '../StructuredField';
import { matchesSchema, namedSchema, schemaDefault, record } from '../form-schema';
import { Value, ConnectorStatus } from '../metadata';
import { Loading, ErrorNotice, Confirm } from '../ui';
import synthetic from '../../../fixtures/products/revenue.json';
const groups = [
  [
    'Meaning',
    [
      'schema_version',
      'product_id',
      'version',
      'name',
      'description',
      'owner',
      'domain',
      'grain',
      'aggregation',
      'links',
      'examples',
    ],
  ],
  ['Contract', ['inputs', 'outputs', 'filters', 'sort', 'units']],
  ['Time and limits', ['timezone', 'date_boundaries', 'freshness', 'limits']],
  ['Access', ['classification', 'redaction']],
  ['Fixtures', ['fixtures']],
] as const;
const labels: Record<string, string> = {
  name: 'Name',
  description: 'Description',
  product_id: 'Product ID',
  version: 'Version',
  owner: 'Owner',
  domain: 'Domain',
  grain: 'Grain',
  aggregation: 'Aggregation',
  schema_version: 'Schema version',
};
function isManifest(value: unknown): value is Model<'Manifest'> {
  return matchesSchema(value, 'Manifest');
}
function fullDefinition(snapshot: Model<'DraftView'>): Record<string, unknown> | null {
  const definition = { ...snapshot.definition, content_digest: null };
  return snapshot.editable_full_definition && isManifest(definition) ? definition : null;
}
function DocumentEditor({
  snapshot,
  readReady = true,
}: {
  snapshot: Model<'DraftView'> | null;
  readReady?: boolean;
}) {
  const { client, generation, queries } = useGateway();
  const navigate = useNavigate();
  const access = useAccess();
  const isCurrent = useInvocation();
  const initial = snapshot ? fullDefinition(snapshot) : schemaDefault(namedSchema('Manifest'));
  const form = useForm<{ definition: Record<string, unknown> }>({
    defaultValues: { definition: record(initial) ? initial : {} },
  });
  const value = form.watch('definition');
  const dirty = form.formState.isDirty;
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [run, setRun] = useState<Model<'TestRunView'> | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [reloading, setReloading] = useState(false);
  const [reloadOpen, setReloadOpen] = useState(false);
  const [locatorUnavailable, setLocatorUnavailable] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [templateOpen, setTemplateOpen] = useState(false);
  const guard = useRef(false);
  const mounted = useRef(true);
  const leave = useRef(false);
  const editGeneration = useRef(0);
  const isDirty = useRef(dirty);
  isDirty.current = dirty;
  const blocker = useBlocker(() => isDirty.current && !leave.current);
  useEffect(() => {
    mounted.current = true;
    const before = (event: BeforeUnloadEvent) => {
      if (isDirty.current && !leave.current) {
        event.preventDefault();
        event.returnValue = '';
      }
    };
    window.addEventListener('beforeunload', before);
    return () => {
      mounted.current = false;
      window.removeEventListener('beforeunload', before);
    };
  }, []);
  const choices = useQuery({
    queryKey: [generation, 'choices', snapshot?.id],
    queryFn: async ({ signal }) => {
      const bindings: Model<'BindingChoice'>[] = [];
      const policies: Model<'PolicyChoice'>[] = [];
      let bindingCursor: string | undefined;
      let policyCursor: string | undefined;
      for (let page = 0; page < 20; page++) {
        const result = await client.bindings(snapshot?.id, bindingCursor, signal);
        bindings.push(...result.items);
        bindingCursor = result.next_cursor ?? undefined;
        if (!bindingCursor) break;
      }
      for (let page = 0; page < 20; page++) {
        const result = await client.policies(snapshot?.id, policyCursor, signal);
        policies.push(...result.items);
        policyCursor = result.next_cursor ?? undefined;
        if (!policyCursor) break;
      }
      if (bindingCursor || policyCursor) throw new GatewayError(429, 'RESOURCE_LIMIT');
      return { bindings, policies };
    },
    enabled: !snapshot || !!initial,
  });
  useAccess(choices.error);
  const binding = choices.isSuccess
    ? choices.data?.bindings.find(
        (item) =>
          item.binding_id === value.binding_id &&
          item.revision === value.binding_revision &&
          item.adapter_revision === value.adapter_revision &&
          item.model_bundle_digest === value.model_bundle_digest,
      )
    : undefined;
  const policy = choices.isSuccess
    ? choices.data?.policies.find((item) => item.policy_id === value.policy_id)
    : undefined;
  const mayEdit = !snapshot || !!initial;
  const current = !snapshot || snapshot.is_current_revision === true;
  function change(key: string, next: unknown) {
    if (reloading) return;
    leave.current = false;
    editGeneration.current++;
    form.setValue(
      'definition',
      { ...form.getValues('definition'), [key]: next, content_digest: null },
      { shouldDirty: true },
    );
  }
  function template() {
    if (reloading) return;
    leave.current = false;
    editGeneration.current++;
    form.setValue(
      'definition',
      {
        ...structuredClone(synthetic),
        limits: schemaDefault(namedSchema('Limits')),
        content_digest: null,
      },
      { shouldDirty: true },
    );
    setTemplateOpen(false);
  }
  async function save() {
    if (
      guard.current ||
      !readReady ||
      !mayEdit ||
      !current ||
      !binding ||
      !policy ||
      choices.isFetching ||
      conflict ||
      uncertain
    )
      return;
    const definition = { ...form.getValues('definition'), content_digest: null };
    if (!isManifest(definition)) {
      setError(new GatewayError(422, 'INVALID_INPUT'));
      return;
    }
    guard.current = true;
    setSaving(true);
    setError(null);
    try {
      const receipt = await client.saveDraft(
        { manifest: definition, expected_revision: snapshot?.revision ?? 0 },
        snapshot?.id,
      );
      if (!mounted.current || !isCurrent()) return;
      try {
        const fresh = await client.draft(receipt.id);
        if (!mounted.current || !isCurrent()) return;
        if (!fullDefinition(fresh)) {
          setUnavailable(true);
          return;
        }
        queries.setQueryData([generation, 'draft', receipt.id], fresh);
        leave.current = true;
        form.reset({ definition: fullDefinition(fresh)! });
        navigate(`/drafts/${encodeURIComponent(receipt.id)}/edit`, { replace: true });
      } catch (caught) {
        if (access.reject(caught) || !isCurrent()) return;
        setUnavailable(true);
        setError(caught);
      }
    } catch (caught) {
      if (access.reject(caught) || !isCurrent()) return;
      setError(caught);
      setConflict(caught instanceof GatewayError && caught.status === 409);
      if (!(caught instanceof GatewayError) || caught.status >= 500) setUncertain(true);
    } finally {
      guard.current = false;
      setSaving(false);
    }
  }
  async function test() {
    if (guard.current || !readReady || !snapshot?.can_test || !current || dirty || uncertain)
      return;
    guard.current = true;
    setTesting(true);
    setError(null);
    const started = editGeneration.current;
    try {
      const tested = await client.testDraft(snapshot.id);
      if (!isCurrent()) return;
      setRun(tested);
      if (editGeneration.current !== started) {
        /* Remains explicitly historical while local form is dirty. */
      }
    } catch (caught) {
      if (access.reject(caught) || !isCurrent()) return;
      setError(caught);
      setUncertain(true);
    } finally {
      guard.current = false;
      setTesting(false);
    }
  }
  async function reload() {
    if (!snapshot || guard.current) return;
    guard.current = true;
    setReloading(true);
    setError(null);
    setReloadOpen(false);
    try {
      const old = await client.draft(snapshot.id);
      if (!isCurrent()) return;
      if (!old.current_draft_id) {
        setLocatorUnavailable(true);
        return;
      }
      const fresh = await client.draft(old.current_draft_id);
      if (!mounted.current || !isCurrent()) return;
      const next = fullDefinition(fresh);
      if (!next) {
        setUnavailable(true);
        return;
      }
      leave.current = true;
      form.reset({ definition: next });
      setConflict(false);
      setUncertain(false);
      setRun(null);
      queries.setQueryData([generation, 'draft', fresh.id], fresh);
      navigate(`/drafts/${encodeURIComponent(fresh.id)}/edit`, { replace: true });
    } catch (caught) {
      if (access.reject(caught) || !isCurrent()) return;
      setError(caught);
    } finally {
      guard.current = false;
      setReloading(false);
    }
  }
  if (unavailable)
    return (
      <>
        <h1>Definition unavailable</h1>
        {!!error && <ErrorNotice error={error} />}
        <Link to="/catalog">Return to catalog</Link>
      </>
    );
  return (
    <>
      <div className="page-heading">
        <div>
          <p className="eyebrow">Versioned definition</p>
          <h1>{snapshot ? 'Edit definition' : 'Create definition'}</h1>
          <p>
            {snapshot
              ? `Revision ${snapshot.revision} · ${snapshot.product_id}`
              : 'New product · expected revision 0'}
          </p>
        </div>
        <div className="inline">
          <Link to="/drafts">Back to saved drafts</Link>
          {snapshot && (
            <Link to={`/drafts/${encodeURIComponent(snapshot.id)}/review`}>Open saved review</Link>
          )}
        </div>
      </div>
      {!!error && <ErrorNotice error={error} />}
      {!mayEdit ? (
        <>
          <p className="notice">
            This is a read-only authorized projection. Full replacement is unavailable to this
            account.
          </p>
          <Value value={snapshot?.definition} />
        </>
      ) : (
        <>
          {(!current || conflict || uncertain) && (
            <div className="notice warning">
              <p>
                {uncertain
                  ? 'Save or test outcome not yet confirmed. Reload the authorized current revision before another mutation.'
                  : 'The current revision must be reloaded before saving again.'}
              </p>
              {snapshot ? (
                <button
                  disabled={reloading || saving || testing}
                  onClick={() => (dirty ? setReloadOpen(true) : void reload())}
                >
                  {reloading ? 'Reloading current revision…' : 'Reload current revision'}
                </button>
              ) : (
                <p>
                  For a new product, an authorized current draft URL is needed to reconcile this
                  outcome. Do not create it again blindly.
                </p>
              )}
            </div>
          )}
          {locatorUnavailable && (
            <p>
              The current revision is unavailable to this account.{' '}
              <Link to="/catalog">Return to catalog</Link>
            </p>
          )}
          {choices.isError && (
            <ErrorNotice error={choices.error} retry={() => void choices.refetch()} />
          )}
          <nav className="section-nav" aria-label="Definition sections">
            {groups.map(([name]) => (
              <a key={name} href={`#section-${name.replaceAll(' ', '-')}`}>
                {name}
              </a>
            ))}
            <a href="#section-binding">Binding and policy</a>
          </nav>
          {!snapshot && (
            <div className="notice">
              <p>
                The synthetic template matches this repository’s reviewed demo fixtures. Changes to
                contract fields or fixtures may require operator-approved test assets.
              </p>
              <button
                className="secondary"
                disabled={reloading}
                onClick={() => (dirty ? setTemplateOpen(true) : template())}
              >
                Use synthetic revenue template
              </button>
            </div>
          )}
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void save();
            }}
          >
            <fieldset
              disabled={saving || reloading || !current || uncertain}
              className="editor-fields"
            >
              <legend className="sr-only">Definition values</legend>
              {groups.map(([name, keys]) => (
                <section
                  className="panel editor-section"
                  key={name}
                  id={`section-${name.replaceAll(' ', '-')}`}
                >
                  <h2>{name}</h2>
                  {name === 'Time and limits' && (
                    <p>
                      Dates are half-open [start,end) in the declared timezone. Freshness measures
                      source-load age, not event recency.
                    </p>
                  )}
                  {name === 'Fixtures' && (
                    <p>
                      Fixture data never selects a principal. Permission-negative tests use fixed
                      server-approved accounts. Decimal amounts and tolerances remain exact strings.
                    </p>
                  )}
                  {keys.map((key) => (
                    <div key={key}>
                      {key === 'product_id' && snapshot ? (
                        <div className="field">
                          <label htmlFor="product-id">Product ID</label>
                          <input id="product-id" value={snapshot.product_id} readOnly />
                        </div>
                      ) : (
                        <StructuredField
                          label={labels[key] ?? key}
                          schema={namedSchema('Manifest').properties![key]}
                          value={
                            value[key] ?? schemaDefault(namedSchema('Manifest').properties![key])
                          }
                          onChange={(next) => change(key, next)}
                        />
                      )}
                    </div>
                  ))}
                </section>
              ))}
              <section className="panel" id="section-binding">
                <h2>Binding and policy</h2>
                <p>
                  Only current approved choices are selectable. They do not prove live source
                  readiness.
                </p>
                {choices.isPending ? (
                  <Loading>Loading approved choices…</Loading>
                ) : (
                  <>
                    {!choices.data?.bindings.length && (
                      <p>No approved binding is available for this account.</p>
                    )}
                    <label htmlFor="binding-choice">Approved binding revision</label>
                    <select
                      id="binding-choice"
                      value={binding ? `${binding.binding_id}:${binding.revision}` : ''}
                      onChange={(event) => {
                        if (reloading) return;
                        const selected = choices.data?.bindings.find(
                          (item) => `${item.binding_id}:${item.revision}` === event.target.value,
                        );
                        if (selected) {
                          leave.current = false;
                          editGeneration.current++;
                          form.setValue(
                            'definition',
                            {
                              ...value,
                              binding_id: selected.binding_id,
                              binding_revision: selected.revision,
                              adapter_revision: selected.adapter_revision,
                              model_bundle_digest: selected.model_bundle_digest,
                              content_digest: null,
                            },
                            { shouldDirty: true },
                          );
                        }
                      }}
                    >
                      <option value="">Choose an approved binding</option>
                      {choices.data?.bindings.map((item) => (
                        <option
                          key={`${item.binding_id}:${item.revision}`}
                          value={`${item.binding_id}:${item.revision}`}
                        >
                          {item.binding_id} · Revision {item.revision}
                        </option>
                      ))}
                    </select>
                    {!choices.data?.policies.length && (
                      <p>No attachable policy is available for this account.</p>
                    )}
                    <label htmlFor="policy-choice">Approved access policy</label>
                    <select
                      id="policy-choice"
                      value={policy?.policy_id ?? ''}
                      onChange={(event) => change('policy_id', event.target.value)}
                    >
                      <option value="">Choose an approved policy</option>
                      {choices.data?.policies.map((item) => (
                        <option key={item.policy_id} value={item.policy_id}>
                          {item.policy_id} · Revision {item.revision}
                        </option>
                      ))}
                    </select>
                  </>
                )}
              </section>
            </fieldset>
            <details className="panel">
              <summary>Read-only manifest preview</summary>
              <Value value={value} />
            </details>
            <div className="action-bar">
              <p role="status">
                {dirty
                  ? 'Unsaved changes · save and test this revision before review.'
                  : snapshot
                    ? `Saved revision ${snapshot.revision}.`
                    : 'New definition · save before testing.'}
                {run && ` Tests ${run.status} for saved revision ${snapshot?.revision}.`}
              </p>
              <div className="inline">
                <button
                  type="submit"
                  disabled={
                    !current ||
                    !readReady ||
                    saving ||
                    reloading ||
                    testing ||
                    !binding ||
                    !policy ||
                    choices.isFetching ||
                    conflict ||
                    uncertain
                  }
                >
                  {saving ? 'Saving…' : 'Save draft'}
                </button>
                <button
                  type="button"
                  className="secondary"
                  disabled={
                    !readReady ||
                    !snapshot?.can_test ||
                    !current ||
                    dirty ||
                    saving ||
                    reloading ||
                    testing ||
                    uncertain
                  }
                  onClick={() => void test()}
                >
                  {testing ? 'Tests running…' : 'Run tests'}
                </button>
                <button
                  type="button"
                  className="secondary"
                  disabled={!snapshot || dirty || saving || testing || reloading || uncertain}
                  onClick={() =>
                    snapshot && navigate(`/drafts/${encodeURIComponent(snapshot.id)}/review`)
                  }
                >
                  Review publication
                </button>
              </div>
            </div>
          </form>
          <ConnectorStatus />
        </>
      )}
      <Confirm
        open={reloadOpen}
        onOpenChange={setReloadOpen}
        title="Discard unsaved changes?"
        description="Reload the current authorized revision. Your unsaved local changes will be discarded."
        confirmLabel="Discard and reload"
        pending={reloading}
        onConfirm={() => void reload()}
      />
      <Confirm
        open={templateOpen}
        onOpenChange={setTemplateOpen}
        title="Replace this unsaved definition?"
        description="Use the synthetic revenue template in this unpublished editor."
        confirmLabel="Use template"
        disabled={reloading}
        onConfirm={template}
      />
      <Confirm
        open={blocker.state === 'blocked'}
        onOpenChange={(next) => {
          if (!next && blocker.state === 'blocked') blocker.reset();
        }}
        title="Discard unsaved changes?"
        description="Leaving this page discards your unsaved definition changes."
        confirmLabel="Discard and leave"
        onConfirm={() => {
          if (blocker.state === 'blocked') blocker.proceed();
        }}
      />
    </>
  );
}
function ExistingEditor({ id }: { id: string }) {
  const { client, generation } = useGateway();
  const query = useQuery({
    queryKey: [generation, 'draft', id],
    queryFn: ({ signal }) => client.draft(id, signal),
    refetchOnWindowFocus: true,
  });
  useAccess(query.error);
  if (query.isPending) return <Loading>Loading draft…</Loading>;
  if (!query.data)
    return (
      <>
        <h1>Definition unavailable</h1>
        <ErrorNotice error={query.error} retry={() => void query.refetch()} />
      </>
    );
  return (
    <>
      {query.isError && (
        <div className="notice warning">
          <p>
            Your local edits are preserved. Retry the draft read before saving or running tests.
          </p>
          <ErrorNotice error={query.error} retry={() => void query.refetch()} />
        </div>
      )}
      <DocumentEditor
        key={query.data.id}
        snapshot={query.data}
        readReady={query.isSuccess && !query.isFetching}
      />
    </>
  );
}
export default function Editor() {
  const { draftId } = useParams();
  return draftId ? (
    <ExistingEditor key={draftId} id={draftId} />
  ) : (
    <DocumentEditor snapshot={null} />
  );
}
