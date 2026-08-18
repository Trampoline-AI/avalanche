"""MockStateProvider — timer-driven simulation for TUI development."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from typing import Callable
from uuid import uuid4

from .models import (
    CatalogSnapshot,
    DetailUpdate,
    LogDetailAppended,
    LogEntry,
    LogLevel,
    NodeState,
    NodeStatus,
    ResetBaseline,
    RunState,
    RunStatus,
    StreamResetNotice,
    TraceDetail,
    WorkflowInfo,
    display_name_from_id,
)

# ── Hardcoded workflow definitions ────────────────────────────────────────

ORDER_WORKFLOW = WorkflowInfo(
    name="order_workflow",
    file_path="workflows/etl/order_workflow.py",
    node_ids=[
        "fetch_orders_1",
        "fetch_inventory_1",
        "validate_1",
        "enrich_1",
        "aggregate_1",
        "save_warehouse_1",
        "notify_1",
    ],
    graph={
        "fetch_orders_1": ["validate_1"],
        "fetch_inventory_1": ["validate_1"],
        "validate_1": ["enrich_1"],
        "enrich_1": ["aggregate_1"],
        "aggregate_1": ["save_warehouse_1", "notify_1"],
    },
    node_types={
        "fetch_orders_1": "source",
        "fetch_inventory_1": "source",
        "validate_1": "step",
        "enrich_1": "step",
        "aggregate_1": "step",
        "save_warehouse_1": "dest",
        "notify_1": "dest",
    },
)

INGEST_WORKFLOW = WorkflowInfo(
    name="ingest_workflow",
    file_path="workflows/ingestion/ingest_workflow.py",
    node_ids=["extract_1", "parse_1", "deduplicate_1", "load_1"],
    graph={
        "extract_1": ["parse_1"],
        "parse_1": ["deduplicate_1"],
        "deduplicate_1": ["load_1"],
    },
    node_types={
        "extract_1": "source",
        "parse_1": "step",
        "deduplicate_1": "step",
        "load_1": "dest",
    },
)

ANALYTICS_WORKFLOW = WorkflowInfo(
    name="analytics_workflow",
    file_path="workflows/etl/analytics_workflow.py",
    node_ids=[
        "load_events_1",
        "load_users_1",
        "join_data_1",
        "compute_metrics_1",
        "export_dashboard_1",
        "export_alerts_1",
    ],
    graph={
        "load_events_1": ["join_data_1"],
        "load_users_1": ["join_data_1"],
        "join_data_1": ["compute_metrics_1"],
        "compute_metrics_1": ["export_dashboard_1", "export_alerts_1"],
    },
    node_types={
        "load_events_1": "source",
        "load_users_1": "source",
        "join_data_1": "step",
        "compute_metrics_1": "step",
        "export_dashboard_1": "dest",
        "export_alerts_1": "dest",
    },
)

ML_WORKFLOW = WorkflowInfo(
    name="ml_workflow",
    file_path="workflows/ml/ml_workflow.py",
    node_ids=[
        "fetch_training_1",
        "fetch_validation_1",
        "fetch_features_1",
        "preprocess_1",
        "train_model_1",
        "evaluate_1",
        "export_onnx_1",
        "deploy_staging_1",
        "deploy_prod_1",
        "notify_slack_1",
    ],
    graph={
        "fetch_training_1": ["preprocess_1"],
        "fetch_validation_1": ["preprocess_1"],
        "fetch_features_1": ["preprocess_1"],
        "preprocess_1": ["train_model_1"],
        "train_model_1": ["evaluate_1"],
        "evaluate_1": ["export_onnx_1", "deploy_staging_1", "deploy_prod_1"],
        "deploy_staging_1": ["notify_slack_1"],
        "deploy_prod_1": ["notify_slack_1"],
    },
    node_types={
        "fetch_training_1": "source",
        "fetch_validation_1": "source",
        "fetch_features_1": "source",
        "preprocess_1": "step",
        "train_model_1": "step",
        "evaluate_1": "step",
        "export_onnx_1": "dest",
        "deploy_staging_1": "dest",
        "deploy_prod_1": "dest",
        "notify_slack_1": "dest",
    },
)

DATA_PLATFORM_WORKFLOW = WorkflowInfo(
    name="data_platform",
    file_path="workflows/etl/data_platform.py",
    node_ids=[
        # 3-way source fan-in
        "ingest_clicks_1",
        "ingest_txns_1",
        "ingest_crm_1",
        # merge → step chain
        "deduplicate_1",
        "normalize_1",
        "validate_1",
        # 2-way scoring fork
        "score_churn_1",
        "score_ltv_1",
        # merge → 4-way export fan-out
        "build_profile_1",
        "export_warehouse_1",
        "export_redis_1",
        "export_api_1",
        "send_alerts_1",
        # 2-way uneven fan-in
        "update_catalog_1",
        "notify_team_1",
    ],
    graph={
        "ingest_clicks_1": ["deduplicate_1"],
        "ingest_txns_1": ["deduplicate_1", "export_redis_1", "send_alerts_1"],
        "ingest_crm_1": ["deduplicate_1", "notify_team_1"],
        "deduplicate_1": ["normalize_1"],
        "normalize_1": ["validate_1"],
        "validate_1": ["score_churn_1", "score_ltv_1"],
        "score_churn_1": ["build_profile_1"],
        "score_ltv_1": ["build_profile_1"],
        "build_profile_1": [
            "export_warehouse_1",
            "export_redis_1",
            "export_api_1",
            "send_alerts_1",
        ],
        "export_warehouse_1": ["update_catalog_1"],
        "export_redis_1": ["update_catalog_1"],
        "export_api_1": ["notify_team_1"],
        "send_alerts_1": ["notify_team_1"],
    },
    node_types={
        "ingest_clicks_1": "source",
        "ingest_txns_1": "source",
        "ingest_crm_1": "source",
        "deduplicate_1": "step",
        "normalize_1": "step",
        "validate_1": "step",
        "score_churn_1": "step",
        "score_ltv_1": "step",
        "build_profile_1": "step",
        "export_warehouse_1": "dest",
        "export_redis_1": "dest",
        "export_api_1": "dest",
        "send_alerts_1": "dest",
        "update_catalog_1": "dest",
        "notify_team_1": "dest",
    },
)

DOC_PROCESSING_WORKFLOW = WorkflowInfo(
    name="doc_processing",
    file_path="workflows/ml/doc_processing.py",
    node_ids=[
        "chunk_new_docs_1",
        "predict_chunks_1",
        "push_to_cdn_1",
        "push_chunks_pg_1",
        "embed_new_docs_1",
        "feed_into_vespa_1",
        "page_highlights_1",
        "push_questions_1",
        "push_chunk_preds_1",
        "corpus_prediction_1",
        "push_corpus_preds_1",
        "doc_prediction_1",
        "push_doc_preds_1",
        "delete_tagged_docs_1",
    ],
    graph={
        # chunk >> (push_chunks_pg & (embed >> vespa)) plus predict/cdn branches.
        "chunk_new_docs_1": [
            "predict_chunks_1",
            "push_to_cdn_1",
            "push_chunks_pg_1",
            "embed_new_docs_1",
        ],
        "predict_chunks_1": ["page_highlights_1"],
        "push_to_cdn_1": ["page_highlights_1"],
        "embed_new_docs_1": ["feed_into_vespa_1"],
        "page_highlights_1": ["push_questions_1", "push_chunk_preds_1"],
        # chunk_branch endpoints → delete_tagged_docs
        "push_chunks_pg_1": ["delete_tagged_docs_1"],
        "feed_into_vespa_1": ["delete_tagged_docs_1"],
        "push_questions_1": ["delete_tagged_docs_1"],
        "push_chunk_preds_1": ["delete_tagged_docs_1"],
        # pred_branch: (corpus >> push_corpus) & (doc >> push_doc) → delete_tagged_docs
        "corpus_prediction_1": ["push_corpus_preds_1"],
        "doc_prediction_1": ["push_doc_preds_1"],
        "push_corpus_preds_1": ["delete_tagged_docs_1"],
        "push_doc_preds_1": ["delete_tagged_docs_1"],
    },
    node_types={
        "chunk_new_docs_1": "step",
        "predict_chunks_1": "step",
        "push_to_cdn_1": "dest",
        "push_chunks_pg_1": "dest",
        "embed_new_docs_1": "step",
        "feed_into_vespa_1": "dest",
        "page_highlights_1": "step",
        "push_questions_1": "dest",
        "push_chunk_preds_1": "dest",
        "corpus_prediction_1": "step",
        "push_corpus_preds_1": "dest",
        "doc_prediction_1": "step",
        "push_doc_preds_1": "dest",
        "delete_tagged_docs_1": "dest",
    },
)

AGENT_TRACE_METADATA = {
    "signature": {
        "name": "InspectRecords",
        "instructions": "Inspect records and summarize active entries.",
        "inputs": [
            {
                "name": "records",
                "annotation": "list[Record]",
                "description": "records to inspect",
            }
        ],
        "outputs": [
            {
                "name": "summary",
                "annotation": "InspectionSummary",
                "description": "structured inspection summary",
            },
            {
                "name": "labels",
                "annotation": "list[str]",
                "description": "inspection labels",
            },
            {
                "name": "note",
                "annotation": "str | None",
                "description": "optional inspection note",
            },
        ],
    },
    "runtime": {
        "lm": "main-model",
        "sub_lm": "sub-model",
        "max_iterations": 4,
    },
    "skills": [
        {
            "name": "audit",
            "instructions": "Verify active status before summarizing.",
            "packages": ["pydantic"],
            "modules": ["record_helpers"],
            "tools": ["lookup_record"],
        }
    ],
    "aggregated_static_instructions": (
        "Inspect records and summarize active entries.\n\n"
        "## Skill: audit\n\nVerify active status before summarizing."
    ),
    "packages": ["pydantic"],
    "modules": ["record_helpers"],
    "tools": [
        {
            "name": "lookup_record",
            "description": "Look up a record by identifier.",
        }
    ],
}


AGENT_TRACE_WORKFLOW = WorkflowInfo(
    name="agent_trace",
    file_path="workflows/agents/agent_trace.py",
    node_ids=["inspect_agent_1"],
    graph={"inspect_agent_1": []},
    node_types={"inspect_agent_1": "step"},
    display_names={"inspect_agent_1": "inspect_agent"},
    agent_node_ids=["inspect_agent_1"],
    agent_metadata_json={
        "inspect_agent_1": json.dumps(AGENT_TRACE_METADATA, separators=(",", ":"))
    },
)


ALL_WORKFLOWS = [
    ORDER_WORKFLOW,
    INGEST_WORKFLOW,
    ANALYTICS_WORKFLOW,
    ML_WORKFLOW,
    DATA_PLATFORM_WORKFLOW,
    DOC_PROCESSING_WORKFLOW,
]

# ── Fake log templates ────────────────────────────────────────────────────

FAKE_LOGS: dict[str, list[tuple[LogLevel, str, float]]] = {
    "fetch_orders_1": [
        (LogLevel.INFO, "Connecting to orders API...", 0.0),
        (LogLevel.INFO, "Fetching page 1/3...", 0.3),
        (LogLevel.INFO, "Fetching page 2/3...", 0.7),
        (LogLevel.INFO, "Fetching page 3/3...", 1.2),
        (LogLevel.INFO, "Fetched 1,234 orders", 1.5),
    ],
    "fetch_inventory_1": [
        (LogLevel.INFO, "Scanning warehouse DB...", 0.0),
        (LogLevel.INFO, "Loading 847 SKUs...", 0.4),
        (LogLevel.WARN, "12 SKUs have no price data", 0.8),
        (LogLevel.INFO, "Inventory snapshot ready", 1.2),
    ],
    "validate_1": [
        (LogLevel.INFO, "Validating 1,234 orders against schema...", 0.0),
        (LogLevel.INFO, "Cross-referencing inventory...", 0.5),
        (LogLevel.WARN, "3 orders reference discontinued SKUs", 1.0),
        (LogLevel.INFO, "Validation complete: 1,231 valid, 3 warnings", 1.5),
    ],
    "enrich_1": [
        (LogLevel.INFO, "Starting enrichment for 1,231 orders...", 0.0),
        (LogLevel.INFO, "Joining inventory data...", 0.4),
        (LogLevel.INFO, "Computing margins...", 0.8),
        (LogLevel.DEBUG, "Margin cache hit rate: 94%", 1.0),
        (LogLevel.INFO, "Enrichment complete", 1.5),
    ],
    "aggregate_1": [
        (LogLevel.INFO, "Aggregating by region...", 0.0),
        (LogLevel.INFO, "Computing daily totals...", 0.4),
        (LogLevel.INFO, "Building summary: 5 regions, 1,231 orders", 0.8),
    ],
    "save_warehouse_1": [
        (LogLevel.INFO, "Writing to iceberg table orders_summary...", 0.0),
        (LogLevel.INFO, "Appended 5 rows, snapshot 42 → 43", 0.5),
        (LogLevel.INFO, "Warehouse save complete", 0.8),
    ],
    "notify_1": [
        (LogLevel.INFO, "Sending Slack notification...", 0.0),
        (LogLevel.INFO, "Notification sent to #data-alerts", 0.4),
    ],
    # Ingest workflow
    "extract_1": [
        (LogLevel.INFO, "Connecting to source...", 0.0),
        (LogLevel.INFO, "Extracting 5,000 records...", 0.5),
        (LogLevel.INFO, "Extraction complete", 1.2),
    ],
    "parse_1": [
        (LogLevel.INFO, "Parsing JSON records...", 0.0),
        (LogLevel.WARN, "2 records have malformed dates", 0.6),
        (LogLevel.INFO, "Parsed 4,998 records", 1.0),
    ],
    "deduplicate_1": [
        (LogLevel.INFO, "Deduplicating on primary key...", 0.0),
        (LogLevel.INFO, "Removed 47 duplicates", 0.8),
    ],
    "load_1": [
        (LogLevel.INFO, "Loading into warehouse...", 0.0),
        (LogLevel.INFO, "Loaded 4,951 rows", 0.6),
    ],
    # Analytics workflow
    "load_events_1": [
        (LogLevel.INFO, "Loading events from Kafka...", 0.0),
        (LogLevel.INFO, "Consumed 12,340 events", 1.0),
    ],
    "load_users_1": [
        (LogLevel.INFO, "Loading user profiles...", 0.0),
        (LogLevel.INFO, "Loaded 890 users", 0.8),
    ],
    "join_data_1": [
        (LogLevel.INFO, "Joining events with users...", 0.0),
        (LogLevel.INFO, "Join complete: 11,200 matched rows", 1.0),
    ],
    "compute_metrics_1": [
        (LogLevel.INFO, "Computing daily active users...", 0.0),
        (LogLevel.INFO, "Computing retention cohorts...", 0.5),
        (LogLevel.INFO, "Metrics computed", 1.2),
    ],
    "export_dashboard_1": [
        (LogLevel.INFO, "Pushing to Grafana dashboard...", 0.0),
        (LogLevel.INFO, "Dashboard updated", 0.5),
    ],
    "export_alerts_1": [
        (LogLevel.INFO, "Evaluating alert rules...", 0.0),
        (LogLevel.INFO, "No alerts triggered", 0.4),
    ],
    # ML workflow
    "fetch_training_1": [
        (LogLevel.INFO, "Loading training data from S3...", 0.0),
        (LogLevel.INFO, "Loaded 50,000 samples", 1.0),
    ],
    "fetch_validation_1": [
        (LogLevel.INFO, "Loading validation split...", 0.0),
        (LogLevel.INFO, "Loaded 10,000 samples", 0.8),
    ],
    "fetch_features_1": [
        (LogLevel.INFO, "Loading feature store snapshot...", 0.0),
        (LogLevel.INFO, "Loaded 120 features", 0.6),
    ],
    "preprocess_1": [
        (LogLevel.INFO, "Normalizing features...", 0.0),
        (LogLevel.INFO, "Imputing missing values (2.1% null)...", 0.4),
        (LogLevel.INFO, "Preprocessing complete", 1.0),
    ],
    "train_model_1": [
        (LogLevel.INFO, "Training XGBoost model...", 0.0),
        (LogLevel.INFO, "Epoch 1/10 — loss: 0.42", 0.5),
        (LogLevel.INFO, "Epoch 5/10 — loss: 0.18", 1.5),
        (LogLevel.INFO, "Epoch 10/10 — loss: 0.09", 2.5),
        (LogLevel.INFO, "Training complete — best loss: 0.09", 3.0),
    ],
    "evaluate_1": [
        (LogLevel.INFO, "Running evaluation on validation set...", 0.0),
        (LogLevel.INFO, "AUC: 0.94, F1: 0.87, Precision: 0.91", 1.0),
    ],
    "export_onnx_1": [
        (LogLevel.INFO, "Exporting model to ONNX format...", 0.0),
        (LogLevel.INFO, "Saved model.onnx (12.4 MB)", 0.5),
    ],
    "deploy_staging_1": [
        (LogLevel.INFO, "Deploying to staging endpoint...", 0.0),
        (LogLevel.INFO, "Health check passed", 0.8),
    ],
    "deploy_prod_1": [
        (LogLevel.INFO, "Deploying to production endpoint...", 0.0),
        (LogLevel.WARN, "Canary rollout: routing 10% traffic...", 0.5),
        (LogLevel.INFO, "Full rollout complete", 1.5),
    ],
    "notify_slack_1": [
        (LogLevel.INFO, "Posting results to #ml-deployments...", 0.0),
        (LogLevel.INFO, "Notification sent", 0.3),
    ],
}

# ── Execution phases for simulation ───────────────────────────────────────

EXECUTION_PHASES: dict[str, list[tuple[list[str], float, str | None]]] = {
    "order_workflow": [
        (["fetch_orders_1", "fetch_inventory_1"], 2.5, None),
        (["validate_1"], 2.5, None),
        (["enrich_1"], 2.5, None),
        (["aggregate_1"], 2.0, None),
        (["save_warehouse_1", "notify_1"], 2.0, None),
    ],
    "ingest_workflow": [
        (["extract_1"], 2.0, None),
        (["parse_1"], 1.5, None),
        (["deduplicate_1"], 1.5, None),
        (["load_1"], 1.0, None),
    ],
    "analytics_workflow": [
        (["load_events_1", "load_users_1"], 2.0, None),
        (["join_data_1"], 2.0, None),
        (["compute_metrics_1"], 2.0, None),
        (["export_dashboard_1", "export_alerts_1"], 1.5, None),
    ],
    "ml_workflow": [
        (["fetch_training_1", "fetch_validation_1", "fetch_features_1"], 2.0, None),
        (["preprocess_1"], 2.0, None),
        (["train_model_1"], 4.0, None),
        (["evaluate_1"], 2.0, None),
        (["export_onnx_1", "deploy_staging_1", "deploy_prod_1"], 2.0, None),
        (["notify_slack_1"], 1.0, None),
    ],
    "data_platform": [
        (["ingest_clicks_1", "ingest_txns_1", "ingest_crm_1"], 3.0, None),
        (["deduplicate_1"], 2.0, None),
        (["normalize_1"], 1.5, None),
        (["validate_1"], 1.0, None),
        (["score_churn_1", "score_ltv_1"], 3.0, None),
        (["build_profile_1"], 2.0, None),
        (
            ["export_warehouse_1", "export_redis_1", "export_api_1", "send_alerts_1"],
            2.0,
            None,
        ),
        (["update_catalog_1", "notify_team_1"], 1.0, None),
    ],
    "doc_processing": [
        (["chunk_new_docs_1", "corpus_prediction_1", "doc_prediction_1"], 3.0, None),
        (
            ["predict_chunks_1", "push_to_cdn_1", "push_chunks_pg_1", "embed_new_docs_1"],
            2.0,
            None,
        ),
        (
            [
                "page_highlights_1",
                "feed_into_vespa_1",
                "push_corpus_preds_1",
                "push_doc_preds_1",
            ],
            2.0,
            None,
        ),
        (["push_questions_1", "push_chunk_preds_1", "delete_tagged_docs_1"], 1.5, None),
    ],
}

SKIP_ON_FAIL = {"aggregate_1", "save_warehouse_1", "notify_1"}


class MockStateProvider:
    """Implements StateProvider with timer-driven simulation."""

    def __init__(self, *, include_agent_trace: bool = False) -> None:
        self.operator_instance_id = "mock"
        self.operator_reachable = True
        self.stream_state = "live"
        self.stream_error = ""
        self._workflows = {p.selector: p for p in ALL_WORKFLOWS}
        if include_agent_trace:
            self._workflows[AGENT_TRACE_WORKFLOW.selector] = AGENT_TRACE_WORKFLOW
        self._runs: dict[str, RunState] = {}
        self._run_callbacks: list[Callable[[RunState], None]] = []
        self._catalog_callbacks: list[Callable[[CatalogSnapshot], None]] = []
        self._log_callbacks: list[Callable[[LogEntry], None]] = []
        self._detail_callbacks: list[Callable[[DetailUpdate], None]] = []
        self._stream_reset_callbacks: list[Callable[[StreamResetNotice], None]] = []
        self._threads: list[threading.Thread] = []

        self._pre_seed_runs()

    def _pre_seed_runs(self) -> None:
        """Pre-seed completed runs so history has data on startup."""
        base_time = time.monotonic() - 300  # 5 minutes ago

        # A successful order_workflow run
        run1 = RunState(
            run_id=f"run_{str(uuid4())[:8]}",
            flow_name="order_workflow",
            status=RunStatus.SUCCESS,
            started_at=base_time,
            ended_at=base_time + 11.5,
        )
        for nid in self._workflows["order_workflow"].node_ids:
            nt = self._workflows["order_workflow"].node_types[nid]
            run1.nodes[nid] = NodeState(
                node_id=nid,
                name=display_name_from_id(nid),
                node_type=nt,
                status=NodeStatus.SUCCESS,
                started_at=base_time,
                ended_at=base_time + 2.0,
            )
        self._runs[run1.run_id] = run1

        # A failed ingest run
        run2 = RunState(
            run_id=f"run_{str(uuid4())[:8]}",
            flow_name="ingest_workflow",
            status=RunStatus.FAILED,
            started_at=base_time + 60,
            ended_at=base_time + 65,
        )
        for nid in ["extract_1", "parse_1"]:
            nt = self._workflows["ingest_workflow"].node_types[nid]
            status = NodeStatus.SUCCESS if nid == "extract_1" else NodeStatus.FAILED
            run2.nodes[nid] = NodeState(
                node_id=nid,
                name=display_name_from_id(nid),
                node_type=nt,
                status=status,
                started_at=base_time + 60,
                ended_at=base_time + 62,
            )
        for nid in ["deduplicate_1", "load_1"]:
            nt = self._workflows["ingest_workflow"].node_types[nid]
            run2.nodes[nid] = NodeState(
                node_id=nid,
                name=display_name_from_id(nid),
                node_type=nt,
                status=NodeStatus.SKIPPED,
            )
        self._runs[run2.run_id] = run2

        trace_events = [
            {
                "sequence": 1,
                "kind": "code.generated",
                "timestamp_ns": 1,
                "data": {
                    "iteration": 1,
                    "code": (
                        "records = [item for item in input_records if item.get('active')]\\n"
                        "summary = {'active_count': len(records), 'names': "
                        "[item['name'] for item in records]}\\n"
                        "print(summary)"
                    ),
                },
            },
            {
                "sequence": 2,
                "kind": "code.executed",
                "timestamp_ns": 2,
                "data": {"iteration": 1, "output": "SANDBOX_STDOUT_SENTINEL"},
            },
            {
                "sequence": 3,
                "kind": "iteration.recorded",
                "timestamp_ns": 3,
                "data": {"step": {"iteration": 1}},
            },
            {
                "sequence": 4,
                "kind": "run.succeeded",
                "timestamp_ns": 4,
                "data": {
                    "status": "completed",
                    "outputs": {
                        "summary": {"active_count": 1, "ready": False},
                        "labels": ["reviewed"],
                        "note": None,
                    },
                },
            },
        ]
        trace_step = {
            "iteration": 1,
            "reasoning": "Filter active records and summarize their names.",
            "code": trace_events[0]["data"]["code"],
            "output": "SANDBOX_STDOUT_SENTINEL",
            "untruncated_output": (
                "FULL OUTPUT: {'active_count': 2, 'names': "
                "['Ada Lovelace', 'Grace Hopper']}\\n" + "wrapped output line " * 12
            ),
            "error": False,
            "duration_ms": 14,
            "tool_calls": [],
            "predict_calls": [],
            "lm": {"finish_reason": "stop"},
            "usage": {"main": {}, "sub": {}},
        }
        trace_envelope = {
            "schema_version": 1,
            "status": "completed",
            "run_id": "agent-mock",
            "events": [
                {
                    "sequence": event["sequence"],
                    "event_kind": event["kind"],
                    "timestamp_ns": event["timestamp_ns"],
                    "data": event["data"],
                }
                for event in trace_events
            ],
            "trace": {
                "status": "completed",
                "model": "mock-main",
                "sub_model": "mock-sub",
                "iterations": 1,
                "max_iterations": 4,
                "duration_ms": 20,
                "usage": {"main": {}, "sub": {}},
                "steps": [trace_step],
                "evidence": {
                    "run_id": "agent-mock",
                    "complete": True,
                    "terminal_outcome": "completed",
                    "events": trace_events,
                },
            },
            "error": None,
        }
        agent_run = RunState(
            run_id="run_agent",
            flow_name="agent_trace",
            status=RunStatus.SUCCESS,
            started_at=base_time,
            ended_at=base_time + 1,
        )
        agent_run.nodes["inspect_agent_1"] = NodeState(
            node_id="inspect_agent_1",
            name="inspect_agent",
            node_type="step",
            status=NodeStatus.SUCCESS,
            started_at=base_time,
            ended_at=base_time + 1,
            agent_trace_json=json.dumps(trace_envelope),
        )
        agent_run.logs.extend(
            [
                LogEntry(
                    datetime.now(),
                    LogLevel.INFO,
                    "inspect_agent_1",
                    "Agent code.generated iteration=1",
                ),
                LogEntry(
                    datetime.now(),
                    LogLevel.INFO,
                    "inspect_agent_1",
                    "Agent iteration.recorded iteration=1 duration=14ms",
                ),
            ]
        )
        if "agent_trace" in self._workflows:
            self._runs[agent_run.run_id] = agent_run

    def list_workflows(self) -> list[WorkflowInfo]:
        return list(self._workflows.values())

    def list_runs(self, workflow_selector: str) -> list[RunState]:
        return [
            run
            for run in self._runs.values()
            if (run.workflow_id or run.flow_name) == workflow_selector
        ]

    def get_run(self, run_id: str) -> RunState | None:
        return self._runs.get(run_id)

    def hydrate_trace(self, run_id: str, node_id: str) -> TraceDetail | None:
        run = self.get_run(run_id)
        node = run.nodes.get(node_id) if run is not None else None
        descriptor = node.trace if node is not None else None
        if run is None or node is None or descriptor is None or not node.agent_trace_json:
            return None
        try:
            envelope = json.loads(node.agent_trace_json)
        except (TypeError, ValueError):
            return None
        trace_body = envelope.get("trace") if isinstance(envelope, dict) else None
        if not isinstance(trace_body, dict):
            return None
        return TraceDetail(
            operator_instance_id=run.operator_instance_id,
            run_id=run.run_id,
            created_sequence=run.created_sequence,
            node_id=node_id,
            descriptor_revision=descriptor.revision,
            trace_body=trace_body,
        )

    def close(self) -> None:
        """Mock provider owns no external resources."""

    def start_run(self, workflow_selector: str, **kwargs) -> str:
        info = self._workflows.get(workflow_selector)
        if info is None:
            matches = [
                item
                for item in self._workflows.values()
                if workflow_selector in {item.name, item.rendered_name, item.builder_symbol}
            ]
            info = matches[0] if len(matches) == 1 else None
        if info is None:
            raise ValueError(f"Unknown or ambiguous workflow: {workflow_selector}")

        run_id = f"run_{str(uuid4())[:8]}"
        run = RunState(
            run_id=run_id,
            flow_name=info.rendered_name,
            workflow_id=info.selector,
            workflow_display_name=info.rendered_name,
            status=RunStatus.RUNNING,
            started_at=time.monotonic(),
        )
        for nid in info.node_ids:
            nt = info.node_types[nid]
            run.nodes[nid] = NodeState(
                node_id=nid,
                name=display_name_from_id(nid),
                node_type=nt,
                status=NodeStatus.PENDING,
            )
        self._runs[run_id] = run

        # Start simulation thread
        t = threading.Thread(
            target=self._simulate_run,
            args=(run, info),
            daemon=True,
        )
        self._threads.append(t)
        t.start()

        return run_id

    def cancel_run(self, run_id: str) -> None:
        run = self._runs.get(run_id)
        if run and run.status == RunStatus.RUNNING:
            run.status = RunStatus.CANCELLED
            run.ended_at = time.monotonic()
            for ns in run.nodes.values():
                if ns.status in (NodeStatus.PENDING, NodeStatus.RUNNING):
                    ns.status = NodeStatus.SKIPPED
                    if ns.started_at and not ns.ended_at:
                        ns.ended_at = time.monotonic()
            self._notify_run(run)

    def get_catalog(self) -> CatalogSnapshot:
        return CatalogSnapshot(
            operator_instance_id=self.operator_instance_id,
            workflows=tuple(self.list_workflows()),
        )

    def on_run_update(self, callback: Callable[[RunState], None]) -> None:
        self._run_callbacks.append(callback)

    def on_catalog_update(self, callback: Callable[[CatalogSnapshot], None]) -> None:
        self._catalog_callbacks.append(callback)

    def on_log(self, callback: Callable[[LogEntry], None]) -> None:
        self._log_callbacks.append(callback)

    def on_detail_update(self, callback: Callable[[DetailUpdate], None]) -> None:
        self._detail_callbacks.append(callback)

    def start_stream(self) -> None:
        """Mocks deliver callbacks directly and need no stream worker."""

    def on_stream_reset(self, callback: Callable[[StreamResetNotice], None]) -> None:
        self._stream_reset_callbacks.append(callback)

    def load_reset_baseline(self, notice: StreamResetNotice) -> ResetBaseline:
        catalog = self.get_catalog()
        return ResetBaseline(
            generation=notice.generation,
            operator_instance_id=self.operator_instance_id,
            as_of_event_ulid=(notice.observed_event_ulid or "00000000000000000000000000"),
            catalog=catalog,
            runs_by_workflow={
                workflow.selector: tuple(self.list_runs(workflow.selector))
                for workflow in catalog.workflows
            },
        )

    def acknowledge_stream_reset(
        self,
        generation: int,
        operator_instance_id: str,
        reconciled_event_ulid: str,
    ) -> None:
        if self.stream_state != "reset_required":
            raise RuntimeError("stream reset is not required")
        self.operator_instance_id = operator_instance_id
        self.stream_state = "live"

    def _notify_run(self, run: RunState) -> None:
        for cb in self._run_callbacks:
            cb(run)

    def _notify_log(self, run: RunState, entry: LogEntry) -> None:
        for cb in self._log_callbacks:
            cb(entry)
        detail = LogDetailAppended(
            operator_instance_id=self.operator_instance_id,
            run_id=run.run_id,
            created_sequence=run.created_sequence,
            sequence=len(run.logs),
            log_sequence=len(run.logs),
            log=entry,
        )
        for cb in self._detail_callbacks:
            cb(detail)

    def _simulate_run(self, run: RunState, info: WorkflowInfo) -> None:
        """Background thread that drives a run through execution phases."""
        phases = EXECUTION_PHASES.get(info.name, [])

        for names, duration, fail_name in phases:
            if run.status != RunStatus.RUNNING:
                return

            # Start nodes
            now = time.monotonic()
            for name in names:
                ns = run.nodes.get(name)
                if ns and ns.status == NodeStatus.PENDING:
                    ns.status = NodeStatus.RUNNING
                    ns.started_at = now
                    self._notify_run(run)

            # Emit logs during the phase
            phase_start = time.monotonic()
            self._emit_phase_logs(run, names, duration, phase_start)

            # Wait for phase duration
            elapsed = time.monotonic() - phase_start
            remaining = duration - elapsed
            if remaining > 0:
                time.sleep(remaining)

            if run.status != RunStatus.RUNNING:
                return

            # Complete nodes
            now = time.monotonic()
            for name in names:
                ns = run.nodes.get(name)
                if ns:
                    ns.status = NodeStatus.FAILED if name == fail_name else NodeStatus.SUCCESS
                    ns.ended_at = now

            if fail_name:
                for ns in run.nodes.values():
                    if ns.node_id in SKIP_ON_FAIL and ns.status == NodeStatus.PENDING:
                        ns.status = NodeStatus.SKIPPED
                run.status = RunStatus.FAILED
                run.ended_at = now
                self._notify_run(run)
                return

            self._notify_run(run)

        # All phases complete
        run.status = RunStatus.SUCCESS
        run.ended_at = time.monotonic()
        self._notify_run(run)

    def _emit_phase_logs(
        self,
        run: RunState,
        node_names: list[str],
        duration: float,
        phase_start: float,
    ) -> None:
        """Emit log entries for nodes during a phase, respecting timing offsets."""
        log_schedule: list[tuple[float, str, LogLevel, str]] = []
        for name in node_names:
            templates = FAKE_LOGS.get(name, [])
            for level, msg, offset in templates:
                if offset < duration:
                    log_schedule.append((offset, name, level, msg))
        log_schedule.sort(key=lambda x: x[0])

        for offset, name, level, msg in log_schedule:
            if run.status != RunStatus.RUNNING:
                return
            wait = offset - (time.monotonic() - phase_start)
            if wait > 0:
                time.sleep(wait)
            entry = LogEntry(
                timestamp=datetime.now(),
                level=level,
                node_id=name,
                message=msg,
            )
            run.logs.append(entry)
            self._notify_log(run, entry)
