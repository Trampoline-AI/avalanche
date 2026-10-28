# OpenAIAgent runtime

OpenAIAgent runs OpenAI models through the
[OpenAI Agents SDK](https://github.com/openai/openai-agents-python). The model
calls your step's tools, reads the files you pass as attachments, and answers
with JSON that matches the signature's outputs. It has no file system and no
tools of its own.

Install it with `uv add 'avalanche-ai[openai]'` wherever the step runs, Ray
workers included. Each call creates its own OpenAI client, which reads
`OPENAI_API_KEY`, and `OPENAI_BASE_URL`, `OPENAI_ORG_ID` and `OPENAI_PROJECT_ID`
when set, from the environment of the process that runs the step. Client
settings made in code with the SDK's `set_default_openai_key` or
`set_default_openai_client` are not used.

## Using it

```python
import avalanche as ava


@ava.agent_step(
    Summarize,
    runtime=ava.runtimes.OpenAIAgent(model="gpt-5.4-mini", max_turns=6),
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
class OpenAIAgent:
    model: str | None = None                  # None: OPENAI_DEFAULT_MODEL, else the SDK's default
    max_turns: PositiveInt | None = None      # most model responses one call may take; None: 10
    system_prompt_append: str | None = None   # added after the signature's instructions
    narrate: bool = True                      # ask the model to say what it is doing (see below)
    tracing: bool = False                     # send the SDK's own trace of each call to OpenAI
```

With `narrate` on, Avalanche adds this paragraph to the agent's instructions,
after the signature's instructions and before `system_prompt_append`:

```text
While you work, keep a short running account of what you are doing. Before each
tool call, write one or two plain sentences saying what you are about to do and
why. When a result changes your plan, say how. When you submit your answer, put a
brief summary of what you did and what you found in run_summary. Keep these notes
short: they are a progress log for the people watching this run, not the answer.
```

The notes before tool calls are an instruction, so the model may skip some. The
summary is not: the answer the model must give gets a required `run_summary`
string field, so every successful call ends with one. Avalanche removes it from
the answer before the outputs are built and shows it as the answering turn's
`message`. A signature output named `run_summary` raises `AgentStepError` unless
`narrate=False`. Set `narrate=False` to leave out both the paragraph and the
field.

The SDK sends a trace of every run to OpenAI unless told not to. Avalanche turns
that off; set `tracing=True` to keep it, and the trace header's
`external_run_id` then names the SDK trace.

### Model settings

Any other keyword is a field of the SDK's `agents.ModelSettings`, passed to the
SDK as you give it, with the SDK's own types:

```python
from openai.types.shared import Reasoning

runtime = ava.runtimes.OpenAIAgent(
    model="gpt-5.4-mini",
    reasoning=Reasoning(effort="low", summary="auto"),
    max_tokens=2000,
)
```

Avalanche checks the setting's name, not its value: the SDK and the API check
values. An unknown name raises `ValueError` when you build the runtime if the
`openai` extra is installed there, and `AgentStepError` naming the setting when
the step runs otherwise. A step's settings replace the workflow default's
setting by setting.

Model settings are never published or recorded: they are not part of the step's
declaration, its live events or its trace. With `RayExecutor`, the runtime
travels to the worker, so every value must pickle with `cloudpickle`.

## Trace example

The `Summarize` call above, as `result.trace`. The model looked up a term, then
answered with its run summary.

```json
{
  "header": {
    "status": "succeeded",
    "stop_reason": "final_output",
    "model": "gpt-5.4-mini-2026-03-17",
    "duration_ms": 2900,
    "usage": {
      "input_tokens": 300,
      "output_tokens": 70,
      "cache_read_input_tokens": 200,
      "cache_creation_input_tokens": 0,
      "cost_usd": null
    },
    "turn_count": 2,
    "tool_call_count": 1,
    "external_run_id": null,
    "session_id": null,
    "native": {
      "format": "openai_agents.responses/v1",
      "max_turns": 6,
      "requests": 2,
      "reasoning_tokens": 0,
      "last_response_id": "resp_02",
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
          "call_id": "call_01",
          "caller": "model",
          "name": "lookup_term",
          "arguments": {"term": "DAG"},
          "result": "\"A data-flow graph.\"",
          "error": null,
          "duration_ms": 2
        }
      ],
      "duration_ms": 800,
      "usage": {
        "input_tokens": 240,
        "output_tokens": 20,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cost_usd": null
      },
      "native": {
        "format": "openai_agents.responses/v1",
        "response_id": "resp_01",
        "model": "gpt-5.4-mini-2026-03-17",
        "reasoning_tokens": 0,
        "refusal": null,
        "answer": false
      }
    },
    {
      "index": 2,
      "message": "Looked up DAG in the glossary and summarized the document in three words.",
      "reasoning": null,
      "tool_calls": [],
      "duration_ms": 700,
      "usage": {
        "input_tokens": 60,
        "output_tokens": 50,
        "cache_read_input_tokens": 200,
        "cache_creation_input_tokens": 0,
        "cost_usd": null
      },
      "native": {
        "format": "openai_agents.responses/v1",
        "response_id": "resp_02",
        "model": "gpt-5.4-mini-2026-03-17",
        "reasoning_tokens": 0,
        "refusal": null,
        "answer": true
      }
    }
  ]
}
```

## Native trace header

The trace header's `native` part.

```python
class OpenAIAgentTraceHeader:
    format: Literal["openai_agents.responses/v1"]
    max_turns: int                  # the most turns the run could take
    requests: int                   # model requests the SDK made, retries included
    reasoning_tokens: int | None    # output tokens spent reasoning; None without requests
    last_response_id: str | None    # the OpenAI ID of the last completed response
    errors: tuple[str, ...]         # why the run failed, cut to 4096 characters
```

## Native turn

Each turn's `native` part: the model response behind the turn.

```python
class OpenAIAgentTurn:
    format: Literal["openai_agents.responses/v1"]
    response_id: str | None
    model: str                      # the model that answered
    reasoning_tokens: int | None    # None when the response reported no usage
    refusal: str | None             # set when the model declined; the turn then counts as failed
    answer: bool                    # this response carried the run's answer
```

## Native live events

The `native` part of a live event, for what only the SDK reports. It is sent with
`common: None`; every other OpenAIAgent event is a shared event alone, with
`native: None`.

```python
OpenAIAgentNativeEvent = AgentStarted


class AgentStarted:                 # the SDK started the step's agent
    format: Literal["openai_agents.responses/v1"]
    kind: Literal["agent.started"]
    agent_name: str                 # the step's name
    model: str | None               # the model setting; None for the SDK's default
    tools: tuple[str, ...]          # every tool the model can call
```

```json
{
  "kind": "event",
  "schema_version": 2,
  "invocation_id": "3f9a0c7e",
  "sequence": 2,
  "timestamp_ns": 1761480000045000000,
  "common": null,
  "native": {
    "format": "openai_agents.responses/v1",
    "kind": "agent.started",
    "agent_name": "summarize",
    "model": "gpt-5.4-mini",
    "tools": ["lookup_term"]
  }
}
```

## How OpenAIAgent fills the shared data

### Request

| `AgentRequest` | OpenAI Agents SDK |
| --- | --- |
| `signature` instructions | The agent's instructions, followed by the narration paragraph when `narrate` is on, the skill list when the step has skills, then `system_prompt_append`. |
| `signature` outputs | The agent's output type: an OpenAI strict JSON schema the answer must match, plus a required `run_summary` string when `narrate` is on. |
| `inputs` | One user message: one instruction line, then the inputs as JSON. |
| `ava.File` inputs | Attached to the message after the JSON, numbered; the JSON names each file input by its attachment, such as `"attachment 1: report.pdf"`. An image (`image/*`) is sent as an image; any other file as a file the model reads, which the OpenAI API accepts only for types it supports, such as PDF and text. The type is the file's `content_type`, else guessed from its name. |
| `ava.Workspace` inputs | Rejected with `AgentStepError`: the agent has no file system. |
| `tools` | Offered to the model as function tools under their own names. Their results reach the model as JSON. |
| `skills` | Listed in the instructions by name and description. The model reads a skill with two tools Avalanche adds: `load_skill(name)` returns its `SKILL.md` and the paths of its files, and `read_skill_file(skill, path)` returns one file's text. The model can read a skill's scripts but not run them. A step tool named `load_skill` or `read_skill_file` raises `AgentStepError` when the step has skills. |
| `invocation_id`, `step_name` | The agent is named after the step; the invocation ID is not sent. |

For `document="Avalanche runs DAGs."`, the model's message is:

```text
Complete the task for these inputs.

{
  "document": "Avalanche runs DAGs."
}
```

When the signature has `ava.File` outputs, the message ends with one more line
asking for each file's name and text (see [Outputs](#outputs)).

A tool that raises does not fail the step. The model gets `"<error type>:
<message>"` as the tool's result, and the trace records it in the call's `error`.
A signature, tool parameter or output type that OpenAI strict JSON schemas cannot
express, such as a `dict[str, int]` output, raises `AgentStepError` before the
call starts.

### Live events

| `common.kind` | OpenAI Agents SDK |
| --- | --- |
| `run.started` | Sent before the run starts. `model` is the `model` setting, `None` for the SDK's default; `inputs` are the step's inputs, files by reference. |
| `tool.started` | The model asked for a tool. `turn` is the turn that asked. |
| `tool.finished` | The tool's result went back to the model. `turn` is the turn that asked; `error` is set when the tool failed. |
| `turn.finished` | A response and its tool calls are done: sent when the next response starts, or when the run ends. |

The `Summarize` call above sends, in order: `run.started`, `agent.started`, then
`tool.started`, `tool.finished` and `turn.finished` for the first turn,
`turn.finished` for the answering turn, then the final event.

### Final event

| Final event | OpenAI Agents SDK |
| --- | --- |
| `trace_finished` | The run ended, with or without an answer. `outputs` is the model's JSON answer without `run_summary`, with `ava.File` outputs as file references. `outputs` is `None` when the run failed, for example after `max_turns` responses or when the API returned an error, or when turning the answer into outputs failed; the step then raises. A failed run's header keeps the turns finished so far. |
| `trace_unavailable` | The call was cancelled. |

### Trace header

| Header field | OpenAI Agents SDK |
| --- | --- |
| `status` | `"succeeded"` when the run returned an answer; else `"failed"`. |
| `stop_reason` | `"final_output"` for an answer. For a failure, the exception's name in snake case, such as `"max_turns_exceeded"`, `"model_behavior_error"` or `"rate_limit_error"`. |
| `model` | The model that answered first, else the `model` setting. |
| `usage` | The whole run's usage, failed retries included; `null` when it made no request. |
| `turn_count` | Number of model responses. |
| `tool_call_count` | Number of tool calls. |
| `external_run_id` | The SDK trace ID when `tracing` is on; else `null`. |
| `session_id` | `null` |

### Turns

One turn per model response.

| Turn field | OpenAI Agents SDK |
| --- | --- |
| `message` | What the model wrote. The answering turn's text is its JSON answer; with `narrate` on, the turn shows the answer's `run_summary` instead. |
| `reasoning` | The response's reasoning summaries and reasoning text, when the model returns them. OpenAI reasoning models return summaries only when asked, for example with `reasoning=Reasoning(summary="auto")`. |
| `tool_calls` | Each tool the model asked for, with `caller: "model"` and the time the tool took. `arguments` are the model's JSON arguments, or its text when that is not valid JSON; `result` is the text the model got. |
| `duration_ms` | From the response's start until its tool calls finished. |
| `usage` | The response's usage, without cost. |
| `native` | An `OpenAIAgentTurn`. |

Tool arguments and results over 256 KiB are replaced by a marker.

### TokenUsage

| Field | OpenAI Agents SDK |
| --- | --- |
| `input_tokens` | Input tokens neither read from nor written to the prompt cache. OpenAI counts both in its `input_tokens`; Avalanche counts them apart, as for every runtime. |
| `output_tokens` | As reported, reasoning tokens included. |
| `cache_read_input_tokens` | OpenAI's cached input tokens. |
| `cache_creation_input_tokens` | OpenAI's cache-write input tokens. |
| `cost_usd` | `null`: the SDK reports no prices. |

### Outputs

The model's JSON answer, validated against the signature. OpenAIAgent returns
`JsonOutputs`, or `PythonOutputs` when the signature has `ava.File` outputs.

| Output type | OpenAI Agents SDK |
| --- | --- |
| `ava.File`, `list[ava.File]` | The model answers each file with its `name` and whole text `content`. Avalanche writes the text as UTF-8 into a fresh folder in the run workspace, `.outputs/<step>/<attempt>/<call>/`, and returns `ava.File` references. A name with folders, `.` or `..`, or a name used twice in one answer, raises `AgentOutputError`. Binary files cannot be written. |
| `ava.Workspace` | Rejected with `AgentStepError`: the agent has no file system. |

Other output types that hold files, such as a model with an `ava.File` field or
`ava.File | None`, are rejected.
