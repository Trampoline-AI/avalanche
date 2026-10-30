"""Workflow file for trying the local operator and connected TUI.

Run directly:
    uv run python examples/operator_workflow.py

Run through the operator:
    uv run ava operator examples/operator_workflow.py

The workflow runs every minute once its schedule is turned on. Schedules start
off; turn this one on from the Web UI's run controls, the TUI (Ctrl+S) or
`ava schedule enable operator_demo_workflow --connect HOST:PORT --input JSON`.
`recipients` has no default, so give the schedule an input that sets it.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import Field

import avalanche as ava

DEMO_ORDERS = [
    {"order_id": "A100", "amount": 125.0, "region": "west", "day": "2026-10-01"},
    {"order_id": "B200", "amount": 240.0, "region": "east", "day": "2026-10-03"},
    {"order_id": "C300", "amount": 18.5, "region": "north", "day": "2026-10-05"},
    {"order_id": "D400", "amount": -40.0, "region": "west", "day": "2026-10-06"},
    {"order_id": "E500", "amount": 310.0, "region": "east", "day": "2026-10-08"},
]


class OrderReportInput(ava.BaseInput):
    """Which orders to report on and who receives the report."""

    recipients: list[str] = Field(description="Email addresses that receive the summary.")
    regions: list[str] = Field(
        default=["west", "east"], description="Regions whose orders are included."
    )
    currency: Literal["USD", "EUR", "CAD"] = Field(
        default="USD", description="Currency the report totals are labelled in."
    )
    min_amount: float = Field(default=0.0, ge=0, description="Skip orders below this amount.")
    max_orders: int = Field(default=50, ge=1, le=500, description="Upper bound on orders read.")
    include_refunds: bool = Field(
        default=False, description="Keep negative amounts (refunds) in the summary."
    )
    since: date | None = Field(
        default=None, description="Only orders on or after this day; all days when empty."
    )
    report_label: str = Field(
        default_factory=lambda: f"orders-{datetime.now():%Y%m%d-%H%M}",
        description="Name of this report; defaults to the run's start time.",
    )


@ava.source
def fetch_orders(config: OrderReportInput, log=ava.Logger()) -> dict[str, object]:
    """Load the local demo orders for the selected regions and days."""
    orders = [
        order
        for order in DEMO_ORDERS
        if order["region"] in config.regions
        and (config.since is None or date.fromisoformat(order["day"]) >= config.since)
    ][: config.max_orders]
    log.info(f"Fetched {len(orders)} demo orders from {', '.join(config.regions)}")
    return {"orders": orders}


@ava.step
def validate_orders(
    order_batch: dict[str, object],
    config: OrderReportInput,
    log=ava.Logger(),
) -> dict[str, object]:
    """Keep orders above the minimum amount, and refunds only when asked."""
    orders = order_batch["orders"]
    valid = [
        order
        for order in orders
        if float(order["amount"]) >= config.min_amount
        or (config.include_refunds and float(order["amount"]) < 0)
    ]
    log.info(f"Validated {len(valid)} orders")
    return {"orders": valid}


@ava.step
def summarize_orders(
    order_batch: dict[str, object],
    config: OrderReportInput,
    log=ava.Logger(),
) -> dict[str, object]:
    """Compute the order count and total amount."""
    orders = order_batch["orders"]
    total = sum(float(order["amount"]) for order in orders)
    log.info(f"Computed order total: {total} {config.currency}")
    return {
        "label": config.report_label,
        "count": len(orders),
        "total": total,
        "currency": config.currency,
    }


@ava.dest
def publish_summary(
    summary: dict[str, object], recipients: list[str], log=ava.Logger()
) -> dict[str, object]:
    """Publish the order summary to the recipients."""
    log.info(f"Published {summary['label']} for {summary['count']} orders to {recipients}")
    return {**summary, "recipients": recipients}


@ava.workflow(input=OrderReportInput, cron="* * * * *")
def operator_demo_workflow():
    # Each step receives the whole run input through its OrderReportInput
    # annotation; the ava.input selector passes just one field to the last step.
    orders = fetch_orders()
    valid_orders = validate_orders(orders)
    summary = summarize_orders(valid_orders)
    return publish_summary(summary, recipients=ava.input.recipients)


def _main() -> None:
    result = (
        operator_demo_workflow()
        .run(executor=ava.LocalExecutor(), input={"recipients": ["ops@example.com"]})
        .result()
    )
    print("Operator workflow example complete")
    print(result)


if __name__ == "__main__":
    _main()
