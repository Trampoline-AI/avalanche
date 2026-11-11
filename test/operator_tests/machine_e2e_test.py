"""Operator proofs: real capture/discovery and state work, no mocked scheduler."""

import asyncio
import importlib.util
import json
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

import avalanche as ava
from avalanche.machine_waits import MachineWaits
from runtime.operator.discovery import WorkflowDiscoveryError, raise_for_discovery_diagnostics
from runtime.operator.registry import WorkflowRegistry

SDK = Path(__file__).resolve().parents[2]
EXAMPLE = SDK / "examples" / "pr_lifecycle.py"
GOLDEN = SDK / "test" / "fixtures" / "pr_lifecycle.machine.json"


def load_example():
    spec = importlib.util.spec_from_file_location("machine_pr_example", EXAMPLE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_review_loop():
    example = load_example()
    example.test_review_loop()
    result = (
        example.review_round()
        .run(input=example.PR(repo="acme/api", number=42, head_sha="a1"))
        .result(timeout=5)
    )
    assert result.name == "review_done"
    assert isinstance(result.output, example.ReviewReport)
    registry = WorkflowRegistry()
    registry.scan([str(EXAMPLE)])
    info = next(info for info in registry.list_workflows() if info.name == "pr_lifecycle")
    captured = json.loads(info.machine_spec_json)
    assert captured == json.loads(GOLDEN.read_text())
    # Reconciliation needs each held node's own schema, not the work's result.
    work = captured["work_graphs"]["amend_round"]
    for node_id, policy in work["node_policies"].items():
        if policy["on_uncertain"] == "hold":
            assert work["node_output_schemas"][node_id] == (
                example.amend_round().nodes[node_id].node.step_interface.step_output.json_schema
            )
    # A builder is traced once and discovery never executes its async work.
    assert example.pr_lifecycle() is example.pr_lifecycle()
    keyed_graph = example.keyed_notification()
    assert {
        "key_fields": list(keyed_graph.key_fields),
        "input_schema": keyed_graph.input_type.model_json_schema(),
        "node_types": {
            name: ref.node.node_type.value for name, ref in keyed_graph.nodes.items()
        },
    } == json.loads((GOLDEN.parent / "keyed_notification.workflow.json").read_text())
    router_graph = example.lifecycle_router()
    router_topology = {
        "graph": dict(router_graph.graph),
        "node_types": {
            name: ref.node.node_type.value for name, ref in router_graph.nodes.items()
        },
        "node_runs": {name: spec.model_dump() for name, spec in router_graph.node_runs.items()},
        "node_machines": {
            name: spec.model_dump() for name, spec in router_graph.node_machines.items()
        },
    }
    assert router_topology == json.loads(
        (GOLDEN.parent / "pr_lifecycle.router.json").read_text()
    )
    routed = router_graph.test(
        example.Delivery(
            repo="acme/api", number=46, head_sha="a1", action="opened", delivery_id="d46"
        )
    )
    assert routed.handle.result(timeout=1)[0].created
    assert routed.handle.result(timeout=1)[1] is None
    assert example.pr_lifecycle.key(example.PR(repo="acme/api", number=42, head_sha="a1")) == (
        "acme/api",
        42,
    )
    timed = example.pr_lifecycle.test(example.PR(repo="acme/api", number=43, head_sha="a1"))
    assert timed.state == "ready_to_merge"
    timed.advance_time(timedelta(days=30))
    assert timed.state == "closed"
    assert timed.history[-2].source == "timer"

    # Router work stays ordinary graph work; machine calls are host-owned nodes.
    @ava.step
    def request() -> example.PR:
        return example.PR(repo="acme/api", number=44, head_sha="a1")

    @ava.step
    def instance_key(started: ava.RunStart) -> str:
        return started.run_id

    @ava.step
    def event_name() -> str:
        return "merged"

    @ava.workflow
    def router():
        value = request()
        started = ava.start_run(example.pr_lifecycle, value)
        # Existing key validates identity only and ignores invalid replacement input.
        again = ava.start_run(
            example.pr_lifecycle, {"repo": "acme/api", "number": 44, "head_sha": 123}
        )
        started >> again
        key = instance_key(again)
        receipt = example.pr_lifecycle.send(key, event_name())
        return started, again, receipt

    graph = router()
    assert {spec.op for spec in graph.node_machines.values()} == {"send"}
    assert len(graph.node_runs) == 2
    from runtime.operator.registry import workflow_to_info

    info = workflow_to_info(graph, __file__)
    assert info.node_machines == graph.node_machines
    assert info.node_runs == graph.node_runs
    with pytest.raises(RuntimeError, match="MachineHost"):
        graph.run()
    with pytest.raises(TypeError, match="ava run"):
        ava.start_run(
            example.pr_lifecycle, example.PR(repo="acme/api", number=44, head_sha="a1")
        )
    with pytest.raises(TypeError, match="host="):
        example.pr_lifecycle.send(("acme/api", 44), "merged")

    run = graph.test()
    started, again, receipt = run.handle.result(timeout=1)
    assert started.created and not again.created and started.run_id == again.run_id
    assert receipt.status == "accepted" and not receipt.duplicate
    instance = next(iter(run.host.instances.values()))
    assert instance.state == "closed" and instance.handle.done()
    assert [item.event for item in instance.history if item.source == "external"] == ["merged"]
    send_id = next(name for name, spec in graph.node_machines.items() if spec.op == "send")
    step_key = ava.RunContext(
        run_id=run.handle.run_id,
        workflow_name=graph.name,
        node_id=send_id,
    ).step_key
    assert next(iter(run.host.receipts))[-1] == step_key

    async def replay_and_explicit_host():
        replay = await example.pr_lifecycle.send(
            ("acme/api", 44),
            "merged",
            idempotency_key=step_key,
            host=run.host,
        )
        assert replay.receipt_id == receipt.receipt_id and replay.duplicate
        assert replay.accepted_at == receipt.accepted_at
        from avalanche.local_waits import WaitRefusalError

        with pytest.raises(WaitRefusalError, match="conflict"):
            await example.pr_lifecycle.send(
                ("acme/api", 44),
                "merged",
                {"changed": True},
                idempotency_key=step_key,
                host=run.host,
            )
        with pytest.raises(WaitRefusalError, match="run_closed"):
            await example.pr_lifecycle.send(
                ("acme/api", 44),
                "merged",
                idempotency_key="new",
                host=run.host,
            )
        with pytest.raises(TypeError, match="host"):
            ava.start_run(
                example.pr_lifecycle,
                example.PR(repo="acme/api", number=44, head_sha="a1"),
                host=run.host,
            )
        with pytest.raises(ValueError, match="idempotency_key"):
            example.pr_lifecycle.send(("acme/api", 44), "merged", host=run.host)
        async_run = await graph.test_async()
        assert async_run.handle.result(timeout=1)[2].status == "accepted"

    asyncio.run(replay_and_explicit_host())

    class FlowInput(ava.BaseInput):
        id: int
        body: str

    @ava.step
    def keyed_result(input: FlowInput) -> str:
        return input.body

    @ava.workflow(input=FlowInput, key=("id",))
    def keyed_child():
        return keyed_result()

    @ava.workflow
    def keyed_router():
        first = ava.start_run(keyed_child, FlowInput(id=1, body="first"))
        again = ava.start_run(keyed_child, {"id": "1", "body": 999})
        second = ava.start_run(keyed_child, FlowInput(id=2, body="second"))
        first >> again >> second
        return first, again, second

    keyed = keyed_router().test()
    first, again, second = keyed.handle.result(timeout=1)
    assert first.created and not again.created and second.created
    assert first.run_id == again.run_id and second.run_id != first.run_id
    assert first.key == [1] and second.key == [2]
    assert [run.handle.result(timeout=1) for run in keyed.host.flow_runs] == ["first", "second"]
    assert keyed_child().key({"id": "1"}) == (1,)
    with pytest.raises(RuntimeError, match="scheduler host"):
        keyed_child().run(input=FlowInput(id=1, body="first"))

    class RouteInput(ava.BaseInput):
        start: bool

    @ava.step
    def should_start(input: RouteInput) -> bool:
        return input.start

    @ava.step
    def should_send(input: RouteInput) -> bool:
        return not input.start

    @ava.step
    def flow_target() -> str:
        return "child_flow"

    @ava.step
    def child_result() -> int:
        return 42

    @ava.workflow
    def child_flow():
        return child_result()

    @ava.workflow(input=RouteInput)
    def conditional_router():
        seeded = ava.start_run(
            example.pr_lifecycle, example.PR(repo="acme/api", number=45, head_sha="a1")
        )
        started = ava.start_run(
            example.pr_lifecycle, {"repo": "acme/api", "number": 45}, when=should_start()
        )
        sent = example.pr_lifecycle.send(("acme/api", 45), "merged", when=should_send())
        # Skipped operations do not validate otherwise invalid arguments or call the host.
        skipped = ava.start_run("missing", {"invalid": True}, when=False)
        skipped_send = example.pr_lifecycle.send((), "", when=False)
        seeded >> started >> sent >> skipped >> skipped_send
        child = ava.start_run(flow_target())
        skipped_send >> child
        return started, sent, skipped, skipped_send, child

    for start in (True, False):
        conditional = conditional_router().test(RouteInput(start=start), targets=(child_flow,))
        started, sent, skipped, skipped_send, child = conditional.handle.result(timeout=1)
        assert (started is not None) == start
        assert (sent is None) == start
        assert skipped is None and skipped_send is None
        assert child.created and child.key is None
        assert conditional.host.flow_runs[0].handle.result(timeout=1) == 42
        assert len(conditional.host.instances) == 1
        assert next(iter(conditional.host.instances.values())).state == (
            "ready_to_merge" if start else "closed"
        )


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ('a = ava.state(work); a.on("go") >> a', "exactly one initial"),
        (
            "a = ava.state(work, initial=True); b = ava.state(work, initial=True)",
            "exactly one initial",
        ),
        ('a = ava.state(work, initial=True, final=True); a.on("go") >> a', "final state"),
        ('a = ava.state(work, initial=True); a.on("go") >> "missing"', "unknown target state"),
        (
            'a = ava.state(work, initial=True); b = ava.state(emitter); a.on("done") >> b',
            "not declared",
        ),
        ("a = ava.state(too_big, initial=True)", "64 nodes"),
        ("a = ava.state(bad_workflow, initial=True)", "workflow emits"),
    ],
)
def test_machine_discovery_rejects_invalid_visit(tmp_path, body, error):
    fixture = tmp_path / "flow.py"
    fixture.write_text(
        """import avalanche as ava
@ava.step
async def work() -> None:
    pass
@ava.step(emits=("done",))
async def emitter() -> ava.Event[int]:
    raise AssertionError("discovery executed work")
@ava.workflow
def too_big():
    for _ in range(65):
        work()
@ava.workflow(emits=("other",))
def bad_workflow():
    return emitter()
@ava.machine(input=Input, key=("id",))
def invalid():
    BODY
""".replace("@ava.machine", "class Input(ava.BaseInput):\n    id: int\n@ava.machine").replace(
            "BODY", body
        )
    )
    registry = WorkflowRegistry()
    registry.scan([str(fixture)])
    with pytest.raises(WorkflowDiscoveryError, match=error):
        raise_for_discovery_diagnostics(tuple(registry.list_diagnostics()))


