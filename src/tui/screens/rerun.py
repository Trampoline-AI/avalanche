"""Rerun configuration dialog for a selected operator run."""

from __future__ import annotations

from typing import TypeAlias

from textual.app import ComposeResult
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Select, SelectionList

from ..models import RunState, WorkflowInfo, display_name_from_id

RerunSelection: TypeAlias = tuple[tuple[str, ...], str] | None


class RerunScreen(ModalScreen[RerunSelection]):
    """Select restart node slugs and direct rerun scheduling mode."""

    CSS = """
    RerunScreen {
        align: center middle;
    }

    #rerun-dialog {
        width: 64;
        height: 28;
        border: solid $primary;
        background: $surface;
        padding: 1 2;
    }

    #rerun-title {
        text-style: bold;
        margin-bottom: 1;
    }

    #rerun-start {
        height: 1fr;
        margin-bottom: 1;
    }

    #rerun-mode {
        margin-bottom: 1;
    }

    #rerun-actions {
        height: 3;
        align-horizontal: right;
    }

    #rerun-actions Button {
        margin-left: 1;
    }
    """

    def __init__(self, run: RunState, workflow: WorkflowInfo) -> None:
        super().__init__()
        self.run = run
        self.workflow = workflow

    def compose(self) -> ComposeResult:
        options = []
        for node_id in self.workflow.node_ids:
            slug = self.workflow.node_slugs.get(node_id, display_name_from_id(node_id))
            label = self.workflow.display_names.get(node_id, display_name_from_id(node_id))
            rendered = label if label == slug else f"{label} ({slug})"
            options.append((rendered, slug, False))

        with Container(id="rerun-dialog"):
            yield Label(f"Rerun {self.run.run_id}", id="rerun-title")
            yield Label("Restart nodes")
            yield SelectionList[str](*options, id="rerun-start")
            yield Label("Mode")
            yield Select[str](
                (("Autorun downstream", "autorun"), ("Lazy selected only", "lazy")),
                value="autorun",
                allow_blank=False,
                id="rerun-mode",
            )
            with Horizontal(id="rerun-actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Rerun", variant="primary", id="submit")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        if event.button.id != "submit":
            return

        start = tuple(self.query_one("#rerun-start", SelectionList).selected)
        if not start:
            self.notify("Select at least one restart node", severity="warning")
            return
        mode = self.query_one("#rerun-mode", Select).value
        if mode not in {"autorun", "lazy"}:
            return
        self.dismiss((start, mode))
