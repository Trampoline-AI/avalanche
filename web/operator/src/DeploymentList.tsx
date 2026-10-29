import { useVirtualizer } from "@tanstack/react-virtual";
import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { LoaderCircle } from "lucide-react";

import type { WorkspaceDeployment, WorkspaceDeployments } from "./WorkflowWorkspace";
import { useTimelineScrollHandle } from "./useTimelineScrollHandle";

const timestampFormat = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});
const dialogButtonClass =
  "cursor-pointer rounded border px-3 py-2 text-xs focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-acid disabled:cursor-wait disabled:opacity-50";

function SetCurrentDialog({
  item,
  setCurrent,
  pending,
  error,
  onCancel,
  onConfirm,
}: {
  item: WorkspaceDeployment;
  setCurrent: NonNullable<WorkspaceDeployments["setCurrent"]>;
  pending: boolean;
  error?: string;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const id = useId();
  const dialog = useRef<HTMLDivElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    cancel.current?.focus();
  }, []);
  useEffect(() => {
    const focusInside = (): HTMLElement[] => {
      const buttons =
        dialog.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)");
      return buttons?.length ? [...buttons] : [dialog.current!];
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        if (!pending) onCancel();
      } else if (event.key === "Tab") {
        event.preventDefault();
        event.stopPropagation();
        const targets = focusInside();
        const index = targets.indexOf(document.activeElement as HTMLButtonElement);
        targets[(index + (event.shiftKey ? -1 : 1) + targets.length) % targets.length].focus();
      }
    };
    const onFocus = (event: FocusEvent) => {
      if (!dialog.current?.contains(event.target as Node)) focusInside()[0].focus();
    };
    document.addEventListener("keydown", onKeyDown, true);
    document.addEventListener("focusin", onFocus);
    return () => {
      document.removeEventListener("keydown", onKeyDown, true);
      document.removeEventListener("focusin", onFocus);
    };
  }, [pending, onCancel]);

  return createPortal(
    <div
      className="avalanche-operator-ui deployment-confirm-backdrop fixed inset-0 z-[1000] flex items-center justify-center p-4"
      onClick={(event) => {
        event.stopPropagation();
        if (event.target === event.currentTarget && !pending) onCancel();
      }}
    >
      <div
        ref={dialog}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={`${id}-title`}
        aria-describedby={`${id}-body`}
        tabIndex={-1}
        className="deployment-confirm-dialog max-h-full w-full max-w-md overflow-auto rounded border border-line bg-panel p-5 font-mono text-ink shadow-[0_12px_40px_var(--color-shadow-5)]"
      >
        <h2 id={`${id}-title`} className="m-0 mb-3 text-base font-semibold">
          {setCurrent.confirmTitle ?? "Set current deployment?"}
        </h2>
        <div id={`${id}-body`} className="text-xs leading-relaxed">
          <p className="m-0 break-words">{setCurrent.confirmText(item)}</p>
          <p className="mt-2 mb-0 text-secondary">
            Deployment:{" "}
            <span title={item.id}>
              {item.id.length > 12 ? `${item.id.slice(0, 12)}…` : item.id}
            </span>
          </p>
        </div>
        {error && (
          <p role="alert" className="mt-3 mb-0 break-words text-xs text-danger">
            {error}
          </p>
        )}
        <div className="mt-5 flex justify-end gap-3">
          <button
            ref={cancel}
            type="button"
            disabled={pending}
            onClick={onCancel}
            className={`${dialogButtonClass} border-line bg-panel text-ink hover:bg-hover`}
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={pending}
            onClick={onConfirm}
            className={`${dialogButtonClass} inline-flex items-center gap-2 border-acid bg-selected text-acid hover:bg-selected-hover`}
          >
            {pending && (
              <LoaderCircle
                aria-hidden="true"
                className="size-3 animate-spin motion-reduce:animate-none"
              />
            )}
            Set current
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

export function DeploymentList({
  deployments,
  expanded,
}: {
  deployments: WorkspaceDeployments;
  expanded: boolean;
}) {
  const { items, loading, hasMore, loadMore, error, selectedId, onSelect, setCurrent } =
    deployments;
  const scrollElement = useRef<HTMLDivElement>(null);
  const scrollHandle = useRef<HTMLDivElement>(null);
  // The host's loading update can arrive after several scroll events. Latch the
  // request synchronously until a page arrives or the host finishes its request.
  const requested = useRef(false);
  const previousPage = useRef({ count: items.length, loading });
  const [confirmation, setConfirmation] = useState<WorkspaceDeployment>();
  const [pending, setPending] = useState(false);
  const [confirmationError, setConfirmationError] = useState<string>();
  const confirming = useRef(false);
  const confirmationTrigger = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!confirmation) confirmationTrigger.current?.focus();
  }, [confirmation]);
  const rowHeight = expanded ? 34 : 27.5;
  const statusColumnClass = `deployment-list-status flex shrink-0 items-center mr-[30px] justify-end text-right ${expanded ? "w-[120px] text-[11px]" : "w-[100px] text-[10px]"} font-mono leading-none`;
  const virtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => scrollElement.current,
    estimateSize: () => rowHeight,
    getItemKey: (index) => items[index].id,
    overscan: 8,
  });
  useEffect(() => {
    virtualizer.measure();
  }, [rowHeight, virtualizer]);
  useTimelineScrollHandle(scrollElement, scrollHandle, expanded);
  useEffect(() => {
    // Reset only on host progress, not an effect replay (React StrictMode).
    if (
      previousPage.current.count !== items.length ||
      previousPage.current.loading !== loading
    ) {
      requested.current = false;
      previousPage.current = { count: items.length, loading };
    }
  }, [items.length, loading]);
  useEffect(() => {
    const viewport = scrollElement.current;
    if (!viewport || loading || !hasMore || !loadMore || error) return;
    const loadNearBottom = () => {
      if (
        requested.current ||
        items.length * rowHeight - viewport.scrollTop - viewport.clientHeight > rowHeight * 4
      )
        return;
      requested.current = true;
      loadMore();
    };
    viewport.addEventListener("scroll", loadNearBottom, { passive: true });
    loadNearBottom();
    return () => viewport.removeEventListener("scroll", loadNearBottom);
  }, [items.length, rowHeight, loading, hasMore, loadMore, error]);

  const confirm = async () => {
    if (!confirmation || !setCurrent || confirming.current) return;
    confirming.current = true;
    setPending(true);
    setConfirmationError(undefined);
    try {
      await setCurrent.onConfirm(confirmation.id);
      setConfirmation(undefined);
    } catch (cause) {
      setConfirmationError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      confirming.current = false;
      setPending(false);
    }
  };

  return (
    <>
      {confirmation && setCurrent && (
        <SetCurrentDialog
          item={confirmation}
          setCurrent={setCurrent}
          pending={pending}
          error={confirmationError}
          onCancel={() => {
            setConfirmation(undefined);
            setConfirmationError(undefined);
          }}
          onConfirm={() => {
            void confirm();
          }}
        />
      )}
      <div
        className={`flex shrink-0 items-center border-b border-run-list-line ${expanded ? "h-9" : "h-7"}`}
        aria-hidden="true"
      >
        <div
          className={`grid min-w-0 flex-1 ${expanded ? "grid-cols-[minmax(0,1fr)_120px]" : "grid-cols-[minmax(0,1fr)_100px]"} items-center gap-3 font-mono text-[9px] tracking-[0.16em] text-run-status-muted uppercase pl-[16px] pr-3`}
        >
          <span className="truncate normal-case">id</span>
          <span>Created</span>
        </div>
        <span className={`${statusColumnClass} text-run-status-muted uppercase`}>
          <span className="text-[9px] tracking-[0.16em]">Status</span>
        </span>
      </div>
      <div className={`timeline-scroll-shell relative min-h-0 ${expanded ? "flex-1" : ""}`}>
        <div
          ref={scrollElement}
          className={`deployment-list-scroll run-list-scroll timeline-scroll overflow-auto overscroll-contain ${expanded ? "h-full" : "max-h-48"}`}
        >
          <div
            className="deployment-list-virtual relative w-full"
            style={{ height: virtualizer.getTotalSize() }}
          >
            {virtualizer.getVirtualItems().map((row) => {
              const item = items[row.index];
              const createdAt = new Date(item.createdAt * 1000);
              return (
                <div
                  key={item.id}
                  className={`deployment-list-item absolute top-0 left-0 flex w-full cursor-pointer items-center shadow-[inset_0_-1px_var(--color-run-list-line)] ${selectedId === item.id ? "bg-selected" : "bg-transparent hover:bg-hover"}`}
                  style={{ height: row.size, transform: `translateY(${row.start}px)` }}
                  // The whole row selects; the inner button keeps the single tab stop and its
                  // Enter/Space clicks bubble here. "set current" stops propagation.
                  onClick={() => onSelect(item.id)}
                >
                  <button
                    type="button"
                    className={`deployment-list-row grid h-full min-w-0 flex-1 cursor-pointer ${expanded ? "grid-cols-[minmax(0,1fr)_120px]" : "grid-cols-[minmax(0,1fr)_100px]"} items-center gap-3 border-0 border-b border-run-list-line bg-transparent text-left font-mono leading-none text-ink focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-acid pl-[16px] pr-3 ${expanded ? "text-[11px]" : "text-[10px]"} ${selectedId === item.id ? "bg-selected shadow-[inset_2px_0_var(--color-acid)] hover:bg-selected-hover" : "hover:bg-hover"}`}
                    title={item.id}
                    aria-label={`${item.id}, ${item.label}, ${timestampFormat.format(createdAt)}${item.current ? ", current" : ""}`}
                    aria-pressed={selectedId === item.id}
                    aria-current={item.current ? "true" : undefined}
                  >
                    <span className="min-w-0 truncate">{item.label}</span>
                    <time
                      className="truncate text-secondary"
                      dateTime={createdAt.toISOString()}
                      title={timestampFormat.format(createdAt)}
                    >
                      {timestampFormat.format(createdAt)}
                    </time>
                  </button>
                  <div className={statusColumnClass}>
                    {item.current ? (
                      <span className="deployment-current-marker inline-flex items-center gap-1 whitespace-nowrap text-secondary">
                        current
                        <span aria-hidden="true" className="text-run-status-success">
                          ●
                        </span>
                      </span>
                    ) : setCurrent ? (
                      <button
                        type="button"
                        aria-label={(setCurrent.label ?? "set current").toLocaleLowerCase()}
                        aria-busy={confirmation?.id === item.id}
                        disabled={!!confirmation || pending}
                        className="deployment-set-current group cursor-pointer whitespace-nowrap border-0 bg-transparent p-0 font-mono text-[length:inherit] leading-none font-normal text-secondary hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-acid disabled:cursor-wait disabled:opacity-50"
                        onClick={(event) => {
                          event.stopPropagation();
                          confirmationTrigger.current = event.currentTarget;
                          setConfirmation(item);
                          setConfirmationError(undefined);
                        }}
                      >
                        <span className="deployment-set-current-label font-normal underline underline-offset-2 group-hover:no-underline">
                          {(setCurrent.label ?? "set current").toLocaleLowerCase()}
                        </span>
                        {confirmation?.id === item.id && (
                          <LoaderCircle
                            aria-hidden="true"
                            className="ml-1 inline size-3 animate-spin align-[-2px] motion-reduce:animate-none"
                          />
                        )}
                      </button>
                    ) : null}
                  </div>
                </div>
              );
            })}
          </div>
          {loading && (
            <div role="status" className="px-[16px] py-1 font-mono text-[10px] text-secondary">
              Loading deployments…
            </div>
          )}
          {error && (
            <div role="alert" className="p-2.5 font-mono text-[10px] text-danger">
              {error}
            </div>
          )}
          {!items.length && !loading && !error && (
            <div className="p-2.5 font-mono text-[10px] text-secondary">No deployments</div>
          )}
        </div>
        <div ref={scrollHandle} className="timeline-scroll-handle" aria-hidden="true" />
      </div>
    </>
  );
}
