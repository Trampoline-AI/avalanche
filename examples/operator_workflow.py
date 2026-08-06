"""Workflow file for trying the local operator and connected TUI.

Run directly:
    uv run python examples/operator_workflow.py

Run through the operator:
    uv run ava operator --flows examples/operator_workflow.py
"""

from __future__ import annotations

import avalanche as ava


@ava.source
def fetch_orders(log=ava.Logger()) -> dict[str, object]:
    log.info("Fetched two local demo orders")
    return {
        "orders": [
            {"order_id": "A100", "amount": 125.0, "region": "west"},
            {"order_id": "B200", "amount": 240.0, "region": "east"},
        ]
    }


@ava.step
def validate_orders(
    order_batch: dict[str, object],
    log=ava.Logger(),
) -> dict[str, object]:
    orders = order_batch["orders"]
    valid = [order for order in orders if float(order["amount"]) > 0]
    log.info(f"Validated {len(valid)} orders")
    return {"orders": valid}


@ava.step
def summarize_orders(
    order_batch: dict[str, object],
    log=ava.Logger(),
) -> dict[str, object]:
    orders = order_batch["orders"]
    total = sum(float(order["amount"]) for order in orders)
    log.info(f"Computed local order total: {total}")
    return {"count": len(orders), "total": total}


@ava.dest
def publish_summary(summary: dict[str, object], log=ava.Logger()) -> dict[str, object]:
    log.info(f"Published summary for {summary['count']} orders")
    return summary


@ava.workflow
def operator_demo_workflow():
    # Equivalent NodeFuture argument form, less visual:
    # orders = fetch_orders()
    # valid_orders = validate_orders(orders)
    # summary = summarize_orders(valid_orders)
    # published = publish_summary(summary)
    return fetch_orders() >> validate_orders() >> summarize_orders() >> publish_summary()


def _main() -> None:
    result = operator_demo_workflow().run(executor=ava.LocalExecutor()).result()
    print("Operator workflow example complete")
    print(result)


if __name__ == "__main__":
    _main()
