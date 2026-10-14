import { useRef, useState } from 'react';
import { useGateway } from './auth';
import { GatewayError, type Model } from './api';
import { Confirm, ErrorNotice } from './ui';
import { isAccessDenied, useAccess, useInvocation } from './access';
type Action = Model<'LifecycleRequest'>['action'];
const consequences: Record<Action, string> = {
  deprecate:
    'Marks this version deprecated. Consumers see a warning and the version remains queryable.',
  withdraw: 'Blocks new execution and retrieval of stored results for this version.',
  rollback:
    'Moves the current pointer to an existing compatible published version. Its immutable definition is not edited.',
};
export function LifecycleActions({
  productId,
  version,
  canPublish,
}: {
  productId: string;
  version: string;
  canPublish: boolean;
}) {
  const { client, queries, generation } = useGateway();
  const [action, setAction] = useState<Action>('deprecate');
  const [target, setTarget] = useState<Model<'ProductDescription'> | null>(null);
  const access = useAccess();
  const isCurrent = useInvocation();
  const [reason, setReason] = useState('');
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [confirmed, setConfirmed] = useState('');
  const guard = useRef(false);
  const intended = useRef<{
    action: Action;
    product_id: string;
    version: string;
    digest: string;
  } | null>(null);
  if (!canPublish) return null;
  async function openExisting() {
    if (guard.current) return;
    guard.current = true;
    setPending(true);
    setTarget(null);
    setError(null);
    const recovery = uncertain ? intended.current : null;
    const targetId = recovery?.product_id ?? productId;
    const targetVersion = recovery?.version ?? version;
    try {
      const found = await client.product(targetId, targetVersion);
      if (!isCurrent()) return;
      if (
        found.product_id !== targetId ||
        found.version !== targetVersion ||
        !['published', 'deprecated'].includes(found.status)
      )
        throw new GatewayError(404, 'NOT_FOUND_OR_FORBIDDEN');
      if (recovery && found.digest !== recovery.digest) throw new GatewayError(409, 'CONFLICT');
      setTarget(found);
      // This DTO can prove deprecation of the exact immutable version. It cannot
      // establish a rollback pointer or distinguish withdrawal from denied access.
      if (recovery?.action === 'deprecate' && found.status === 'deprecated') {
        setConfirmed(`Authorized version state: deprecated ${targetVersion}.`);
        setReason('');
        setUncertain(false);
        intended.current = null;
      }
    } catch (caught) {
      if (!isCurrent()) return;
      // A publisher may read a draft but lack consumer access to its published version.
      // This lookup denies only that lifecycle target, never infers a guessed version.
      if (isAccessDenied(caught)) {
        void queries.cancelQueries({ queryKey: [generation, 'product', targetId] });
        queries.removeQueries({ queryKey: [generation, 'product', targetId] });
        void queries.cancelQueries({ queryKey: [generation, 'execution'] });
        queries.removeQueries({ queryKey: [generation, 'execution'] });
      }
      setError(caught);
    } finally {
      guard.current = false;
      setPending(false);
    }
  }
  async function submit() {
    if (guard.current || !reason.trim() || !target || uncertain || !canPublish) return;
    guard.current = true;
    setPending(true);
    setError(null);
    setConfirmed('');
    intended.current = {
      action,
      product_id: target.product_id,
      version: target.version,
      digest: target.digest,
    };
    try {
      await client.lifecycle(target.product_id, { action, version: target.version, reason });
      if (!isCurrent()) return;
      setConfirmed(`Gateway confirmed: ${action} ${version}.`);
      intended.current = null;
      setReason('');
      setTarget(null);
      await queries.invalidateQueries({ queryKey: [generation, 'catalog'] });
      await queries.invalidateQueries({ queryKey: [generation, 'product', productId] });
    } catch (caught) {
      if (access.reject(caught) || !isCurrent()) return;
      setError(caught);
      if (!(caught instanceof GatewayError) || caught.status >= 500) setUncertain(true);
    } finally {
      guard.current = false;
      setPending(false);
      setOpen(false);
    }
  }
  return (
    <details className="panel">
      <summary>Lifecycle actions for this product</summary>
      <p>
        These actions affect an already published version. The gateway authorizes the exact target
        again at submission.
      </p>
      {!!error && <ErrorNotice error={error} />}
      <p role="status">{confirmed}</p>
      {uncertain && (
        <p className="notice warning">
          Lifecycle outcome not yet confirmed. Verify the authorized version state before attempting
          another change.
        </p>
      )}
      {(!target || uncertain) && (
        <button className="secondary" disabled={pending} onClick={() => void openExisting()}>
          {pending ? 'Opening existing version…' : 'Open existing published version'}
        </button>
      )}
      {target && (
        <>
          <p>
            Reviewed target: {target.name} · {target.status} · Digest {target.digest}
          </p>
          <label htmlFor="lifecycle-action">Lifecycle action</label>
          <select
            id="lifecycle-action"
            value={action}
            disabled={pending || uncertain || open}
            onChange={(event) => setAction(event.target.value as Action)}
          >
            <option value="deprecate">Deprecate</option>
            <option value="withdraw">Withdraw</option>
            <option value="rollback">Rollback current pointer</option>
          </select>
          <label htmlFor="lifecycle-version">Existing target version</label>
          <input id="lifecycle-version" value={target.version} readOnly />
          <p>{consequences[action]}</p>
          <button
            className="secondary"
            disabled={pending || uncertain || open}
            onClick={() => setOpen(true)}
          >
            Review lifecycle change
          </button>
          <Confirm
            open={open}
            onOpenChange={setOpen}
            title={`${action} ${target.product_id} ${target.version}?`}
            description={consequences[action]}
            confirmLabel="Confirm lifecycle change"
            disabled={!reason.trim()}
            pending={pending}
            onConfirm={() => void submit()}
          >
            <label htmlFor="lifecycle-reason">Lifecycle reason</label>
            <textarea
              id="lifecycle-reason"
              maxLength={1024}
              value={reason}
              disabled={pending}
              onChange={(event) => setReason(event.target.value)}
            />
          </Confirm>
        </>
      )}
    </details>
  );
}
