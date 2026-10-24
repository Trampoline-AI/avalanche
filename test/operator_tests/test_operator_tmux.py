"""One real terminal path: discover, press run, and retrieve the published result."""

import shlex
import shutil
import socket
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest

from runtime.operator.client import GrpcStateProvider, OperatorCallError
from runtime.operator.models import RunStatus


@pytest.mark.tmux
def test_terminal_runs_discovered_workflow(tmp_path):
    if not shutil.which("tmux"):
        pytest.skip("tmux not installed")
    workflow = tmp_path / "terminal_flow.py"
    workflow.write_text(
        "import avalanche as ava\n"
        '@ava.source\ndef value():\n    return {"answer": 42}\n'
        "@ava.workflow\ndef terminal_flow():\n    return value()\n"
    )
    with socket.socket() as listener:
        listener.bind(("localhost", 0))
        port = listener.getsockname()[1]
    session = f"ava-test-{uuid4().hex}"
    root = Path(__file__).parents[2]

    def tmux(*args):
        return subprocess.run(
            ["tmux", "-L", session, *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            cwd=root,
        ).stdout

    def screen():
        return tmux("capture-pane", "-t", f"{session}:.1", "-p")

    client = GrpcStateProvider(f"localhost:{port}")
    try:
        tmux(
            "new-session",
            "-d",
            "-s",
            session,
            "-x",
            "200",
            "-y",
            "50",
            shlex.join(
                ["uv", "run", "ava", "operator", str(workflow), "--port", str(port), "--no-web"]
            ),
        )
        deadline = time.monotonic() + 20
        while True:
            try:
                if client.list_workflows():
                    break
            except OperatorCallError:
                pass
            assert time.monotonic() < deadline, "Operator did not become ready"
            time.sleep(0.1)
        tmux(
            "split-window",
            "-h",
            "-t",
            session,
            shlex.join(["uv", "run", "ava", "tui", "--connect", f"localhost:{port}"]),
        )
        deadline = time.monotonic() + 20
        while "DAG" not in screen():
            assert time.monotonic() < deadline, screen()
            time.sleep(0.1)
        if "EXPLORER" not in screen():
            tmux("send-keys", "-t", f"{session}:.1", "Space", "e")
        while "terminal_flow" not in screen():
            assert time.monotonic() < deadline, screen()
            time.sleep(0.1)
        tmux("send-keys", "-t", f"{session}:.1", "Escape")
        deadline = time.monotonic() + 20
        # Extension discovery is asynchronous. Once ready, run now opens the
        # schema preview/confirmation even when this workflow takes no input.
        while "Preview validated input" not in screen():
            assert time.monotonic() < deadline, screen()
            tmux("send-keys", "-t", f"{session}:.1", "r")
            time.sleep(0.2)
        tmux("send-keys", "-t", f"{session}:.1", "Tab")
        time.sleep(0.1)
        tmux("send-keys", "-t", f"{session}:.1", "Enter")
        while "{}" not in screen():
            assert time.monotonic() < deadline, screen()
            time.sleep(0.1)
        tmux("send-keys", "-t", f"{session}:.1", "Tab")
        time.sleep(0.1)
        tmux("send-keys", "-t", f"{session}:.1", "Enter")
        deadline = time.monotonic() + 20
        while True:
            runs = client.list_runs("terminal_flow")
            if runs and runs[0].status in {RunStatus.SUCCESS, RunStatus.FAILED}:
                break
            assert time.monotonic() < deadline, screen()
            time.sleep(0.1)
        assert runs[0].status == RunStatus.SUCCESS, screen()
        assert client.get_run_result(runs[0].run_id) == {"answer": 42}
    finally:
        client.close()
        subprocess.run(
            ["tmux", "-L", session, "kill-server"],
            capture_output=True,
            timeout=10,
        )


@pytest.mark.asyncio
async def test_operator_tui_extensions_live_pilot(tmp_path):
    """Real CLI daemon, real gRPC provider and Pilot; no mocked transport/data."""
    import asyncio
    import hashlib
    import json
    import os
    import sys
    from dataclasses import replace
    from textwrap import dedent

    from tui.app import AvalancheApp
    from tui.models import StreamLogEntry
    from tui.screens.input_form import InputForm
    from tui.state import TuiContext, TuiExtensionError
    from tui.widgets.log_panel import LogWidget

    evidence = Path(os.environ.get("AVALANCHE_TUI_EVIDENCE", str(tmp_path / "evidence")))
    evidence.mkdir(parents=True, exist_ok=True)
    assertions = []
    gate = tmp_path / "release-step"
    artifact = tmp_path / "artifact.bin"
    workflow = tmp_path / "pilot_flow.py"
    workflow.write_text(
        dedent(f"""
        import logging
        import sys
        import time
        from pathlib import Path
        import avalanche as ava

        class Input(ava.BaseInput):
            count: int

        @ava.source
        def produce(payload: Input):
            print("live-stdout-sentinel", flush=True)
            print("live-stderr-sentinel", file=sys.stderr, flush=True)
            logging.getLogger("proof").warning("live-logging-sentinel")
            print("x" * 20000, flush=True)
            while not Path({str(gate)!r}).exists():
                time.sleep(0.05)
            return payload.count

        @ava.step
        def publish(count: int):
            path = Path({str(artifact)!r})
            path.write_bytes(b"operator-artifact:" + str(count).encode())
            file = ava.File.from_path(path)
            return {{"file": file, "workspace": ava.Workspace.from_directory(
                path.parent, reference=file.ref.workspace)}}

        @ava.workflow(input=Input)
        def typed():
            return publish(produce())

        @ava.source
        def long_step():
            print("cancel-ready-sentinel", flush=True)
            time.sleep(60)
            return None

        @ava.workflow
        def cancellable():
            return long_step()

        @ava.workflow(cron="0 0 1 1 *")
        def scheduled():
            return None
    """)
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    root = Path(__file__).parents[2]
    output = (evidence / "operator.log").open("w")
    command = [
        sys.executable,
        "-m",
        "ava_cli",
        "operator",
        str(workflow),
        "--port",
        str(port),
        "--webhook-port",
        "0",
        "--no-web",
    ]

    def launch():
        return subprocess.Popen(command, cwd=root, stdout=output, stderr=subprocess.STDOUT)

    process = launch()
    provider = GrpcStateProvider(f"127.0.0.1:{port}", unary_timeout=2)
    observer = GrpcStateProvider(f"127.0.0.1:{port}", unary_timeout=2)
    unary_calls = []
    real_call = provider._call

    def record_call(fn, *args, **kwargs):
        unary_calls.append(fn._method.decode())
        return real_call(fn, *args, **kwargs)

    provider._call = record_call
    notices, callbacks = [], []
    provider.on_stream_reset(notices.append)
    provider.on_run_update(lambda run: callbacks.append((run.operator_instance_id, run.run_id)))
    try:
        deadline = time.monotonic() + 25
        while True:
            try:
                flows = await asyncio.to_thread(observer.list_workflows)
                if flows:
                    break
            except OperatorCallError:
                pass
            assert process.poll() is None, (evidence / "operator.log").read_text()
            assert time.monotonic() < deadline, "Operator readiness timed out"
            await asyncio.sleep(0.1)
        flow = next(flow for flow in flows if flow.name == "typed")
        assert json.loads(flow.input_schema_json)["properties"]["count"]["type"] == "integer"
        assert flow.version
        assertions.append("catalog_input_schema_and_version")
        app = AvalancheApp(provider=provider, workflow=flow.selector)

        async def wait_for(pilot, predicate, seconds=25):
            deadline = time.monotonic() + seconds
            while not predicate():
                assert time.monotonic() < deadline, (
                    provider.stream_state,
                    provider.stream_error,
                    app.extension_session.error if app.extension_session else "no session",
                )
                await pilot.pause(0.05)

        async with app.run_test(size=(160, 50)) as pilot:
            await wait_for(
                pilot,
                lambda: app.extension_session.description is not None
                and app.extension_session.context == app.extension_context,
            )
            assert app.extension_context.version == flow.version
            await pilot.press("r")
            assert isinstance(app.screen, InputForm)
            app.screen.query_one("#field-0").value = "7"
            await pilot.click("#form-validate")
            assert not app.screen.query_one("#form-submit").disabled, (
                app.screen.query_one("#form-preview").render(),
                app.screen.query_one("#field-0").value,
            )
            await pilot.click("#form-submit")
            await wait_for(
                pilot,
                lambda: any(log.message == "live-stderr-sentinel" for log in app.store.logs),
            )
            run = app.store.current_run
            assert run is not None and run.status == RunStatus.RUNNING
            assert any(node.status.value == "running" for node in run.nodes.values())
            assert not gate.exists()
            live = [log for log in app.store.logs if "live-" in log.message]
            assert {log.channel for log in live if isinstance(log, StreamLogEntry)} == {
                "stdout",
                "stderr",
                "log",
            }
            assert len({log.attempt for log in live}) == 1
            assert all(log.attempt and log.node_id == "produce_1" for log in live)
            await wait_for(
                pilot, lambda: any("record truncated" in log.message for log in app.store.logs)
            )
            assertions.extend(
                [
                    "typed_start",
                    "live_dag_states",
                    "stdout_stderr_logging_before_step_exit",
                    "real_node_attempt_channel_identity",
                    "oversized_record_marker",
                ]
            )
            await pilot.press("s")
            assert not app._log_autoscroll
            await pilot.press("/", *"live-stderr-sentinel", "enter")
            assert app.store.match_count == 1
            await pilot.press("escape", "f")
            app.screen.query_one("#filter-node").value = "produce_1"
            app.screen.query_one("#filter-level").value = "ERROR"
            app.screen.query_one("#filter-channel").value = "stderr"
            app.screen.query_one("#filter-attempt").value = live[0].attempt
            await pilot.click("#filter-apply")
            widget = app.screen.query_one("#log-content", LogWidget)
            assert len(widget._rows) == 1
            assert widget._rows[0][1].channel == "stderr"
            await pilot.press("s")
            assert app._log_autoscroll
            assertions.append("pilot_log_follow_search_channel_attempt_filters")
            await pilot.pause(0.5)
            idle_calls = len(unary_calls)
            assert provider.ping() is True
            await pilot.pause(2.2)
            assert len(unary_calls) == idle_calls
            assertions.append("idle_no_unary_polling_cached_ping")
            app.save_screenshot(str(evidence / "live.svg"))
            gate.touch()
            await wait_for(pilot, lambda: app.store.current_run.status == RunStatus.SUCCESS)
            run_id = app.store.current_run.run_id
            context = replace(app.extension_context, run=run_id)
            extension = provider.tui_extensions
            page = await asyncio.to_thread(
                extension.read, "artifacts", context, limit=1, byte_limit=4096
            )
            assert len(page.items) == 1 and page.next_cursor is not None
            workspace_page = await asyncio.to_thread(
                extension.read,
                "artifacts",
                context,
                cursor=page.next_cursor,
                limit=1,
                byte_limit=4096,
            )
            assert workspace_page.revision == page.revision
            assert workspace_page.next_cursor is None
            assert workspace_page.items[0]["kind"] == "workspace"
            assert workspace_page.items[0]["downloadable"] is False
            assert "content" not in workspace_page.items[0]
            assertions.append("paged_file_and_workspace_descriptors_no_inline_bodies")
            assert "content" not in page.items[0]
            destination = tmp_path / "download.bin"
            result = await asyncio.to_thread(
                extension.invoke,
                "artifact.download",
                context,
                {"artifact_id": page.items[0]["id"], "destination": str(destination)},
                intent_id="download-proof",
            )
            expected = hashlib.sha256(b"operator-artifact:7").hexdigest()
            assert result.status == "completed"
            assert (
                hashlib.sha256(destination.read_bytes()).hexdigest()
                == expected
                == page.items[0]["sha256"]
            )
            assertions.append("explicit_artifact_download_sha256")
            with pytest.raises(TuiExtensionError):
                await asyncio.to_thread(
                    extension.invoke,
                    "artifact.download",
                    context,
                    {"artifact_id": page.items[0]["id"], "destination": str(destination)},
                    intent_id="no-overwrite-proof",
                )
            assert hashlib.sha256(destination.read_bytes()).hexdigest() == expected
            await wait_for(
                pilot,
                lambda: app.extension_session.description is not None
                and any(
                    action.id == "artifact.download"
                    for action in app.extension_session.description.actions
                ),
            )
            download = next(
                action
                for action in app.extension_session.description.actions
                if action.id == "artifact.download"
            )
            app.invoke_extension_action(download)
            await pilot.pause()
            assert isinstance(app.screen, InputForm)
            pilot_destination = tmp_path / "pilot-download.bin"
            app.screen.query_one("#field-0").value = json.dumps(page.items[0]["id"])
            app.screen.query_one("#field-1").value = json.dumps(str(pilot_destination))
            await pilot.click("#form-validate")
            assert not app.screen.query_one("#form-submit").disabled
            assert not pilot_destination.exists()
            await pilot.click("#form-submit")
            await wait_for(pilot, pilot_destination.exists)
            assert hashlib.sha256(pilot_destination.read_bytes()).hexdigest() == expected
            assertions.append("pilot_explicit_download_confirmation_and_no_overwrite")
            schedules = await asyncio.to_thread(extension.read, "schedules", context, limit=1)
            assert schedules.items[0]["cron"] == "0 0 1 1 *"
            assert schedules.items[0]["control"] == "inspection_only"
            assert not any(
                action.id.startswith("schedule.")
                for action in extension.describe(context).actions
            )
            assertions.append("real_schedule_inspection_no_fake_toggle")
            from textual.widgets import DataTable

            panel = next(
                panel
                for panel in app.extension_session.description.panels
                if panel.resource == "schedules"
            )
            app.open_extension_panel(panel)
            await pilot.pause()
            await wait_for(
                pilot, lambda: app.screen.query_one("#resource-body", DataTable).row_count == 1
            )
            app.save_screenshot(str(evidence / "schedules.svg"))
            await pilot.press("escape")
            assertions.append("pilot_real_schedule_resource_panel")
            # Seed a second real history row, then prove the first immutable cut
            # remains pinned while the new run changes state.
            first = await asyncio.to_thread(extension.read, "runs", TuiContext(()), limit=1)
            slow = next(flow for flow in flows if flow.name == "cancellable")
            slow_id = await asyncio.to_thread(observer.start_run, slow.selector)
            await wait_for(pilot, lambda: any(item[1] == slow_id for item in callbacks))
            # The original history cut is complete and does not include the later run.
            assert first.items[0]["id"] == run_id
            all_page = await asyncio.to_thread(extension.read, "runs", TuiContext(()), limit=1)
            assert all_page.next_cursor
            second = await asyncio.to_thread(
                extension.read, "runs", TuiContext(()), cursor=all_page.next_cursor, limit=1
            )
            assert all_page.revision == second.revision
            assert {all_page.items[0]["id"], second.items[0]["id"]} == {run_id, slow_id}
            with pytest.raises(TuiExtensionError, match="cursor_expired"):
                await asyncio.to_thread(
                    extension.read,
                    "runs",
                    TuiContext((), flow=flow.selector),
                    cursor=all_page.next_cursor,
                )
            assertions.append("server_paged_history_stable_revision_and_scope_bound_cursor")
            slow_context = TuiContext((), flow=slow.selector, version=slow.version, run=slow_id)
            # Inspect actual running state before requesting cancellation.
            deadline = time.monotonic() + 20
            while True:
                current = await asyncio.to_thread(observer.get_run, slow_id)
                if current.status == RunStatus.RUNNING:
                    break
                assert time.monotonic() < deadline
                await pilot.pause(0.05)
            cancellation = await asyncio.to_thread(
                extension.invoke, "run.cancel", slow_context, {}, intent_id="cancel-proof"
            )
            assert cancellation.status == "accepted"
            deadline = time.monotonic() + 20
            while (
                await asyncio.to_thread(observer.get_run, slow_id)
            ).status != RunStatus.CANCELLED:
                assert time.monotonic() < deadline
                await pilot.pause(0.05)
            assertions.append("cancel_long_step_accepted_then_terminal")
            old_epoch = provider.operator_instance_id
            await asyncio.to_thread(process.terminate)
            await asyncio.to_thread(process.wait, 15)
            await wait_for(pilot, lambda: not provider.operator_reachable)
            assert provider.ping() is False
            process = launch()
            await wait_for(
                pilot,
                lambda: provider.operator_instance_id != old_epoch
                and provider.stream_state.value == "live",
                seconds=45,
            )
            assert notices
            assert app.store.current_run is None
            cut = len(callbacks)
            await pilot.pause(0.3)
            assert all(epoch != old_epoch for epoch, _ in callbacks[cut:])
            assertions.append("restart_cached_disconnect_exact_reset_ack_no_stale_callbacks")
            app.save_screenshot(str(evidence / "reconnected.svg"))
        readonly = GrpcStateProvider(f"127.0.0.1:{port}", read_only=True)
        try:
            ro_app = AvalancheApp(provider=readonly, workflow=flow.selector, read_only=True)
            async with ro_app.run_test(size=(160, 50)) as pilot:
                await wait_for(pilot, lambda: ro_app.extension_session.description is not None)
                assert not ro_app.can_start_run()
                await pilot.press("r")
                assert not isinstance(ro_app.screen, InputForm)
                ro_context = ro_app.extension_context
                assert not next(
                    action
                    for action in readonly.tui_extensions.describe(ro_context).actions
                    if action.id == "run.start"
                ).available
                with pytest.raises(TuiExtensionError) as blocked:
                    readonly.start_run(flow.selector, input={"count": 1})
                assert blocked.value.code == "read_only"
                with pytest.raises(TuiExtensionError):
                    readonly.tui_extensions.invoke(
                        "run.start", ro_context, {"count": 1}, intent_id="readonly"
                    )
                assertions.append("pilot_and_direct_provider_read_only_fences")
        finally:
            readonly.close()
    finally:
        provider.close()
        observer.close()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=15)
        output.close()
        (evidence / "assertions.json").write_text(
            json.dumps(
                {
                    "passed": assertions,
                    "NOT_RUN": {
                        "multi_iteration_scripted_lm_trace": (
                            "No scripted-LM example exists under examples/. "
                            "The inline operator predictor fixture emits "
                            "a zero-iteration trace, not a multi-iteration LM."
                        )
                    },
                    "operator_command": command,
                    "owned_process_exit": process.returncode,
                },
                indent=2,
            )
        )