@pytest.mark.asyncio
async def test_machine_run_control_early_events_payloads_and_defer(tmp_path):
    class Input(ava.BaseInput):
        id: int

    class Amended(BaseModel):
        sha: str
        file: ava.File | None = None
        tree: ava.Workspace | None = None

    entered, release = asyncio.Event(), asyncio.Event()
    received: list[str] = []
    from avalanche.references import local_workspace

    (tmp_path / "payload.txt").write_text("retained event bytes")
    tree = local_workspace(tmp_path)
    file = ava.File.from_path(tree.path / "payload.txt", workspace=tree)

    effects: set[str] = set()

    @ava.step
    async def idle(context: ava.RunContext) -> None:
        # A recipient dedupes retry identities but must not collapse distinct visits.
        effects.add(context.step_key)

    @ava.step
    async def amend_work() -> None:
        entered.set()
        await release.wait()

    @ava.step
    async def close_work(payload: Amended) -> None:
        received.append(payload.sha)
        if payload.sha == "routed":
            assert payload.file.read_bytes() == b"retained event bytes"
            assert (payload.tree.path / "payload.txt").read_text() == "retained event bytes"

    @ava.machine(input=Input, key=("id",))
    def lifecycle():
        review = ava.state(idle, initial=True, defer=["deferred"])
        amend = ava.state(amend_work, defer=["deferred"])
        ready = ava.state(idle)
        closed = ava.state(close_work, final=True)
        review.on("start") >> amend
        amend.on("amended", payload=Amended) >> ready
        ready.on("deferred", payload=Amended) >> closed
        amend.after(timedelta(seconds=5)) >> closed

    m = await lifecycle.test_async(Input(id=1))
    assert (await m.asend("amended", Amended(sha="ignored"))).reason == "ignored"
    assert (await m.asend("deferred", Amended(sha="queued"))).reason == "accepted"
    drive = asyncio.create_task(m.asend("start"))
    await entered.wait()
    with pytest.raises(ValidationError):
        await m.asend("amended", {"sha": 123})
    assert (await m.asend("amended", Amended(sha="first"))).reason == "accepted"
    assert (await m.asend("amended", Amended(sha="stale"))).reason == "accepted"
    assert m.state == "amend"  # accepted early, but work has not completed
    release.set()
    await drive
    assert m.state == "closed"
    assert received == ["queued"]
    assert len(effects) == 2
    assert [(f.kind, f.event) for f in m.waits.facts] == [
        ("event_ignored", "amended"),
        ("event_dropped", "amended"),
    ]
    assert isinstance(m.handle.waits, MachineWaits)
    assert m.handle.done()
    # Exited amend's timer is cancelled; advancing it cannot add history.
    history = list(m.history)
    await m.aadvance_time(timedelta(seconds=5))
    assert m.history == history

    @ava.step
    def routed_payload() -> Amended:
        return Amended(sha="routed", file=file, tree=tree)

    @ava.step
    def routed_event() -> str:
        return "deferred"

    @ava.step
    def delivery_id() -> str:
        return "delivery-2"

    @ava.workflow
    def payload_router():
        started = ava.start_run(lifecycle, Input(id=2))
        buffered = lifecycle.send(
            (2,),
            routed_event(),
            routed_payload(),
            idempotency_key=delivery_id(),
        )
        started >> buffered
        start_event = lifecycle.send((2,), "start")
        buffered >> start_event
        amended = lifecycle.send((2,), "amended", Amended(sha="advance"))
        start_event >> amended
        return amended

    routed = await payload_router().test_async()
    assert routed.handle.result(timeout=1).status == "accepted"
    routed_instance = next(iter(routed.host.instances.values()))
    assert routed_instance.state == "closed"
    assert received == ["queued", "routed"]
    assert len(effects) == 4
    assert any(address[-1] == "delivery-2" for address in routed.host.receipts)

    # The test host follows the same run-owned workspace/commit path as the
    # spawned operator, including a child started by the final visit.
    spec = importlib.util.spec_from_file_location(
        "artifact_machine_fixture", SDK / "test" / "fixtures" / "local_machine.py"
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    source = tmp_path / "portable"
    source.mkdir()
    (source / "data.txt").write_text("captured")
    ticket = fixture.ArtifactTicket(
        ticket=90,
        journal=str(tmp_path / "visits"),
        document=file,
        workspace=ava.Workspace.from_path(source),
    )
    instance = await fixture.artifact_approval.test_async(ticket)
    identity = instance.handle.run_id
    committed = local_workspace(source)
    await instance.asend(
        "artifacts", fixture.ArtifactDelivery(document=file, workspace=committed)
    )
    assert {file.ref.workspace, committed.reference} <= instance.waits.reference_grants
    await instance.asend("approved")
    assert instance.handle.run_id == identity and instance.handle.done()
    assert instance.host.flow_runs[0].handle.result() == "retained event bytes:capturedxxx"
    assert (committed.path / "data.txt").read_text() == "captured"
    assert instance.host.flow_runs[0].input.document.ref == file.ref


def test_machine_visit_reconciliation_reuses_local_waits():
    class Input(ava.BaseInput):
        id: int

    class Result(BaseModel):
        message: str

    calls: list[str] = []

    @ava.step(on_uncertain="hold", emits=("done",))
    async def uncertain() -> ava.Event[Result]:
        calls.append("effect")
        raise ava.OutcomeUnknownError("reply lost")

    @ava.step
    async def finish(result: Result) -> None:
        assert result.message == "verified"

    @ava.workflow(output=Result, emits=("done",))
    def work():
        return uncertain()

    @ava.machine(input=Input, key=("id",))
    def reconciled():
        waiting = ava.state(work, initial=True)
        closed = ava.state(finish, final=True)
        waiting.on("done") >> closed

    m = reconciled.test(Input(id=1))
    assert m.state == "waiting"
    assert not m.handle.done()
    assert calls == ["effect"]
    m.decide(
        "uncertain",
        {
            "kind": "done",
            "output": {
                "name": "done",
                "output": {"message": "verified"},
            },
        },
        reason="checked the external effect",
    )
    assert m.state == "closed"
    assert m.handle.done()
    assert calls == ["effect"]

    child_keys: set[str] = set()

    @ava.step(on_uncertain="hold")
    async def mapped(value: int, context: ava.RunContext) -> int:
        child_keys.add(context.step_key)
        calls.append(f"mapped:{value}")
        if value == 1:
            raise ava.OutcomeUnknownError("reply lost")
        return value * 10

    @ava.workflow
    def mapped_work():
        return mapped.map([1, 2], max_items=2)

    @ava.machine(input=Input, key=("id",))
    def mapped_machine():
        closed = ava.state(mapped_work, initial=True, final=True)
        return closed

    mapped_run = mapped_machine.test(Input(id=2))
    assert not mapped_run.handle.done()
    mapped_run.decide("mapped[0]", {"kind": "done", "output": 10}, reason="verified")
    assert mapped_run.handle.result(timeout=1) == [10, 20]
    assert calls == ["effect", "mapped:1", "mapped:2"]
    assert len(child_keys) == 2


def test_machine_discovery_warns_without_executing_work(tmp_path):
    fixture = tmp_path / "warning_flow.py"
    fixture.write_text("""import avalanche as ava
class Input(ava.BaseInput):
    id: int
@ava.step(emits=("unhandled",))
async def work() -> ava.Event[int]:
    raise AssertionError("discovery executed work")
@ava.machine(input=Input, key=("id",), cron="0 * * * *",
             webhook=ava.Webhook(path="/machine"))
def warned():
    waiting = ava.state(work, initial=True)
    waiting.on("external") >> waiting
@ava.workflow(input=Input, key=("id",), emits=("unhandled",))
def keyed_flow():
    return work()
@ava.workflow
def router():
    started = ava.start_run(warned, Input(id=1))
    sent = warned.send((1,), "external")
    started >> sent
    return sent
""")
    registry = WorkflowRegistry(cache_dir=tmp_path / "cache")
    registry.scan([str(fixture)])
    diagnostics = tuple(registry.list_diagnostics())
    assert [d.kind for d in diagnostics] == ["warning"]
    assert "unhandled" in diagnostics[0].message
    raise_for_discovery_diagnostics(diagnostics)
    assert sorted(info.name for info in registry.list_workflows()) == [
        "keyed_flow",
        "router",
        "warned",
    ]
    keyed_info = next(info for info in registry.list_workflows() if info.name == "keyed_flow")
    assert keyed_info.key_fields == ("id",)
    machine_info = next(info for info in registry.list_workflows() if info.name == "warned")
    assert machine_info.cron == "0 * * * *"
    assert machine_info.webhook_path == "/machine"
    router = next(info for info in registry.list_workflows() if info.name == "router")
    assert {name: spec.model_dump() for name, spec in router.node_machines.items()} == {
        "warned_send_1": {"machine": "warned", "op": "send"},
    }
    assert {name: spec.model_dump() for name, spec in router.node_runs.items()} == {
        "start_run_1": {"op": "start"},
    }
    cached = WorkflowRegistry(cache_dir=tmp_path / "cache")
    cached.scan([str(fixture)])
    cached_router = next(info for info in cached.list_workflows() if info.name == "router")
    assert cached_router.node_machines == router.node_machines
    assert cached_router.node_runs == router.node_runs
    cached_keyed = next(info for info in cached.list_workflows() if info.name == "keyed_flow")
    assert cached_keyed.key_fields == keyed_info.key_fields


def test_machine_work_failure_retry_and_undeclared_event():
    class Input(ava.BaseInput):
        id: int

    calls: list[str] = []

    @ava.step(retries=2)
    async def fails() -> None:
        calls.append("attempt")
        raise ValueError("exhausted")

    @ava.machine(input=Input, key=("id",))
    def failed():
        state = ava.state(fails, initial=True)
        state.on("retry") >> state

    with pytest.raises(ExceptionGroup, match="TaskGroup") as failure:
        failed.test(Input(id=1))
    assert isinstance(failure.value.exceptions[0], ValueError)
    assert calls == ["attempt"] * 3
    calls.clear()

    @ava.step(retries=7, emits=("declared",))
    async def bad_event() -> ava.Event[int]:
        calls.append("attempt")
        return ava.event("typo", 1)

    @ava.machine(input=Input, key=("id",))
    def invalid_event():
        state = ava.state(bad_event, initial=True)
        state.on("declared") >> state

    with pytest.raises(ExceptionGroup) as failure:
        invalid_event.test(Input(id=2))
    assert isinstance(failure.value.exceptions[0], ava.NonRetryableError)
    assert calls == ["attempt"]
    calls.clear()

    @ava.workflow(emits=("declared",))
    def ordinary():
        return bad_event()

    handle = ordinary().run()
    assert isinstance(handle.exception(timeout=5), ava.NonRetryableError)
    assert calls == ["attempt"]
