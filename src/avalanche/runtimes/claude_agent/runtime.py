"""The Claude Agent SDK runtime descriptor.

Building a ``ClaudeAgent`` needs neither the SDK nor the Claude CLI: both are
used only when a step runs, so workflows that declare Claude steps import and
publish their declarations without the ``claude`` extra installed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, PositiveFloat, PositiveInt

from ...agent.contract import AgentEventSink, AgentRequest, AgentRunOutcome
from ...agent.declaration import ClaudeAgentDeclaration
from ...agent.steps import AgentStepExecutionError
from ...agent.trace import RuntimeKind

PermissionMode = Literal["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk"]


class ClaudeAgent(BaseModel):
    """Run agent steps with Claude through the Claude Agent SDK.

    ``model`` selects the Claude model, such as ``"claude-haiku-5-5"``; ``None``
    lets the Claude CLI choose. ``builtin_tools`` names Claude Code tools the
    agent may use without asking, such as ``"Read"`` or ``"Bash"``; none are
    enabled by default. The step's own tools are always available.

    Steps run the ``claude`` CLI, which must be installed wherever the step runs,
    Ray workers included. It authenticates as it does in a terminal: with a
    ``claude`` login, ``CLAUDE_CODE_OAUTH_TOKEN`` from ``claude setup-token``, or
    ``ANTHROPIC_API_KEY``.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    kind: ClassVar[RuntimeKind] = "claude_agent"

    model: str | None = None
    max_turns: PositiveInt | None = None
    builtin_tools: Sequence[str] = ()
    permission_mode: PermissionMode = "default"
    system_prompt_append: str | None = None
    max_budget_usd: PositiveFloat | None = None

    def merged_over(self, base: ClaudeAgent) -> ClaudeAgent:
        """This runtime with the fields it leaves unset taken from ``base``."""
        fields = {name: getattr(base, name) for name in base.model_fields_set}
        fields.update({name: getattr(self, name) for name in self.model_fields_set})
        return ClaudeAgent.model_validate(fields)

    def declaration(self, *, workflow_default: ClaudeAgent | None) -> ClaudeAgentDeclaration:
        """Describe this step runtime merged over the workflow default, statically."""
        effective = self if workflow_default is None else self.merged_over(workflow_default)
        return ClaudeAgentDeclaration(
            model=effective.model,
            settings={
                "max_turns": effective.max_turns,
                "permission_mode": effective.permission_mode,
                "system_prompt_append": effective.system_prompt_append,
                "max_budget_usd": effective.max_budget_usd,
            },
            builtin_tools=tuple(effective.builtin_tools),
        )

    async def invoke(self, request: AgentRequest, events: AgentEventSink) -> AgentRunOutcome:
        try:
            from . import _sdk
        except ModuleNotFoundError as exc:
            if exc.name != "claude_agent_sdk":
                raise
            raise AgentStepExecutionError(
                f"agent step {request.step_name!r} runs on ava.runtimes.ClaudeAgent, which "
                "needs the Claude Agent SDK: install avalanche-ai[claude]"
            ) from exc
        return await _sdk.run(self, request, events)
