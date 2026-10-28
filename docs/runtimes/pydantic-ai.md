# PydanticAIAgent runtime

PydanticAIAgent runs an agent step through [Pydantic AI](https://ai.pydantic.dev),
on any model Pydantic AI supports: OpenAI, Anthropic, Google, Groq, Mistral,
Bedrock, Ollama and others. The model calls your step's tools and answers by
calling Pydantic AI's `final_result` tool with JSON that matches the signature's
outputs; Pydantic AI asks it to try again when the answer does not validate.

Install it with `uv add 'avalanche-ai[pydantic-ai]'` wherever the step runs, Ray
workers included. The extra installs the full `pydantic-ai` package, with every
provider's client. Each provider reads its credentials from its usual environment
variable, such as `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`, in the process that runs
the step.

## Using it

```python
import avalanche as ava


@ava.agent_step(
    Summarize,
    runtime=ava.runtimes.PydanticAIAgent(model="openai:gpt-5.4-nano", max_turns=10),
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
class PydanticAIAgent:
    model: str | None = None                # "provider:model", e.g. "openai:gpt-5.4-nano"
    max_turns: PositiveInt | None = None    # most model requests; None: Pydantic AI's limit, 50
    instructions_append: str | None = None  # added after the signature's instructions
```

`model` is required to run, on the step or on the workflow default; a step that
runs without one raises `AgentStepError`. Building the runtime needs no Pydantic
AI install, so workflows that declare these steps import and publish without the
extra; a step that runs without it raises `AgentStepExecutionError` naming the
extra.

### Pydantic AI agent options

Any other keyword is an argument of
[`pydantic_ai.Agent`](https://ai.pydantic.dev/api/agent/), passed with Pydantic
AI's own types:

```python
from pydantic_ai.capabilities import WebSearch
from pydantic_ai.mcp import MCPToolset

runtime = ava.runtimes.PydanticAIAgent(
    model="anthropic:claude-haiku-5-5",
    model_settings={"temperature": 0.2, "max_tokens": 2000},
    retries=3,
    toolsets=[MCPToolset("https://docs.example.com/mcp")],
    capabilities=[WebSearch()],
)
```

Avalanche checks the option's name, not its value: Pydantic AI checks values. An
unknown name raises `ValueError` when you build the runtime if the extra is
installed there, and `AgentStepError` naming the option when the step runs
otherwise.

| Agent option | With yours |
| --- | --- |
| `tools`, `toolsets`, `capabilities` | Must be lists. Yours are added after Avalanche's: the step's tools, the file tools and the step's skills. Anything else raises `AgentStepError`. |
| `name` | Defaults to the step name. |
| `output_type` | Rejected with `ValueError`: the signature's outputs define the answer. |
| `instructions`, `system_prompt` | Rejected with `ValueError`: the step's instructions are the agent's instructions. Use `instructions_append`. |
| `deps_type` | Rejected with `ValueError`: steps run without Pydantic AI dependencies. |
| Any other option | Passed as given, such as `model_settings`, `retries`, `end_strategy`, `tool_timeout` or `max_concurrency`. |

A step's options replace the workflow default's option by option: a step's
`capabilities` replaces the default's `capabilities` whole.

Agent options are never published or recorded: the step's declaration lists their
names only, and their values stay out of its live events and its trace. With
`RayExecutor`, the runtime travels to the worker, so every option value must
pickle with `cloudpickle`.

## Trace example

The `Summarize` call above, as `result.trace`. The model looked up a term, then
answered through `final_result`.

```json
{
  "header": {
    "status": "succeeded",
    "stop_reason": "final_result",
    "model": "gpt-5.4-nano-2026-03-17",
    "duration_ms": 2400,
    "usage": {
      "input_tokens": 410,
      "output_tokens": 52,
      "cache_read_input_tokens": 0,
      "cache_creation_input_tokens": 0,
      "cost_usd": 0.0001
    },
    "turn_count": 2,
    "tool_call_count": 2,
    "external_run_id": "01a184cb-2778-75d1-9d49-c6e9ffb3be53",
    "session_id": "01a184cb-2778-75d1-9d49-c6ea532b6c3c",
    "native": {
      "format": "pydantic_ai.messages/v1",
      "requests": 2,
      "retries": 0,
      "error": null
    }
  },
  "turns": [
    {
      "index": 1,
      "message": null,
      "reasoning": null,
      "tool_calls": [
        {
          "call_id": "call_01",
          "caller": "model",
          "name": "lookup_term",
          "arguments": {"term": "DAG"},
          "result": "A data-flow graph.",
          "error": null,
          "duration_ms": 1
        }
      ],
      "duration_ms": 1300,
      "usage": {
        "input_tokens": 190,
        "output_tokens": 20,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cost_usd": 0.00004
      },
      "native": {
        "format": "pydantic_ai.messages/v1",
        "model": "gpt-5.4-nano-2026-03-17",
        "provider": "openai",
        "provider_response_id": "resp_01",
        "finish_reason": "stop"
      }
    },
    {
      "index": 2,
      "message": null,
      "reasoning": null,
      "tool_calls": [
        {
          "call_id": "call_02",
          "caller": "model",
          "name": "final_result",
          "arguments": {"summary": "Avalanche runs DAGs.", "word_count": 3},
          "result": "Final result processed.",
          "error": null,
          "duration_ms": 0
        }
      ],
      "duration_ms": 1100,
      "usage": {
        "input_tokens": 220,
        "output_tokens": 32,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cost_usd": 0.00006
      },
      "native": {
        "format": "pydantic_ai.messages/v1",
        "model": "gpt-5.4-nano-2026-03-17",
        "provider": "openai",
        "provider_response_id": "resp_02",
        "finish_reason": "stop"
      }
    }
  ]
}
```

## Native trace header

The trace header's `native` part.

```python
class PydanticAITraceHeader:
    format: Literal["pydantic_ai.messages/v1"]
    requests: int        # model requests, as max_turns counts them
    retries: int         # tool and answer calls the model was asked to make again
    error: str | None    # why the run failed, cut to 4096 characters
```

## Native turn

Each turn's `native` part: the model response behind the turn.

```python
class PydanticAITurn:
    format: Literal["pydantic_ai.messages/v1"]
    model: str | None                 # the model that answered, as the provider names it
    provider: str | None              # e.g. "openai"
    provider_response_id: str | None
    finish_reason: str | None         # "stop", "length", "content_filter", "tool_call" or
                                      # "error"; "error" marks the turn failed
```

## Native live events

The `native` part of a live event, for what only Pydantic AI reports. Every class
also has `format: Literal["pydantic_ai.messages/v1"]`.

```python
PydanticAINativeEvent = RunInitialized | ToolRetried


class RunInitialized:              # sent with common: None
    kind: Literal["run.initialized"]
    run_id: str
    conversation_id: str
    model: str
    tools: tuple[str, ...]         # the step's tools and the file tools Avalanche adds
    skills: tuple[str, ...]        # the step's skills, which the model loads by name


class ToolRetried:                 # sent with the tool.finished event of the call
    kind: Literal["tool.retried"]
    call_id: str
    attempt: int
    max_retries: int
```

An answer that failed validation, and the request to try again:

```json
{
  "kind": "event",
  "schema_version": 2,
  "invocation_id": "3f9a0c7e",
  "sequence": 4,
  "timestamp_ns": 1761480000045000000,
  "common": {
    "kind": "tool.finished",
    "call_id": "call_02",
    "name": "final_result",
    "turn": 1,
    "error": "{\"error\":[{\"type\":\"int_parsing\",\"loc\":[\"word_count\"],\"msg\":\"Input should be a valid integer, unable to parse string as an integer\",\"input\":\"three\"}]}"
  },
  "native": {
    "format": "pydantic_ai.messages/v1",
    "kind": "tool.retried",
    "call_id": "call_02",
    "attempt": 1,
    "max_retries": 1
  }
}
```

## How PydanticAIAgent fills the shared data

### Request

| `AgentRequest` | Pydantic AI |
| --- | --- |
| `signature` instructions | The agent's instructions, then a note about the file tools when there is a working folder, then `instructions_append`. |
| `signature` outputs | The `final_result` tool's arguments. `ava.Workspace` outputs are left out; `ava.File` outputs become text files (see [Outputs](#outputs)). |
| `inputs` | The prompt: one instruction line, then the inputs as JSON. |
| `ava.File` inputs | Attached to the prompt after the JSON, each after a line naming it, such as `attached file 1: report.pdf`; the JSON gives the label in the field. The model receives the file's bytes. |
| `ava.Workspace` input | The working folder of the file tools. At most one. |
| `tools` | Offered under their own names. A tool that raises does not fail the step: the model gets `{"error": "<Error>: <message>"}` as a failed result, and the trace records that text in the call's `error`. |
| `skills` | Each skill is a Pydantic AI capability loaded on demand. The model sees its name and description, and loads its instructions with the `load_capability` tool. Skills with files add a `read_skill_file` tool. |
| `invocation_id`, `step_name` | The step name is the agent's `name`; the invocation ID is not sent. |

For `document="Avalanche runs DAGs."`, the prompt is:

```text
Complete the task for these inputs. Input files are attached after them, each named by its label.

{
  "document": "Avalanche runs DAGs."
}
```

Pydantic AI sends files to a model as images, audio, video or documents (PDF,
plain text, CSV, HTML, Markdown, Word and Excel); each provider takes some of these.
A file's content type comes from the `ava.File`, else from its name. A file without
one, or of another type, raises `AgentStepError` before the model is called; a type
the provider does not take fails the call.

#### File tools

With an `ava.Workspace` input or output, the agent gets three tools for the folder
it works in. Each path is relative to that folder, and one that resolves outside
it, through `..` or a symbolic link, is refused.

| Tool | Does |
| --- | --- |
| `list_files(folder=".")` | The files under a folder, as relative paths; at most 1000. |
| `read_file(path)` | A UTF-8 text file's text. |
| `write_file(path, text)` | Writes a UTF-8 text file, making its folders and replacing a file already there. |

A reference workspace is worked on in place, so the agent's writes land in your
directory; a value workspace is worked on in its temporary copy. With a workspace
output but no workspace input, the agent works in an empty temporary folder.

#### Skills

A skill's capability instructions are its instructions, followed, when it has
files, by the list of its file paths and how to read them:

```text
Read references/glossary.md before answering.

Read this skill's files with read_skill_file, skill 'contract-glossary':
- references/glossary.md
```

`read_skill_file(skill, path)` returns a UTF-8 text file of a skill by its path in
the skill, and refuses any other path. The skill's `license`, `compatibility`,
`metadata` and `allowed_tools` are not sent.

### Live events

| `common.kind` | Pydantic AI |
| --- | --- |
| `run.started` | Sent before the agent is built. `model` is the `model` setting; `inputs` are the step's inputs, files and workspaces by reference. |
| `tool.started` | Pydantic AI started a tool or `final_result` call. `turn` is the turn that asked. |
| `tool.finished` | The call's result went back to the model. `error` is set when the call failed or must be made again; the latter also carries `ToolRetried`. |
| `turn.finished` | The response's tool calls finished. |

The `Summarize` call above sends, in order: `run.started`, `run.initialized`,
then `tool.started`, `tool.finished` and `turn.finished` for each of its two turns
(the second for `final_result`), then the final event.

Tools the provider runs itself, such as web search through a capability, appear
in their turn's `tool_calls` with their results, without `tool.started` or
`tool.finished` events.

### Final event

| Final event | Pydantic AI |
| --- | --- |
| `trace_finished` | The run returned an answer, or Pydantic AI ended it: a model API error, `max_turns` reached, or answers that kept failing validation. In the second case the header's `status` is `"failed"`, its `stop_reason` is Pydantic AI's error class, such as `"UsageLimitExceeded"`, and `outputs` is `None`; the step raises `AgentStepExecutionError`. `outputs` is also `None` when turning the answer into outputs failed, for example a file name outside the output folder. |
| `trace_unavailable` | The call was cancelled, or failed another way, such as a missing provider key when the agent is built. |

### Trace header

| Header field | Pydantic AI |
| --- | --- |
| `status` | `"succeeded"` when the run returned an answer; else `"failed"`. |
| `stop_reason` | `"final_result"`, or the error class that ended the run. |
| `model` | The model that answered first, as the provider names it, else the `model` setting. |
| `duration_ms` | Measured by Avalanche from building the agent to the answer. |
| `usage` | The run's usage, with its cost when Pydantic AI knows the model's price. `null` when a response did not report its usage. |
| `turn_count` | Number of model responses. |
| `tool_call_count` | Number of tool calls, `final_result` and `load_capability` included. |
| `external_run_id` | Pydantic AI's run ID. |
| `session_id` | Pydantic AI's conversation ID. |

### Turns

One turn per model response.

| Turn field | Pydantic AI |
| --- | --- |
| `message` | The response's text parts, joined by blank lines. |
| `reasoning` | The response's thinking parts, when the provider returns them. |
| `tool_calls` | Each call the response made, with `caller: "model"`. `arguments` are the arguments as the model wrote them, a string when they are not JSON. `result` is the tool's return value as JSON, with files by reference. `duration_ms` is measured by Avalanche; `null` for tools the provider ran. |
| `duration_ms` | From the model request to the end of the response's tool calls. |
| `usage` | The response's usage, with its cost when known. |
| `native` | A `PydanticAITurn`. |

Tool arguments and results over 256 KiB are replaced by a marker.

### TokenUsage

| Field | Pydantic AI |
| --- | --- |
| `input_tokens` | Input tokens outside the prompt cache: Pydantic AI's input count minus its cache reads and writes. |
| `output_tokens` | As reported. |
| `cache_read_input_tokens`, `cache_creation_input_tokens` | As reported; `0` when the provider reports none. |
| `cost_usd` | Pydantic AI's price estimate, `null` when it has no price for the model. |

### Outputs

The model's `final_result` arguments, validated against the signature.

| Output type | Pydantic AI |
| --- | --- |
| `ava.File`, `list[ava.File]` | The model answers with `{"name": ..., "text": ...}` for each file. Avalanche writes the text, as UTF-8, into a fresh folder in the run workspace, `.outputs/<step>/<attempt>/<call>/`, and returns `ava.File` references. A name that leaves the folder, or one given twice, raises `AgentOutputError`. Only text files can be returned. |
| `ava.Workspace` | Left out of the answer. Avalanche fills it with the working folder's contents at the end. |

Other types that hold files, such as a model with an `ava.File` field, are
rejected before the model is called, as inputs and as outputs.
