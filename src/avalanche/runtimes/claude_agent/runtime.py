"""The Claude Agent SDK runtime descriptor.

Building a ``ClaudeAgent`` needs neither the SDK nor the Claude CLI: both are
used only when a step runs, so workflows that declare Claude steps import and
publish their declarations without the ``claude`` extra installed.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Collection, Mapping, Sequence
from types import MappingProxyType
from typing import Any, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, PositiveFloat, PositiveInt, model_validator

from ...agent.contract import AgentEventSink, AgentRequest, AgentRunOutcome
from ...agent.declaration import ClaudeAgentDeclaration
from ...agent.steps import AgentStepExecutionError
from ...agent.trace import RuntimeKind

PermissionMode = Literal["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk"]

# SDK options a step cannot set, because Avalanche needs them for the step's answer
# and working folder, with what to use instead.
_REPLACED_OPTIONS: Mapping[str, str] = MappingProxyType(
    {
        "system_prompt": "the step's instructions are the system prompt; add to it with "
        "system_prompt_append",
        "output_format": "the step signature's outputs define the answer",
        "cwd": "give the step an ava.Workspace input to choose the folder Claude works in",
    }
)


def unknown_sdk_options(names: Collection[str], known: Collection[str]) -> str | None:
    """Why ``names`` are not all Claude Agent SDK options, or ``None`` when they are."""
    unknown = sorted(set(names) - set(known))
    if not unknown:
        return None
    return (
        f"ClaudeAgent got unknown options {unknown}: other than its own settings, "
        "ClaudeAgent takes only fields of claude_agent_sdk.ClaudeAgentOptions"
    )


def _sdk_option_names() -> frozenset[str] | None:
    """The SDK's option names, or ``None`` without the ``claude`` extra."""
    try:
        from claude_agent_sdk import ClaudeAgentOptions
    except ModuleNotFoundError as exc:
        if exc.name != "claude_agent_sdk":
            raise
        return None
    return frozenset(field.name for field in dataclasses.fields(ClaudeAgentOptions))


class ClaudeAgent(BaseModel):
    """Run agent steps with Claude through the Claude Agent SDK.

    ``model`` selects the Claude model, such as ``"claude-haiku-5-5"``; ``None``
    lets the Claude CLI choose. ``builtin_tools`` names Claude Code tools the
    agent may use without asking, such as ``"Read"`` or ``"Bash"``; none are
    enabled by default. The step's own tools are always available. With
    ``narrate`` on, the system prompt asks Claude to note what it is about to do
    before each tool call, and its answer must carry a ``run_summary`` that the
    trace shows on the answering turn.

    Steps run the Claude Code CLI that ships with the Claude Agent SDK package
    (the ``claude`` extra), which must be installed wherever the step runs, Ray
    workers included; a separately installed ``claude`` is used only when the
    package has none. It authenticates as it does in a terminal: with a ``claude``
    login, ``CLAUDE_CODE_OAUTH_TOKEN`` from ``claude setup-token``, or
    ``ANTHROPIC_API_KEY``.

    Any other keyword is a ``claude_agent_sdk.ClaudeAgentOptions`` field passed to
    the SDK, such as ``effort`` or ``mcp_servers``. ``mcp_servers``, ``hooks``,
    ``plugins``, ``allowed_tools``, ``add_dirs``, ``tools`` and ``skills`` are
    combined with what Avalanche sets for the step; any other option replaces
    Avalanche's value. ``system_prompt``, ``output_format`` and ``cwd`` are
    rejected. An unknown name fails here when the SDK is installed, and when the
    step runs otherwise. These options are never published or recorded.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="allow")

    kind: ClassVar[RuntimeKind] = "claude_agent"

    model: str | None = None
    max_turns: PositiveInt | None = None
    builtin_tools: Sequence[str] = ()
    permission_mode: PermissionMode = "default"
    system_prompt_append: str | None = None
    max_budget_usd: PositiveFloat | None = None
    narrate: bool = True

    @model_validator(mode="after")
    def _check_sdk_options(self) -> Self:
        names = self.sdk_options.keys()
        for name in names:
            if name in _REPLACED_OPTIONS:
                raise ValueError(
                    f"ClaudeAgent does not take {name!r}: {_REPLACED_OPTIONS[name]}"
                )
        # Importing the SDK is slow: only a runtime with SDK options pays for it.
        known = _sdk_option_names() if names else None
        if known is not None and (problem := unknown_sdk_options(names, known)):
            raise ValueError(problem)
        return self

    @property
    def sdk_options(self) -> Mapping[str, Any]:
        """The Claude Agent SDK options given by keyword, by name, unchecked."""
        # Values are the SDK's own types, checked by the SDK and the CLI.
        extra = self.__pydantic_extra__
        assert extra is not None, "ClaudeAgent allows extra fields"
        return MappingProxyType(extra)

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
                "narrate": effective.narrate,
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
