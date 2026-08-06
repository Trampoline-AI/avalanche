"""Quick demo: run a real workflow through the Operator and watch node transitions."""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from runtime.operator import Operator
from runtime.operator.models import RunStatus

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def main():
    op = Operator(workflow_paths=[FIXTURES])

    print("Discovered workflows:")
    for p in op.list_workflows():
        print(f"  {p.name}  ({len(p.node_ids)} nodes: {' → '.join(p.node_ids)})")
    print()

    def on_update(run):
        nodes = "  ".join(
            f"{ns.name}={ns.status.value}" for ns in run.nodes.values()
        )
        print(f"  [{run.status.value:>10}] {nodes}")

    op.on_run_update(on_update)

    print("Starting simple_workflow...")
    run_id = op.start_run("simple_workflow")

    while True:
        run = op.get_run(run_id)
        if run and run.status in (RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.CANCELLED):
            break
        time.sleep(0.05)

    print(f"\nFinal: {run.status.value} in {run.elapsed:.2f}s")
    if run.logs:
        print(f"\nLogs ({len(run.logs)}):")
        for le in run.logs:
            print(f"  [{le.level.value:>5}] {le.node_id}: {le.message}")


if __name__ == "__main__":
    main()
