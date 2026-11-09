import { useState } from "react";
import type { OperatorApi } from "./api";
import type { MachineVisit } from "./machine";
import { GraphCanvas } from "./GraphCanvas";
import { Inspector } from "./Inspector";
import { RunLogPane } from "./RunLogPane";
import { RunWaitPanel } from "./RunWaitPanel";
import { useRunEvaluations } from "./useRunEvaluations";

/**
 * One visit's step graph, inspector, logs and waits. The host supplies the visit's
 * own run snapshot (qualified nodes, retained I/O, trace and log tokens), so the
 * shared components show real execution detail; nothing is synthesized here.
 */
export function MachineVisitWorkspace({
  api,
  runId,
  visit,
  enabled,
  current,
}: {
  api: OperatorApi;
  runId: string;
  visit: MachineVisit;
  enabled: boolean;
  current: boolean;
}) {
  const [selected, setSelected] = useState<string>();
  const run = visit.snapshot;
  const evaluations = useRunEvaluations(api, run.operatorInstanceId, runId);
  return (
    <section aria-label={`Visit ${visit.visit} step workspace`} className="grid gap-3">
      <div className="run-canvas grid h-[420px] min-w-0 grid-cols-1 overflow-hidden rounded border border-line bg-run-canvas md:grid-cols-[minmax(0,1fr)_360px]">
        <div className="relative min-h-0 min-w-0">
          <GraphCanvas
            runTopology={run.topology}
            runNodes={run.nodes}
            expansions={run.expansions}
            runTriggeredBy={run.summary?.triggeredBy}
            evaluations={evaluations.records}
            onOpenNode={setSelected}
            selectedNodeId={selected}
            onClearNode={() => setSelected(undefined)}
          />
        </div>
        {selected ? (
          <Inspector
            api={api}
            embedded
            run={run}
            nodeId={selected}
            evaluationState={evaluations}
            onClose={() => setSelected(undefined)}
          />
        ) : (
          <p className="m-0 border-l border-line bg-panel p-4 text-xs text-secondary">
            Select a step to inspect its outputs, attempts and trace.
          </p>
        )}
      </div>
      <div className="flex min-h-0 flex-col overflow-hidden rounded border border-line">
        <RunLogPane api={api} run={run} nodeId={selected} onSelectNode={setSelected} />
      </div>
      {api.listRunWaits ? (
        <RunWaitPanel
          api={api}
          runId={runId}
          visit={visit.visit}
          readOnly={!enabled || !current}
          live={current}
        />
      ) : (
        <p className="text-xs text-secondary">
          Visit waits and decision audits are unavailable from this host.
        </p>
      )}
    </section>
  );
}
