"""TitleBar widget — workflow name + action hints at top of right pane."""

from __future__ import annotations

from rich.style import Style
from rich.text import Text
from textual.widgets import Static

from ..theme import ICE_FROST, ICE_STEEL, ICE_TEAL


class TitleBar(Static):
    """Shows 'Workflow: name  [Run]' at top of right pane."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._test_store = None

    def render(self) -> Text:
        store = self._test_store or self.app.store
        flow_name = store.current_workflow.name if store.current_workflow else ""
        text = Text()
        text.append("  Workflow: ", Style(color=ICE_STEEL))
        text.append(flow_name or "—", Style(color=ICE_FROST, bold=True))
        text.append("  ")
        text.append("[r] Run", Style(color=ICE_TEAL))
        return text
