import { useVirtualizer } from "@tanstack/react-virtual";
import { useEffect, useRef, useState } from "react";
import { LoaderCircle } from "lucide-react";

import type { WorkspaceDeployment, WorkspaceDeployments } from "./WorkflowWorkspace";
import { useTimelineScrollHandle } from "./useTimelineScrollHandle";

const timestampFormat = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});
const actionClass =
  "shrink-0 cursor-pointer rounded border-0 bg-transparent font-mono text-[10px] text-secondary hover:bg-canvas hover:text-ink focus-visible:outline-2 focus-visible:outline-acid disabled:cursor-wait disabled:opacity-50";

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
  const rowHeight = expanded ? 34 : 27.5;
  const statusColumnClass = `deployment-list-status flex shrink-0 items-center justify-end text-right ${expanded ? "mr-[20px] w-[120px] text-[11px]" : "mr-[17px] w-[100px] text-[10px]"} font-mono leading-none`;
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
        <div
          className="shrink-0 border-b border-run-list-line bg-panel p-3 font-mono text-[10px] text-ink"
          role="group"
          aria-label="Confirm current deployment"
        >
          <p className="m-0 mb-2 break-words">{setCurrent.confirmText(confirmation)}</p>
          {confirmationError && (
            <p role="alert" className="mb-2 text-danger">
              {confirmationError}
            </p>
          )}
          <div className="flex items-center gap-3">
            <button
              type="button"
              className={actionClass}
              disabled={pending}
              onClick={(event) => {
                event.stopPropagation();
                void confirm();
              }}
            >
              {pending ? "Setting current…" : "Confirm"}
            </button>
            <button
              type="button"
              className={actionClass}
              disabled={pending}
              onClick={(event) => {
                event.stopPropagation();
                setConfirmation(undefined);
                setConfirmationError(undefined);
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}
      <div
        className={`flex shrink-0 items-center border-b border-run-list-line ${expanded ? "h-9" : "h-7"}`}
        aria-hidden="true"
      >
        <div
          className={`grid min-w-0 flex-1 ${expanded ? "grid-cols-[minmax(0,1fr)_120px]" : "grid-cols-[minmax(0,1fr)_100px]"} items-center gap-3 font-mono text-[9px] tracking-[0.16em] text-run-status-muted uppercase ${expanded ? "px-[20px]" : "px-[9px]"}`}
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
                  className={`absolute top-0 left-0 flex w-full items-center shadow-[inset_0_-1px_var(--color-run-list-line)] ${selectedId === item.id ? "bg-selected" : "bg-transparent hover:bg-hover"}`}
                  style={{ height: row.size, transform: `translateY(${row.start}px)` }}
                >
                  <button
                    type="button"
                    className={`deployment-list-row grid h-full min-w-0 flex-1 cursor-pointer ${expanded ? "grid-cols-[minmax(0,1fr)_120px]" : "grid-cols-[minmax(0,1fr)_100px]"} items-center gap-3 border-0 border-b border-run-list-line bg-transparent text-left font-mono leading-none text-ink focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-acid ${expanded ? "px-[20px] text-[11px]" : "px-[9px] text-[10px]"} ${selectedId === item.id ? "bg-selected shadow-[inset_2px_0_var(--color-acid)] hover:bg-selected-hover" : "hover:bg-hover"}`}
                    title={item.id}
                    aria-label={`${item.id}, ${item.label}, ${timestampFormat.format(createdAt)}${item.current ? ", current" : ""}`}
                    aria-pressed={selectedId === item.id}
                    aria-current={item.current ? "true" : undefined}
                    onClick={() => onSelect(item.id)}
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
            <div role="status" className="px-2.5 py-1 font-mono text-[10px] text-secondary">
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
