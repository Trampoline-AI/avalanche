"""Sample workflows for testing the operator's workflow discovery."""

import logging

import avalanche as ava
from avalanche import Logger, dest, source, step, workflow

# Third-party-style logger (not avalanche.node.*)
vendor_log = logging.getLogger("vendor.db")


class InvocationInput(ava.BaseInput):
    message: str = "default"
    document: ava.File | None = None
    document_ref: ava.S3File | None = None


class InvocationContext(ava.RunContext):
    request_id: str = "none"


@source
def capture_invocation(payload: InvocationInput, ctx: InvocationContext, log=Logger()):
    file_text = payload.document.read_bytes().decode() if payload.document else ""
    ref_uri = payload.document_ref.uri if payload.document_ref else ""
    log.info(
        "Invocation input: "
        f"message={payload.message}; request_id={ctx.request_id}; "
        f"execution_id={ctx.execution_id}; node={ctx.node_id}; file={file_text}; s3={ref_uri}"
    )
    return payload.message


@workflow(input=InvocationInput, context=InvocationContext)
def input_workflow():
    capture_invocation()


@source
def fetch_data(log=Logger()):
    log.info("Connecting to source database...")
    log.debug("Connection pool: acquiring slot 1/5")
    vendor_log.info("SELECT count(*) FROM orders")
    log.info("Found 3 pending items")
    vendor_log.info("SELECT * FROM orders WHERE status='pending' LIMIT 3")
    log.debug("Row 1: id=101, amount=42.50, sku=WIDGET-A, region=us-east")
    log.debug("Row 2: id=102, amount=18.00, sku=GADGET-B, region=eu-west")
    log.debug("Row 3: id=103, amount=95.20, sku=WIDGET-A, region=us-east")
    log.debug("Computing row checksums...")
    log.debug("Checksum: sha256=a3f2...c891 (3 rows, 155.70 total)")
    log.info("Fetch complete: 3 rows, 155.70 total")
    log.debug("Connection pool: releasing slot 1/5")
    return {"items": [1, 2, 3]}


@step
def process_data(data, log=Logger()):
    n = len(data.get("items", []))
    log.info(f"Processing {n} items...")
    log.info("Applying schema validation")
    log.debug("Column 'amount': float64, nullable=false — OK")
    log.debug("Column 'sku': string, max_len=32 — OK")
    log.debug("Column 'region': string, enum=['us-east','eu-west','ap-south'] — OK")
    log.debug("Schema check passed for all rows")
    log.info("Checking for null values...")
    log.debug("Null scan: 0 nulls found in 3 rows × 4 columns")
    log.info("Deduplicating by order_id")
    log.debug("Dedup: 3 input rows → 3 unique (0 duplicates removed)")
    log.info(f"Transforming {n} rows: multiply values by 2")
    log.debug("Row 1: 42.50 → 85.00")
    log.debug("Row 2: 18.00 → 36.00")
    log.debug("Row 3: 95.20 → 190.40")
    log.info("Type casting: ensuring int output")
    log.info("Processing complete: 3 rows output")
    return {k: [x * 2 for x in v] if isinstance(v, list) else v for k, v in data.items()}


@dest
def save_data(data, log=Logger()):
    log.info("Preparing batch insert...")
    log.debug("Batch size: 3 rows, estimated 0.4KB")
    vendor_log.info("BEGIN TRANSACTION")
    vendor_log.info("INSERT INTO warehouse VALUES (85.00, 'WIDGET-A', 'us-east')")
    vendor_log.info("INSERT INTO warehouse VALUES (36.00, 'GADGET-B', 'eu-west')")
    vendor_log.info("INSERT INTO warehouse VALUES (190.40, 'WIDGET-A', 'us-east')")
    vendor_log.info("COMMIT")
    log.info("Saved 3 rows to warehouse")
    log.info("Verifying row count...")
    vendor_log.info("SELECT count(*) FROM warehouse WHERE batch_id='current'")
    log.debug("Row count verified: 3 rows committed")
    log.info("Rebuilding indexes on warehouse.order_id...")
    log.debug("Index rebuild complete: 0.02s")
    vendor_log.warning("Table warehouse approaching storage threshold (82%)")
    log.info("Batch insert complete, cleaning up temp files")
    return data


@workflow(cron="*/5 * * * *")
def simple_workflow():
    fetch_data() >> process_data() >> save_data()


