import type { components } from '../generated/api';
import { Rows, Value } from '../metadata';
import { LifecycleActions } from '../Lifecycle';
type Run = components['schemas']['TestRunView'];
type Diff = components['schemas']['VersionDiff'];
export function FixtureChecks({ run }: { run: Run }) {
  return (
    <section aria-labelledby="fixture-checks">
      <h2 id="fixture-checks">Fixture checks · {run.status}</h2>
      <p>The gateway reports the full run outcome. Visible rows are permission-filtered.</p>
      {[...run.checks]
        .sort((a, b) => Number(a.passed) - Number(b.passed))
        .map((check) => (
          <article className="panel" key={check.fixture_id}>
            <h3>
              {check.fixture_id} · {check.passed ? 'Passed' : 'Failed'}
            </h3>
            <p>{check.kind.replaceAll('_', ' ')}</p>
            {check.kind === 'permission_negative' && check.passed && (
              <p>Expected access denial verified</p>
            )}
            <dl className="facts">
              <dt>Expected error</dt>
              <dd>{check.expected_error ?? 'No error'}</dd>
              <dt>Actual error</dt>
              <dd>{check.actual_error ?? 'No error'}</dd>
            </dl>
            {check.expected === undefined ? (
              <p>Expected values not provided in this view.</p>
            ) : (
              <Rows rows={check.expected} caption={`${check.fixture_id}: expected values`} />
            )}
            {check.actual === undefined ? (
              <p>Actual values not provided in this view.</p>
            ) : (
              <Rows rows={check.actual} caption={`${check.fixture_id}: actual values`} />
            )}
            <details>
              <summary>Tolerances and watermark evidence</summary>
              <dl className="facts">
                <dt>Absolute tolerance</dt>
                <dd>{check.absolute_tolerance ?? 'Not provided in this view'}</dd>
                <dt>Relative tolerance</dt>
                <dd>{check.relative_tolerance ?? 'Not provided in this view'}</dd>
                <dt>Fixture reference watermark</dt>
                <dd>{check.reference_watermark ?? 'Not provided in this view'}</dd>
                <dt>Observed source-load watermark</dt>
                <dd>{check.observed_source_load_watermark ?? 'Not provided in this view'}</dd>
                <dt>Fixture freshness</dt>
                <dd>{check.freshness_status ?? 'Not provided in this view'}</dd>
              </dl>
            </details>
          </article>
        ))}
    </section>
  );
}
const sections: Record<string, string> = {
  name: 'Meaning',
  description: 'Meaning',
  owner: 'Meaning',
  domain: 'Meaning',
  links: 'Meaning',
  examples: 'Meaning',
  aggregation: 'Meaning',
  grain: 'Meaning',
  inputs: 'Contract',
  outputs: 'Contract',
  filters: 'Contract',
  sort: 'Contract',
  units: 'Contract',
  timezone: 'Time and limits',
  date_boundaries: 'Time and limits',
  freshness: 'Time and limits',
  limits: 'Time and limits',
  binding_id: 'Binding',
  binding_revision: 'Binding',
  adapter_revision: 'Binding',
  model_bundle_digest: 'Binding',
  policy_id: 'Access',
  classification: 'Access',
  redaction: 'Access',
  fixtures: 'Fixtures',
};
export function DefinitionDiff({ diff }: { diff: Diff }) {
  const groups = new Map<string, Diff['changes']>();
  for (const change of diff.changes) {
    const group = sections[change.path.split('/')[1]] ?? 'Identity';
    groups.set(group, [...(groups.get(group) ?? []), change]);
  }
  return (
    <section aria-labelledby="definition-diff">
      <h2 id="definition-diff">Definition changes</h2>
      <p>{diff.before_digest === null ? 'First publication' : 'Current published definition'}</p>
      {diff.before_digest && <Value value={diff.before_digest} />}
      {!diff.changes.length && <p>No definition changes.</p>}
      {[...groups].map(([group, changes]) => (
        <section key={group}>
          <h3>{group}</h3>
          {changes.map((change, index) => (
            <article className="panel diff" key={`${change.path}-${index}`}>
              <h4>
                {change.path
                  .split('/')
                  .slice(1)
                  .map((part) => part.replaceAll('~1', '/').replaceAll('~0', '~'))
                  .join(' / ')}
              </h4>
              <p className="badge">{change.kind}</p>
              <div className="diff-grid">
                <div>
                  <h5>Current</h5>
                  {change.kind === 'added' ? <p>Not present</p> : <Value value={change.before} />}
                </div>
                <div>
                  <h5>Proposed</h5>
                  {change.kind === 'removed' ? <p>Removed</p> : <Value value={change.after} />}
                </div>
              </div>
            </article>
          ))}
        </section>
      ))}
    </section>
  );
}
const staleCopy: Record<string, string> = {
  missing: 'No test evidence is available for this draft.',
  draft: 'The reviewed draft or its test evidence does not match the current draft.',
  binding: 'The approved binding is unavailable or no longer matches the tested binding.',
  policy: 'Current policy or fixed test-account permissions no longer match this evidence.',
  fixtures:
    'The approved fixture configuration or recorded execution evidence is unavailable or no longer matches.',
  failed: 'The latest test run did not pass.',
};
export function StaleEvidence({ reasons }: { reasons: readonly string[] }) {
  if (!reasons.length) return null;
  return (
    <section className="notice warning" aria-labelledby="stale-evidence">
      <h2 id="stale-evidence">Evidence needs attention</h2>
      <ul>
        {reasons.map((reason, index) => (
          <li key={`${reason}-${index}`}>
            {staleCopy[reason] ?? 'Evidence could not be verified.'}
          </li>
        ))}
      </ul>
      <p>Review this revision and retest when the approved assets and permissions are available.</p>
    </section>
  );
}

