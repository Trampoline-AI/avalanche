"""Demo: start a slow workflow and cancel it mid-run."""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from avalanche.operator import Operator

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def main():
    op = Operator(workflow_paths=[FIXTURES])

    def on_update(run):
        nodes = "  ".join(
            f"{ns.name}={ns.status.value}" for ns in run.nodes.values()
        )
        print(f"  [{run.status.value:>10}] {nodes}")

    op.on_run_update(on_update)

    print("Starting slow_workflow (will cancel after 0.3s)...")
    run_id = op.start_run("slow_workflow")

    time.sleep(0.3)
    print("\n  >>> Cancelling! <<<\n")
    op.cancel_run(run_id)

    time.sleep(0.5)
    run = op.get_run(run_id)
    print(f"\nFinal: {run.status.value}")
    for ns in run.nodes.values():
        print(f"  {ns.name}: {ns.status.value}")


if __name__ == "__main__":
    main()
