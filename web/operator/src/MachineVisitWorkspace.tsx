import { useMemo, useState } from "react";
import type { OperatorApi } from "./api";
import type { MachineVisit } from "./machine";
import { GraphCanvas } from "./GraphCanvas";
import { Inspector } from "./Inspector";
import { NodeSnapshotMsg, RunSnapshotMsg, RunSummaryMsg, WorkflowTopologyMsg } from "./model";
import { useRunEvaluations } from "./useRunEvaluations";

/** Visit-qualified identities and execution kinds stay intact in the shared graph/inspector. */
export function MachineVisitWorkspace({
  api,
  runId,
  visit,
}: {
  api: OperatorApi;
  runId: string;
  visit: MachineVisit;
}) {
  const [selected, setSelected] = useState<string>();
  const run = useMemo(() => {
    const entries = Object.entries(visit.nodes);
    const graph: WorkflowTopologyMsg["graph"] = {};
    for (const [id, node] of entries)
      for (const parent of node.deps) {
        (graph[parent] ??= { children: [] }).children.push(id);
      }
    return RunSnapshotMsg.create({
      // Visit retention is not a run outcome. Only per-node execution status is known here.
      summary: RunSummaryMsg.create({ runId }),
      topology: WorkflowTopologyMsg.create({
        nodeIds: entries.map(([id]) => id),
        graph,
        nodeTypes: Object.fromEntries(entries.map(([id, node]) => [id, node.kind])),
        displayNames: Object.fromEntries(entries.map(([id]) => [id, id])),
      }),
      nodes: entries.map(([id, node]) =>
        NodeSnapshotMsg.create({
          nodeId: id,
          name: id,
          nodeType: node.kind,
          status: node.state,
        }),
      ),
    });
  }, [runId, visit]);
  const evaluations = useRunEvaluations(api, "", runId);
  return (
    <section aria-label={`Visit ${visit.visit} step workspace`} className="grid gap-3">
      <div className="grid h-[420px] min-w-0 grid-cols-1 overflow-hidden rounded border border-line md:grid-cols-[minmax(0,1fr)_320px]">
        <GraphCanvas
          runTopology={run.topology}
          runNodes={run.nodes}
          onOpenNode={setSelected}
          selectedNodeId={selected}
          onClearNode={() => setSelected(undefined)}
        />
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
          <div className="p-4 text-xs text-secondary">
            Select a step to inspect its execution.
          </div>
        )}
      </div>
      <div className="overflow-auto">
        <table className="w-full text-left text-xs text-secondary">
          <thead>
            <tr>
              <th>Step</th>
              <th>Kind</th>
              <th>Status</th>
              <th>Generation</th>
              <th>Attempts</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(visit.nodes).map(([id, node]) => (
              <tr key={id}>
                <td className="py-2">
                  <button onClick={() => setSelected(id)} className="text-ink underline">
                    {id}
                  </button>
                </td>
                <td>{node.kind}</td>
                <td>{node.state}</td>
                <td>{node.g}</td>
                <td>{node.attempts}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