import { useState, useRef } from 'react';
import { Link, useParams, useSearchParams, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useGateway } from '../auth';
import { useAccess, useInvocation } from '../access';
import { GatewayError, type Model } from '../api';
import { ErrorNotice, Loading, Confirm } from '../ui';
function coherent(draft: Model<'DraftView'>, diff: Diff, run: Run | null) {
  return (
    draft.is_current_revision === true &&
    diff.draft_id === draft.id &&
    diff.product_id === draft.product_id &&
    diff.revision === draft.revision &&
    diff.after_digest === draft.digest &&
    Array.isArray(diff.stale_evidence) &&
    diff.stale_evidence.length === 0 &&
    run !== null &&
    diff.latest_test_run_id === run.id &&
    run.draft_id === draft.id &&
    run.draft_digest === draft.digest &&
    run.status === 'passed' &&
    run.checks.some((check) => check.kind === 'permission_negative' && check.passed) &&
    run.checks.every((check) => check.passed)
  );
}
function ReviewPage({ id }: { id: string }) {
  const { client, generation, queries } = useGateway();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const access = useAccess();
  const isCurrent = useInvocation();
  const requestedRun = params.get('run');
  const [reason, setReason] = useState('');
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [outcome, setOutcome] = useState<
    'idle' | 'pending' | 'conflict' | 'uncertain' | 'confirmed'
  >('idle');
  const [testing, setTesting] = useState(false);
  const [testUncertain, setTestUncertain] = useState(false);
  const [locatorUnavailable, setLocatorUnavailable] = useState(false);
  const [published, setPublished] = useState<{
    product_id: string;
    version: string;
    digest: string;
  } | null>(null);
  const busy = useRef(false);
  const intended = useRef<{ product_id: string; version: string; digest: string } | null>(null);
  const review = useQuery({
    queryKey: [generation, 'review', id, requestedRun],
    queryFn: async ({ signal }) => {
      const [draft, initial] = await Promise.all([
        client.draft(id, signal),
        client.diff(id, signal),
      ]);
      const runId = requestedRun ?? initial.latest_test_run_id;
      const run = runId ? await client.testRun(id, runId, signal) : null;
      const diff = await client.diff(id, signal);
      return { draft, diff, run };
    },
  });
  useAccess(review.error);
  if (review.isPending) return <Loading>Loading publication review…</Loading>;
  if (review.isError)
    return (
      <>
        <h1>Review unavailable</h1>
        <ErrorNotice error={review.error} retry={() => void review.refetch()} />
        <Link to="/catalog">Return to catalog</Link>
      </>
    );
  const { draft, diff, run } = review.data;
  const isCoherent = coherent(draft, diff, run);
  const ready =
    draft.can_publish === true &&
    isCoherent &&
    !review.isFetching &&
    !testing &&
    !testUncertain &&
    outcome === 'idle' &&
    navigator.onLine !== false;
  const version = typeof draft.definition.version === 'string' ? draft.definition.version : null;
  async function publish() {
    if (busy.current || !ready || !reason.trim() || !version) return;
    busy.current = true;
    setError(null);
    setOutcome('pending');
    intended.current = { product_id: draft.product_id, version, digest: draft.digest };
    try {
      const receipt = await client.publish(id, {
        tested_digest: draft.digest,
        expected_revision: draft.revision,
        reason,
      });
      if (!isCurrent()) return;
      if (
        receipt.product_id !== draft.product_id ||
        receipt.version !== version ||
        receipt.digest !== draft.digest
      ) {
        setOutcome('uncertain');
        return;
      }
      setPublished(receipt);
      setOutcome('confirmed');
      await queries.invalidateQueries({ queryKey: [generation, 'catalog'] });
    } catch (caught) {
      if (access.reject(caught) || !isCurrent()) return;
      setError(caught);
      setOutcome(
        caught instanceof GatewayError && caught.status === 409 ? 'conflict' : 'uncertain',
      );
    } finally {
      busy.current = false;
      setOpen(false);
    }
  }
  async function reconcile() {
    const target = intended.current;
    if (!target || busy.current) return;
    busy.current = true;
    setError(null);
    try {
      const found = await client.product(target.product_id, target.version);
      if (!isCurrent()) return;
      if (found.version === target.version && found.digest === target.digest) {
        setPublished(target);
        setOutcome('confirmed');
      } else setError(new GatewayError(409, 'CONFLICT'));
    } catch (caught) {
      if (access.reject(caught) || !isCurrent()) return;
      setError(caught);
    } finally {
      busy.current = false;
    }
  }
  async function reload() {
    if (busy.current) return;
    busy.current = true;
    setError(null);
    try {
      const current = await client.draft(id);
      if (!isCurrent()) return;
      if (!current.current_draft_id) {
        setLocatorUnavailable(true);
        return;
      }
      setOutcome('idle');
      setTestUncertain(false);
      setReason('');
      setLocatorUnavailable(false);
      if (current.current_draft_id !== id)
        navigate(`/drafts/${encodeURIComponent(current.current_draft_id)}/review`);
      else await review.refetch();
    } catch (caught) {
      if (access.reject(caught) || !isCurrent()) return;
      setError(caught);
    } finally {
      busy.current = false;
    }
  }
  async function test() {
    if (busy.current || !draft.can_test || !draft.is_current_revision) return;
    busy.current = true;
    setTesting(true);
    setError(null);
    try {
      await client.testDraft(id);
      if (!isCurrent()) return;
      setTestUncertain(false);
      await review.refetch();
    } catch (caught) {
      if (access.reject(caught) || !isCurrent()) return;
      setError(caught);
      setTestUncertain(true);
    } finally {
      busy.current = false;
      setTesting(false);
    }
  }
  return (
    <>
      <p className="eyebrow">Evidence-first review</p>
      <h1>Review publication</h1>
      <p>
        {String(draft.definition.name ?? draft.product_id)} · Version {version ?? 'unavailable'} ·
        Revision {draft.revision}
      </p>
      <p>
        Draft {draft.id} ·{' '}
        {draft.is_current_revision ? 'Current saved revision' : 'Historical revision'}
      </p>
      {!!error && <ErrorNotice error={error} />}
      {outcome === 'confirmed' && published ? (
        <div className="notice success" role="status">
          <h2>Published version {published.version}</h2>
          <p>The gateway confirmed this exact definition.</p>
          <Value value={published.digest} />
          <Link
            to={`/products/${encodeURIComponent(published.product_id)}?version=${encodeURIComponent(published.version)}`}
          >
            Open published product
          </Link>
        </div>
      ) : (
        <>
          {outcome === 'uncertain' && (
            <div className="notice warning">
              <p>Publication outcome not yet confirmed.</p>
              <p>
                The request may have committed. Check the exact version before taking another
                action.
              </p>
              <button onClick={() => void reconcile()}>Check publication outcome</button>
            </div>
          )}
          {testUncertain && (
            <p role="status">
              Test outcome not yet confirmed. Reload this review to inspect the recorded evidence;
              tests will not restart automatically.
            </p>
          )}
          {(!draft.is_current_revision || outcome === 'conflict' || testUncertain) && (
            <div className="notice warning">
              <p>This revision needs a fresh review.</p>
              <button onClick={() => void reload()}>Reload current revision</button>
            </div>
          )}
          {locatorUnavailable && (
            <p>
              The current revision is unavailable to this account.{' '}
              <Link to="/catalog">Return to catalog</Link>
            </p>
          )}
          <StaleEvidence reasons={diff.stale_evidence ?? ['missing']} />
          {!isCoherent && !diff.stale_evidence?.length && (
            <p className="notice warning">
              The reviewed draft and assessed test evidence do not form a current matching revision.
              Reload and review before publishing.
            </p>
          )}
          {requestedRun && requestedRun !== diff.latest_test_run_id && (
            <p>
              Historical test evidence.{' '}
              <button onClick={() => setParams({})}>Review latest evidence</button>
            </p>
          )}
          {run && <FixtureChecks run={run} />}
          <DefinitionDiff diff={diff} />
          <details className="panel">
            <summary>Authorized evidence pins</summary>
            <dl className="facts">
              <dt>Saved digest</dt>
              <dd>{draft.digest}</dd>
              {run && (
                <>
                  <dt>Test run</dt>
                  <dd>{run.id}</dd>
                  <dt>Tested digest</dt>
                  <dd>{run.draft_digest}</dd>
                  <dt>Tested binding digest</dt>
                  <dd>{run.binding_digest}</dd>
                  <dt>Policy</dt>
                  <dd>
                    {run.policy_id} · Revision {run.policy_revision}
                  </dd>
                  <dt>Fixture version</dt>
                  <dd>{run.fixture_version}</dd>
                  <dt>Test recorded</dt>
                  <dd>{run.created_at}</dd>
                </>
              )}
            </dl>
          </details>
          <section className="action-bar" aria-label="Publication actions">
            <p>
              {draft.can_publish
                ? 'Publishing requires current, matching evidence and a reason.'
                : 'Publishing is unavailable for this account.'}
            </p>
            <div className="inline">
              {draft.editable_full_definition && (
                <Link className="button secondary" to={`/drafts/${encodeURIComponent(id)}/edit`}>
                  Edit draft
                </Link>
              )}
              <button
                className="secondary"
                disabled={
                  !draft.can_test ||
                  !draft.is_current_revision ||
                  testing ||
                  testUncertain ||
                  outcome !== 'idle'
                }
                onClick={() => void test()}
              >
                {testing ? 'Tests running…' : 'Run tests'}
              </button>
              <button
                className="secondary"
                disabled={testing || outcome === 'pending'}
                onClick={() => void review.refetch()}
              >
                Refresh review
              </button>
              <button disabled={!ready || !version} onClick={() => setOpen(true)}>
                Publish
              </button>
            </div>
          </section>
          <Confirm
            open={open}
            onOpenChange={setOpen}
            title="Publish this reviewed version?"
            description={`Publish ${draft.product_id} version ${version}, draft revision ${draft.revision}. The gateway rechecks the exact tested digest, policy and binding before committing.`}
            confirmLabel="Confirm publication"
            onConfirm={() => void publish()}
            disabled={!ready || !reason.trim()}
            pending={outcome === 'pending'}
          >
            <label htmlFor="transition-reason">Transition reason</label>
            <textarea
              id="transition-reason"
              maxLength={1024}
              value={reason}
              disabled={outcome === 'pending'}
              onChange={(event) => setReason(event.target.value)}
            />
            <p>No credentials or private source information.</p>
          </Confirm>
        </>
      )}
      {version && (
        <LifecycleActions
          productId={draft.product_id}
          version={version}
          canPublish={draft.can_publish === true}
        />
      )}
    </>
  );
}
export default function Review() {
  const { draftId } = useParams();
  return draftId ? <ReviewPage key={draftId} id={draftId} /> : <h1>Review unavailable</h1>;
}