@source
def slow_source(log=Logger()):
    import time
    log.info("Initializing source connection...")
    log.debug("Connection pool: max=5, idle=2, timeout=30s")
    log.debug("SSL handshake: TLS 1.3, cipher=AES-256-GCM")
    time.sleep(0.5)
    for i in range(3):
        log.info(f"Fetching batch {i+1}/3...")
        log.debug(f"Batch {i+1}: offset={i*1000}, limit=1000")
        log.debug(f"Batch {i+1}: query plan — seq scan on source_table")
        time.sleep(0.5)
        log.debug(f"Batch {i+1}: received 1000 rows, 245KB transferred")
        log.debug(f"Batch {i+1}: checksum=0x{(i+1)*1111:04x}")
        log.info(f"Batch {i+1} complete: 1000 rows")
        log.debug(f"Memory usage: {45 + i*12}MB heap, {12 + i*3}MB buffers")
        time.sleep(0.5)
    log.debug("Connection keepalive: ping OK (2ms)")
    log.info("Source extraction finished: 3000 total rows")
    log.debug("Total transfer: 735KB in 3 batches")
    return "slow_data"


@step
def slow_transform(data, log=Logger()):
    import time
    log.info("Starting transformation workflow...")
    log.debug("Input data: 3000 rows, estimated 735KB")
    for i in range(5):
        step_names = ["parsing", "validating", "normalizing", "enriching", "indexing"]
        log.info(f"Step {i+1}/5: {step_names[i]}...")
        log.debug(f"Step {i+1} input: 3000 rows")
        time.sleep(0.3)
        log.debug(f"Step {i+1}: allocated temp buffer (12MB)")
        time.sleep(0.2)
        log.debug(f"Step {i+1}: processed 3000 rows in 0.5s (6000 rows/s)")
        log.info(f"Step {i+1}/5 complete: 3000 rows output")
        log.debug(f"Step {i+1}: freed temp buffer")
        log.debug(f"Memory after step {i+1}: {50 + i*8}MB heap")
        time.sleep(0.5)
        if i < 4:
            log.debug(f"Inter-step validation: row counts match ({3000} == {3000})")
    log.info("All step steps complete: 3000 rows output")
    log.debug("Total step time: ~5.0s, peak memory: 82MB")
    return f"{data}_processed"


@workflow(cron="* * * * *")
def slow_workflow():
    slow_source() >> slow_transform()


# ── Complex workflow: order processing (no schedule, manual only) ────

@source
def fetch_orders(log=Logger()):
    import time
    log.info("Connecting to orders database...")
    log.debug("Using connection pool: orders-db-prod (us-east-1)")
    vendor_log.info("SELECT count(*) FROM orders WHERE status='new'")
    log.info("Found 2 new orders")
    log.debug("Pagination: page 1/1, page_size=100")
    time.sleep(0.1)
    vendor_log.info("SELECT * FROM orders WHERE status='new' ORDER BY created_at")
    log.debug(
        "Order #1: id=1, amount=100.00, sku=WIDGET-A, "
        "customer=acme-corp, created=2026-04-01T10:00:00Z"
    )
    log.debug("Order #1: shipping=standard, weight=2.3kg, warehouse=us-east-dc1")
    log.debug(
        "Order #2: id=2, amount=250.00, sku=GADGET-B, "
        "customer=globex, created=2026-04-01T10:05:00Z"
    )
    log.debug("Order #2: shipping=express, weight=0.8kg, warehouse=eu-west-dc2")
    log.debug("Rate limit: 48/50 requests remaining (resets in 12s)")
    log.debug("Cache miss: orders query not in cache, TTL=60s")
    time.sleep(0.1)
    log.info("Orders fetch complete: 2 orders, 350.00 total")
    log.debug("Query execution time: 23ms")
    return {"orders": [{"id": 1, "amount": 100}, {"id": 2, "amount": 250}]}


@source
def fetch_inventory(log=Logger()):
    import time
    log.info("Connecting to inventory service...")
    log.debug("Service endpoint: https://inventory.internal:8443/api/v2")
    time.sleep(0.1)
    vendor_log.info("GET /api/v2/inventory?skus=WIDGET-A,GADGET-B")
    log.debug("WIDGET-A: 50 units available (warehouse: us-east-dc1)")
    log.debug("WIDGET-A: reorder point=20, lead time=5 days")
    log.debug("GADGET-B: 12 units available (warehouse: eu-west-dc2)")
    log.debug("GADGET-B: reorder point=25, lead time=3 days")
    log.warning("Low stock alert: GADGET-B at 12 units (below reorder point of 25)")
    log.info("Inventory snapshot loaded: 2 SKUs, 62 total units")
    log.debug("Response time: 45ms, payload: 1.2KB")
    time.sleep(0.1)
    return {"inventory": {"sku_a": 50, "sku_b": 12}}


