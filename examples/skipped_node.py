"""Intentional absence satisfies dependencies without failing a workflow.

Run directly:
    uv run python examples/skipped_node.py

Explore the graph and skip detail in the browser:
    uv run ava dev examples/skipped_node.py
"""

from __future__ import annotations

import avalanche as ava


@ava.source
def optional_records() -> ava.Skipped:
    """There are no eligible records in this local demo partition."""
    return ava.skip(
        "No eligible records for this partition", {"partition": "today", "count": 0},
    )


@ava.step
def publish_summary(records: ava.Skipped, log=ava.Logger()) -> str:
    """A skipped value is explicit, and its dependency is still satisfied."""
    message = f"Downstream completed: {records.reason}"
    log.info(message)
    return message


@ava.workflow
def skipped_node_example():
    return optional_records() >> publish_summary()


def _main() -> None:
    result = skipped_node_example().run(executor=ava.LocalExecutor()).result()
    print(result)
    print("Skipped-node example completed successfully")


if __name__ == "__main__":
    _main()
