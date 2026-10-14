import { useEffect, useRef, type ReactNode } from 'react';
import { Dialog, Button, Flex } from '@radix-ui/themes';
import { GatewayError } from './api';
export function ErrorNotice({ error, retry }: { error: unknown; retry?: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    ref.current?.focus();
  }, [error]);
  return (
    <div ref={ref} tabIndex={-1} role="alert" className="notice warning">
      <p>
        {error instanceof GatewayError
          ? error.message
          : 'The request could not be completed. Check the connection and try a fresh read.'}
      </p>
      {error instanceof GatewayError && error.requestId && <p>Request ID: {error.requestId}</p>}
      {retry && <button onClick={retry}>Retry read</button>}
    </div>
  );
}
export function Loading({ children }: { children: ReactNode }) {
  return (
    <p role="status" aria-busy="true">
      {children}
    </p>
  );
}
export function Confirm({
  open,
  onOpenChange,
  title,
  description,
  children,
  confirmLabel,
  onConfirm,
  disabled = false,
  pending = false,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: string;
  children?: ReactNode;
  confirmLabel: string;
  onConfirm: () => void;
  disabled?: boolean;
  pending?: boolean;
}) {
  const trigger = useRef<HTMLElement | null>(null);
  return (
    <Dialog.Root
      open={open}
      onOpenChange={(next) => {
        if (!pending) onOpenChange(next);
      }}
    >
      <Dialog.Content
        maxWidth="600px"
        aria-describedby="confirm-description"
        onOpenAutoFocus={() => {
          if (document.activeElement instanceof HTMLElement)
            trigger.current = document.activeElement;
        }}
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          const target = trigger.current;
          if (target?.isConnected && !target.matches(':disabled')) target.focus();
          else
            (
              document.querySelector<HTMLElement>('[role="alert"]') ??
              document.getElementById('main')
            )?.focus();
        }}
      >
        <Dialog.Title>{title}</Dialog.Title>
        <Dialog.Description id="confirm-description">{description}</Dialog.Description>
        {children}
        <Flex gap="3" mt="4" justify="end">
          <Dialog.Close>
            <Button variant="soft" color="gray" disabled={pending}>
              Cancel
            </Button>
          </Dialog.Close>
          <Button disabled={disabled || pending} onClick={onConfirm}>
            {pending ? 'Submitting…' : confirmLabel}
          </Button>
        </Flex>
      </Dialog.Content>
    </Dialog.Root>
  );
}