@step
def validate(orders, inventory, log=Logger()):
    n = len(orders.get("orders", []))
    log.info(f"Validating {n} orders against inventory...")
    log.debug("Validation rules: stock_check, schema, business_rules, fraud_screen")
    log.debug("Order #1 (WIDGET-A): stock=50, requested=1 — PASS")
    log.debug("Order #1: schema validation — PASS")
    log.debug("Order #1: business rules (amount < $10000) — PASS")
    log.debug("Order #1: fraud score=0.02 (threshold=0.8) — PASS")
    log.debug("Order #2 (GADGET-B): stock=12, requested=1 — PASS")
    log.debug("Order #2: schema validation — PASS")
    log.debug("Order #2: business rules (amount < $10000) — PASS")
    log.debug("Order #2: fraud score=0.05 (threshold=0.8) — PASS")
    log.info("All orders pass inventory check")
    log.warning("Note: GADGET-B stock will be at 11 after this order (below reorder point)")
    log.info(f"Validation complete: {n}/{n} orders valid, 0 rejected")
    log.debug("Validation time: 8ms")
    return {"validated": orders.get("orders", []), "stock": inventory}


@step
def enrich(data, log=Logger()):
    log.info("Enriching orders with customer profiles...")
    log.debug("Customer service: https://crm.internal:8443/api/v1/customers")
    for order in data.get("validated", []):
        cid = f"cust_{order['id']}"
        order["customer"] = cid
        log.debug(f"Order #{order['id']}: looking up customer {cid}")
        log.debug(f"Order #{order['id']}: cache HIT for {cid} (TTL remaining: 245s)")
        log.debug(f"Order #{order['id']}: profile completeness=92%, tier=gold")
        log.debug(f"Order #{order['id']}: enriched with customer={cid}, tier=gold")
    log.info(f"Enrichment complete: {len(data.get('validated', []))} orders")
    log.debug("Avg lookup latency: 3ms (100% cache hits)")
    return data


@step
def aggregate(data, log=Logger()):
    log.info("Computing order aggregates...")
    orders = data.get("validated", [])
    total = sum(o.get("amount", 0) for o in orders)
    avg = total / len(orders) if orders else 0
    log.info(f"Total revenue: ${total:.2f}")
    log.info(f"Average order: ${avg:.2f}")
    log.info(f"Order count: {len(orders)}")
    log.debug(f"Min order: ${min(o['amount'] for o in orders):.2f}")
    log.debug(f"Max order: ${max(o['amount'] for o in orders):.2f}")
    log.debug(f"Median order: ${sorted(o['amount'] for o in orders)[len(orders)//2]:.2f}")
    log.debug("Revenue buckets: $0-100=1, $100-500=1, $500+=0")
    log.debug("P50=$175.00, P90=$250.00, P99=$250.00")
    log.debug("Compared to previous day: +15.2% revenue, -5% order count")
    log.info("Aggregation complete")
    log.debug("Compute time: 2ms")
    return {"total": total, "count": len(orders)}


@dest
def save_warehouse(data, log=Logger()):
    log.info("Writing aggregates to warehouse...")
    log.debug("Target table: daily_summary, partition: 2026-04-02")
    log.debug("Partition exists: yes, current size: 12.4MB")
    vendor_log.info("BEGIN TRANSACTION")
    vendor_log.info(
        "INSERT INTO daily_summary (date, total, count) "
        f"VALUES ('2026-04-02', {data.get('total')}, {data.get('count')})"
    )
    vendor_log.info(
        "UPDATE daily_summary_latest SET total=350.00, count=2 "
        "WHERE date='2026-04-02'"
    )
    vendor_log.info("COMMIT")
    log.info("Warehouse write complete: 2 rows affected")
    log.debug("Checking compaction threshold...")
    log.debug("Partition 2026-04-02: 12.4MB (threshold: 100MB) — no compaction needed")
    log.debug("Backup verification: last backup 2h ago, RPO=4h — OK")
    log.info("Post-write checks passed")
    return data


@dest
def notify(data, log=Logger()):
    log.info("Preparing notification...")
    log.info(f"Summary: {data.get('count')} orders, ${data.get('total'):.2f} revenue")
    log.debug("Formatting message for Slack markdown...")
    log.debug("Message length: 234 chars (limit: 4000)")
    log.info("Sending to #orders-alerts channel")
    log.debug("Webhook: POST https://hooks.slack.com/services/T0123/B0456/xxxx")
    log.debug("HTTP 200 OK, response_time=120ms")
    log.info("Primary notification sent successfully")
    log.info("Sending backup to #orders-daily-digest...")
    log.debug("Webhook: POST https://hooks.slack.com/services/T0123/B0789/yyyy")
    log.debug("HTTP 200 OK, response_time=95ms")
    log.info("Backup notification sent")
    log.debug("Delivery receipts: 2/2 channels confirmed")
    return data


@workflow
def order_workflow():
    """Complex workflow: parallel sources, fan-in, sequential steps, fan-out."""
    orders = fetch_orders()
    inventory = fetch_inventory()
    validated = validate(orders, inventory)
    enriched = enrich(validated)
    agg = aggregate(enriched)
    (save_warehouse(agg) & notify(agg))
