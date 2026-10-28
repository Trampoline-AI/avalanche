from __future__ import annotations

from ava_cli import app
from ava_cli.runs_view import format_duration
from runtime.operator.models import RunState, RunStatus


class FakeProvider:
    def __init__(self, runs: list[RunState]) -> None:
        self.runs = runs
        self.cancelled: list[str] = []
        self.closed = False

    def list_runs(self, workflow_selector: str) -> list[RunState]:
        return self.runs

    def cancel_run(self, run_id: str) -> None:
        self.cancelled.append(run_id)

    def close(self) -> None:
        self.closed = True


def test_format_duration():
    assert format_duration(59) == "59s"
    assert format_duration(61) == "1m01s"
    assert format_duration(3600) == "1h00m"


def test_runs_list_prints_a_table(monkeypatch, capsys):
    runs = [
        RunState(
            run_id="r1",
            flow_name="flow",
            status=RunStatus.SUCCESS,
            started_at=1700000000.0,
            ended_at=1700000065.0,
        )
    ]
    monkeypatch.setattr(app, "_make_provider", lambda connect: FakeProvider(runs))

    assert app.main(["runs", "list", "flow"]) == 0
    assert capsys.readouterr().out == (
        "RUN ID  WORKFLOW  STATUS   STARTED           DURATION\n"
        "r1      flow      success  2023-11-14 22:13  1m05s\n"
    )


def test_runs_cancel_all_running(monkeypatch, capsys):
    provider = FakeProvider([RunState(run_id="r1", flow_name="flow", status=RunStatus.RUNNING)])
    monkeypatch.setattr(app, "_make_provider", lambda connect: provider)

    assert app.main(["runs", "cancel", "--all-running", "flow"]) == 0
    assert provider.cancelled == ["r1"]
    assert "cancelled 1 run(s)" in capsys.readouterr().out
