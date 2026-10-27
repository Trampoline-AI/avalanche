# ClaudeAgent runtime

ClaudeAgent runs Claude through the
[Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python), which
drives the Claude Code CLI. Claude works in a folder, calls your step's tools and
any Claude Code tools you enable, such as `Read` or `Bash`, and answers with JSON
that matches the signature's outputs.

Install it with `uv add 'avalanche-ai[claude]'`, and install the `claude` CLI
wherever the step runs, Ray workers included. The CLI signs in as it does in a
terminal: a `claude` login, `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`,
or `ANTHROPIC_API_KEY`.

## Using it

```python
import avalanche as ava


@ava.agent_step(
    Summarize,
    runtime=ava.runtimes.ClaudeAgent(
        model="claude-haiku-5-5",
        max_turns=10,
        builtin_tools=["Read", "Grep"],
    ),
    tools=[lookup_term],
)
async def summarize(document: str, *, agent: ava.Agent) -> str:
    result = await agent(document=document)
    return result.outputs.summary
```

`Summarize` and `lookup_term` are defined on the
[agent runtimes page](../agent-runtime-contract.md#calling-an-agent-step).

## Settings

```python
PermissionMode = Literal["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk"]


class ClaudeAgent:
    model: str | None = None                  # None: the CLI chooses
    max_turns: int | None = None              # most turns before the session stops
    builtin_tools: Sequence[str] = ()         # Claude Code tools usable without asking, e.g. "Read"
    permission_mode: PermissionMode = "default"
    system_prompt_append: str | None = None   # added after the signature's instructions
    max_budget_usd: float | None = None       # most US dollars one call may spend
```

Each call ignores your own Claude setup: your user and project settings are not
loaded, and MCP servers and connectors from your Claude account are not offered.
Claude sees only the step's tools and skills, and the `builtin_tools` you list.
The Claude CLI's own bundled skills are hidden.

## Trace example

The `Summarize` call above. Claude looked up a term, then answered.

```json
{
  "schema_version": 2,
  "header": {
    "runtime_kind": "claude_agent",
    "trace_format": "claude_agent.messages/v1",
    "status": "succeeded",
    "stop_reason": "success",
    "model": "claude-haiku-5-5",
    "duration_ms": 5400,
    "usage": {
      "input_tokens": 1200,
      "output_tokens": 80,
      "cache_read_input_tokens": 900,
      "cache_creation_input_tokens": 0,
      "cost_usd": 0.0042
    },
    "turn_count": 2,
    "tool_call_count": 1,
    "external_run_id": "9f2c-session",
    "session_id": "9f2c-session",
    "native": {
      "format": "claude_agent.messages/v1",
      "result_subtype": "success",
      "num_turns": 3,
      "duration_api_ms": 4100,
      "permission_denials": 0,
      "errors": []
    }
  },
  "turns": [
    {
      "index": 1,
      "message": "Let me check the glossary.",
      "reasoning": null,
      "tool_calls": [
        {
          "call_id": "toolu_01",
          "caller": "model",
          "name": "lookup_term",
          "arguments": {"term": "DAG"},
          "result": "A data-flow graph.",
          "error": null,
          "duration_ms": null
        }
      ],
      "duration_ms": null,
      "usage": {
        "input_tokens": 600,
        "output_tokens": 40,
        "cache_read_input_tokens": 450,
        "cache_creation_input_tokens": 0,
        "cost_usd": null
      },
      "native": {
        "format": "claude_agent.messages/v1",
        "message_id": "msg_01",
        "model": "claude-haiku-5-5",
        "stop_reason": "tool_use",
        "api_error": null
      }
    },
    {
      "index": 2,
      "message": "{\"summary\": \"Avalanche runs DAGs.\", \"word_count\": 3}",
      "reasoning": null,
      "tool_calls": [],
      "duration_ms": null,
      "usage": {
        "input_tokens": 600,
        "output_tokens": 40,
        "cache_read_input_tokens": 450,
        "cache_creation_input_tokens": 0,
        "cost_usd": null
      },
      "native": {
        "format": "claude_agent.messages/v1",
        "message_id": "msg_02",
        "model": "claude-haiku-5-5",
        "stop_reason": "end_turn",
        "api_error": null
      }
    }
  ],
  "turns_streamed": false
}
```

## Native trace header

The trace header's `native` part.

```python
class ClaudeAgentTraceHeader:
    format: Literal["claude_agent.messages/v1"]
    result_subtype: str | None     # how the session ended, e.g. "success", "error_max_turns"
    num_turns: int                 # as the CLI counts them, tool-result round trips included
    duration_api_ms: int | None    # time spent waiting on the API
    permission_denials: int        # tool uses Claude was not allowed
    errors: tuple[str, ...]        # why the session failed: the first 8, each cut to 1024 characters
```

## Native turn

Each turn's `native` part: the Claude response behind the turn.

```python
class ClaudeAgentTurn:
    format: Literal["claude_agent.messages/v1"]
    message_id: str | None
    model: str                     # the model that answered
    stop_reason: str | None        # e.g. "tool_use", "end_turn"
    api_error: str | None          # set when the API failed; the turn then counts as failed
```

## Native live events

The `native` part of each live event. Every class also has
`format: Literal["claude_agent.messages/v1"]`. The comment names the `common`
event it is sent with.

```python
ClaudeAgentNativeEvent = (
    Started | SessionInitialized | ToolUsed | ToolReturned
    | MessageFinished | RateLimited | Finished
)


class Started:                     # common: None; the inputs given to Claude
    kind: Literal["started"]
    model: str | None
    inputs: JsonValue


class SessionInitialized:          # common: None
    kind: Literal["session.initialized"]
    session_id: str | None
    model: str | None
    tools: tuple[str, ...]         # every tool Claude can use


class ToolUsed:                    # common: tool.started
    kind: Literal["tool.used"]
    turn_index: int


class ToolReturned:                # common: tool.finished
    kind: Literal["tool.returned"]
    turn_index: int


class MessageFinished:             # common: turn.finished, which holds the turn
    kind: Literal["message.finished"]


class RateLimited:                 # common: None
    kind: Literal["rate_limit"]
    status: str
    limit_type: str | None


class Finished:                    # common: None; the session's result
    kind: Literal["finished"]
    subtype: str
    is_error: bool
    num_turns: int
    structured_output: JsonValue   # Claude's answer; None when output_omitted
    output_omitted: bool           # True when the answer is over 256 KiB
    workspace_outputs: tuple[str, ...]  # ava.Workspace output fields Avalanche fills
    errors: tuple[str, ...]
```

The session starting, with the tools Claude can use:

```json
{
  "kind": "event",
  "schema_version": 2,
  "invocation_id": "3f9a0c7e",
  "sequence": 2,
  "timestamp_ns": 1761480000045000000,
  "runtime_kind": "claude_agent",
  "common": null,
  "native": {
    "format": "claude_agent.messages/v1",
    "kind": "session.initialized",
    "session_id": "9f2c-session",
    "model": "claude-haiku-5-5",
    "tools": ["Read", "Grep", "mcp__avalanche__lookup_term"]
  }
}
```

The session ending, with Claude's answer:

```json
{
  "kind": "event",
  "schema_version": 2,
  "invocation_id": "3f9a0c7e",
  "sequence": 7,
  "timestamp_ns": 1761480005400000000,
  "runtime_kind": "claude_agent",
  "common": null,
  "native": {
    "format": "claude_agent.messages/v1",
    "kind": "finished",
    "subtype": "success",
    "is_error": false,
    "num_turns": 3,
    "structured_output": {"summary": "Avalanche runs DAGs.", "word_count": 3},
    "output_omitted": false,
    "workspace_outputs": [],
    "errors": []
  }
}
```

## How ClaudeAgent fills the shared data

### Request

| `AgentRequest` | Claude |
| --- | --- |
| `signature` instructions | The system prompt, followed by `system_prompt_append`. |
| `signature` outputs | The JSON schema Claude must answer with. |
| `inputs` | The prompt: one instruction line, then the inputs as JSON. |
| `ava.File` inputs | Read where they are, without copies. The prompt gives each file's path, relative when it is inside the working folder. Folders outside it are added to the folders Claude may access. |
| `ava.Workspace` input | The working folder. At most one. Without one, Claude works in an empty temporary folder. |
| `tools` | Offered to Claude as `mcp__avalanche__<name>`. |
| `skills` | Written for this call only into a temporary Claude Code plugin named `avalanche`, removed when the call ends. Claude sees each as `avalanche:<name>` with its description and loads it with the `Skill` tool. When a skill has files and `builtin_tools` does not list `Read`, `Read` is added, and every read outside the plugin folder is denied, in every permission mode. A `Read` you list keeps its usual scope. |
| `invocation_id`, `step_name` | Not sent. |

For `document="Avalanche runs DAGs."`, Claude's prompt is:

```text
Complete the task for these inputs. Files are given as paths; relative paths are relative to the current working directory.

{
  "document": "Avalanche runs DAGs."
}
```

When the signature has `ava.File` outputs, the prompt ends with one more line
naming the folder to write them in (see [Outputs](#outputs)).

A tool that raises does not fail the step. Claude gets the error as the tool's
result, and the trace records it in the call's `error`.

### Live events

| `common.kind` | Claude |
| --- | --- |
| `tool.started` | Claude asked for a tool. |
| `tool.finished` | The tool's result reached Claude. |
| `turn.finished` | Claude finished a response. |

Subagents' own messages are not recorded; their work shows as the result of the
tool call that started them.

### Trace header

| Header field | Claude |
| --- | --- |
| `status` | `"succeeded"` when the session succeeded with a JSON object as its answer; else `"failed"`. |
| `stop_reason` | The session's result subtype, such as `"success"` or `"error_max_turns"`. `"no_structured_output"` or `"structured_output_not_object"` when it succeeded without a usable answer. |
| `model` | The model that answered, else the `model` setting. |
| `usage` | The session's usage, with its total cost. |
| `turn_count` | Number of Claude responses. |
| `tool_call_count` | Number of tool calls. |
| `external_run_id`, `session_id` | The Claude session ID. |

### Turns

One turn per Claude response.

| Turn field | Claude |
| --- | --- |
| `message` | What Claude wrote. |
| `reasoning` | Claude's thinking, when shown. |
| `tool_calls` | Each tool Claude asked for, with `caller: "model"` and `duration_ms: null`. |
| `duration_ms` | `null` |
| `usage` | The response's usage, without cost. |
| `native` | A `ClaudeAgentTurn`. |

Tool arguments and results over 256 KiB are replaced by a marker.

### TokenUsage

| Field | Claude |
| --- | --- |
| `input_tokens`, `output_tokens` | As reported. |
| `cache_read_input_tokens`, `cache_creation_input_tokens` | As reported. |
| `cost_usd` | The session's total cost in the header; `null` in turns. |

### Outputs

Claude's JSON answer, validated against the signature.

| Output type | Claude |
| --- | --- |
| `ava.File`, `list[ava.File]` | Claude writes the files in a fresh folder in the run workspace, `.outputs/<step>/<attempt>/<call>/`, and answers with their paths. Avalanche returns them as `ava.File` references. |
| `ava.Workspace` | Left out of the schema Claude answers with. Avalanche fills it with the working folder's contents at the end. |

Other output types that hold files, such as a model with an `ava.File` field,
are rejected.
