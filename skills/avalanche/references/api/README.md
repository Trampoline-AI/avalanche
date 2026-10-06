# Avalanche API reference

API reference for writing and running workflows with Avalanche 0.7.0. Each entry
lists the callable or value an author uses, its parameters/defaults, fields,
return value, errors, and behavioral constraints. This is not an inventory of
Python-importable implementation objects or a workflow-design guide.

```python
import avalanche as ava
```

## Reference

| Area | APIs |
| --- | --- |
| [Workflows](workflows.md) | `source`, `step`, `dest`, `workflow`; agent/classifier step entry points; graph operations; `cron` and `Webhook`. |
| [Runs](runs.md) | `Workflow.run`, `LocalExecutor`, and the returned run handle's result, status, and cancellation methods. |
| [Inputs and context](inputs-context.md) | `BaseInput`, `BaseContext`, injected `RunContext`, `ava.input`, and `Logger`. |
| [Files and workspaces](files-workspaces.md) | `File` and `Workspace` construction, fields, reads, and snapshots. |
| [Agents](agents.md) | `agent_step`, `Signature`, `InputField`, `OutputField`, injected `Agent`, skills, agent files, and caller errors. |
| [Classifiers](classifiers.md) | `classifier_step`, injected `Classifier`, question fields, `ClassificationResult`, answer/usage fields, and caller errors. |
| [Iceberg](storage/iceberg.md) | `IcebergNamespace`, `IcebergNsConfig`, `IcebergTable`, configuration, schemas, and append/read/scan results. |
| [Lance](storage/lance.md) | `LanceNamespace`, `LanceNamespaceConfig`, `LanceTable`, configuration, schemas, and append/read/scan results. |
| [Streams](storage/streams.md) | `Stream` declarations, modes, injected values, and consumption behavior. |
| [Cursors](storage/cursors.md) | `Cursor`, checkpoint reads/writes, and transactional appends. |
| [CLI](cli.md) | `ava` commands, arguments, options, outputs, and exit behavior. |

The workflow page lists every step primitive. Its agent and classifier entries
link to their dedicated references rather than repeating their full parameter
and result definitions. Other pages likewise link the APIs they accept or return.

## Reading entries

- `*` introduces keyword-only parameters. A field without a default is required;
  nullable and optional-to-supply are different properties.
- An **author-facing call form** deliberately shows the workflow-author subset of
  a callable, not extension hooks or every implementation-level argument.
- A decorated node call declares work; its deferred output is not a completed
  payload. A run handle is the object used to wait for execution.
- An injected argument is supplied by Avalanche when a step runs. Its declaration
  marker is not the value received by the step body.
- Standard Pydantic model methods and upstream Polars, Arrow, PyIceberg, DSPy, and
  PredictRLM APIs are not reproduced here. Returned fields and methods needed to
  use Avalanche are documented with the API that produces them.

Python 3.11–3.13 is supported. Agent and classifier support is included in the
base installation; Lance storage needs `uv add 'avalanche-ai[lance]'`. Avalanche
remains an early-release, local-development toolkit.

## Offline skill reference

The same reference tree is bundled under `skills/avalanche/references/api/`.
Keep those files identical to `docs/api/` when updating an API entry; all topic
links work in either location without a repository checkout.
