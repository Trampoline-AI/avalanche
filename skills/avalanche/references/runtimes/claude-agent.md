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
    max_turns: PositiveInt | None = None      # most turns before the session stops
    builtin_tools: Sequence[str] = ()         # Claude Code tools usable without asking, e.g. "Read"
    permission_mode: PermissionMode = "default"
    system_prompt_append: str | None = None   # added after the signature's instructions
    max_budget_usd: PositiveFloat | None = None  # most US dollars one call may spend
```

Each call ignores your own Claude setup: your user and project settings are not
loaded, and MCP servers and connectors from your Claude account are not offered.
Claude sees only the step's tools and skills, the `builtin_tools` you list, and
what you add through [SDK options](#claude-agent-sdk-options). The Claude CLI's
own bundled skills are hidden.

### Claude Agent SDK options

Any other keyword is a field of the SDK's
[`ClaudeAgentOptions`](https://github.com/anthropics/claude-agent-sdk-python),
passed to the SDK as you give it, with the SDK's own types:

```python
from claude_agent_sdk import HookMatcher


async def check_command(hook_input, tool_use_id, context):
    return {}  # an empty answer lets the command run


runtime = ava.runtimes.ClaudeAgent(
    model="claude-sonnet-5-5",
    effort="low",
    fallback_model="claude-haiku-5-5",
    mcp_servers={"docs": {"type": "http", "url": "https://docs.example.com/mcp"}},
    allowed_tools=["mcp__docs__search"],
    hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[check_command])]},
)
```

Avalanche checks the option's name, not its value: the SDK and the CLI check
values, and using an option correctly is up to you. An unknown name raises
`ValueError` when you build the runtime if the `claude` extra is installed there,
and `AgentStepError` naming the option when the step runs otherwise.

Avalanche fills some options for every call. How yours combine with them:

| SDK option | With yours |
| --- | --- |
| `mcp_servers` | Your servers are added next to Avalanche's `avalanche` server, which carries the step's tools. It must be a dict: a configuration file path raises `AgentStepError`, and so does a server of yours named `avalanche`. |
| `hooks` | Your matchers are added after Avalanche's, event by event. Avalanche's only hook keeps `Read` inside the skill folder when step skills have files. |
| `plugins` | Your plugins are added next to the temporary `avalanche` plugin that carries the step's skills. |
| `allowed_tools`, `add_dirs` | Your entries are added after Avalanche's, without repeats. |
| `tools` | A list is added after the `builtin_tools` and the skill tools. A preset or `None` replaces them. |
| `skills` | A list is added after the step's skills. `"all"` replaces them. |
| `system_prompt` | Rejected with `ValueError`: the step's instructions are the system prompt. Use `system_prompt_append`. |
| `output_format` | Rejected with `ValueError`: the signature's outputs define Claude's answer. |
| `cwd` | Rejected with `ValueError`: give the step an `ava.Workspace` input to choose the folder Claude works in. |
| Any other option | Yours replaces Avalanche's value. |

Replacing `setting_sources` or `strict_mcp_config` gives up the isolation
described above: your Claude settings, or the MCP servers and connectors of your
Claude account, reach the step.

A step's options replace the workflow default's option by option: a step's
`mcp_servers` replaces the default's `mcp_servers` whole.

SDK options are never published or recorded: they are not part of the step's
declaration, its live events or its trace, so `env` values and MCP server
credentials stay in the process that runs the step. With `RayExecutor`, the
runtime travels to the worker, so every option value must pickle with
`cloudpickle`. Hooks and in-process MCP servers from `create_sdk_mcp_server` do,
but they run as copies on the worker: state they change stays there. Prefer the
step's `tools=` for Python tools.

## Trace example

The `Summarize` call above, as `result.trace`. Claude looked up a term, then
answered through the `StructuredOutput` tool.

```json
{
  "header": {
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
    "tool_call_count": 2,
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
          "name": "mcp__avalanche__lookup_term",
          "arguments": {"term": "DAG"},
          "result": [{"type": "text", "text": "\"A data-flow graph.\""}],
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
      "message": null,
      "reasoning": null,
      "tool_calls": [
        {
          "call_id": "toolu_02",
          "caller": "model",
          "name": "StructuredOutput",
          "arguments": {"summary": "Avalanche runs DAGs.", "word_count": 3},
          "result": "Structured output provided successfully",
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
        "message_id": "msg_02",
        "model": "claude-haiku-5-5",
        "stop_reason": "tool_use",
        "api_error": null
      }
    }
  ]
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
    errors: tuple[str, ...]        # why the session failed: the first 8 listed errors, each cut to
                                   # 1024 characters, else the result text cut to 4096
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

The `native` part of a live event, for what only Claude reports. Every class also
has `format: Literal["claude_agent.messages/v1"]`. Both are sent with
`common: None`; every other ClaudeAgent event is a shared event alone, with
`native: None`.

```python
ClaudeAgentNativeEvent = SessionInitialized | RateLimited


class SessionInitialized:          # the CLI's report of the session it started
    kind: Literal["session.initialized"]
    session_id: str | None
    model: str | None
    tools: tuple[str, ...]         # every tool Claude can use


class RateLimited:
    kind: Literal["rate_limit"]
    status: str
    limit_type: str | None
```

The session starting, with the tools Claude can use:

```json
{
  "kind": "event",
  "schema_version": 2,
  "invocation_id": "3f9a0c7e",
  "sequence": 2,
  "timestamp_ns": 1761480000045000000,
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
| `run.started` | Sent before the CLI starts. `model` is the `model` setting, `None` when the CLI chooses; `inputs` are the step's inputs, files and workspaces by reference. |
| `tool.started` | Claude asked for a tool. `turn` is the turn that asked. |
| `tool.finished` | The tool's result reached Claude. `turn` is the turn that asked; `error` is set when the tool failed. |
| `turn.finished` | Claude finished a response. |

The `Summarize` call above sends, in order: `run.started`,
`session.initialized`, then `tool.started`, `tool.finished` and `turn.finished`
for each of its two turns (the second for `StructuredOutput`), then the final
event.

Subagents' own messages are not recorded; their work shows as the result of the
tool call that started them.

### Final event

Sent once the session has ended and its outputs, if any, are built.

| Final event | Claude |
| --- | --- |
| `trace_finished` | The session returned a result. `outputs` is Claude's JSON answer, with `ava.File` outputs as file references and `ava.Workspace` outputs by reference (see [file and workspace values](../agent-runtime-contract.md#file-and-workspace-values)). `outputs` is `None` when the header's `status` is `"failed"`, or when turning the answer into outputs failed, for example a file path outside the output folder; the step then raises. |
| `trace_unavailable` | The call was cancelled, the Claude CLI is not installed, the SDK raised, or the CLI ended without a result. |

The final event of the call above:

```json
{
  "kind": "trace_finished",
  "schema_version": 2,
  "invocation_id": "3f9a0c7e",
  "header": { "...": "as in the trace example" },
  "outputs": {"summary": "Avalanche runs DAGs.", "word_count": 3},
  "omitted_outputs": [],
  "complete": true
}
```

### Trace header

| Header field | Claude |
| --- | --- |
| `status` | `"succeeded"` when the session succeeded with a JSON object as its answer; else `"failed"`. |
| `stop_reason` | The session's result subtype, such as `"success"` or `"error_max_turns"`. `"no_structured_output"` or `"structured_output_not_object"` when it succeeded without a usable answer. |
| `model` | The model that answered, else the `model` setting. |
| `usage` | The session's usage, with its total cost. |
| `turn_count` | Number of Claude responses. |
| `tool_call_count` | Number of tool calls, Claude's `StructuredOutput` answer included. |
| `external_run_id`, `session_id` | The Claude session ID. |

### Turns

One turn per Claude response.

| Turn field | Claude |
| --- | --- |
| `message` | What Claude wrote. |
| `reasoning` | Claude's thinking, when shown. |
| `tool_calls` | Each tool Claude asked for, with `caller: "model"` and `duration_ms: null`. Step tools are named `mcp__avalanche__<name>`, and their `result` is the MCP content Claude got: text blocks holding the tool's return value as JSON. Claude's answer is a `StructuredOutput` call whose `arguments` are the outputs. |
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
