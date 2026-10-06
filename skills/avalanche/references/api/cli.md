# CLI reference

The supported command is `ava`, installed with `avalanche-ai`. Run it in the
project environment with `uv run ava`. Every command supports `-h` / `--help`.
Invoking without a subcommand prints help and returns `2`; argument-parser errors
also exit with status `2`.

Commands operate on local-development workflows. See [workflow declarations](workflows.md),
[inputs and context](inputs-context.md), and [file/workspace values](files-workspaces.md)
for the Python values submitted or retrieved through the CLI.

## `ava init [--editable-deps]`

Initialize an empty working directory using the packaged bootstrap script.
`--editable-deps` clones Avalanche and PredictRLM for editable development instead
of using only published packages. This command installs dependencies and may
prompt for provider setup; it is not a read-only project inspection. It returns
the bootstrap script's exit status.

## `ava operator [FLOW ...] [options]`

Discover workflows, serve gRPC and local webhooks, watch source changes, and by
default serve the browser UI and REST API.

| Argument | Default | Meaning |
| --- | --- | --- |
| `FLOW ...` | Workspace configuration | Python file/directory targets; accepts `alias=path` roots. Discovery imports Python modules, so use narrow trusted paths, not `.` over an arbitrary repository. |
| `--host` | `127.0.0.1` | gRPC listener host. Non-loopback use requires an external trusted, authenticated boundary. |
| `--port` | `7433` | gRPC port. |
| `--webhook-port` | `7434` | Local webhook HTTP port. |
| `--web-port` | `7435` | Loopback browser UI and REST API HTTP port. |
| `--no-web` | Off | Disable the browser/REST listener, leaving gRPC and webhook operation. |
| `--ray` | Off | Run workflows through Ray rather than the local executor. Requires the `ray` extra. |
| `--log-level` | `WARNING` | Terminal log threshold: `DEBUG`, `INFO`, `WARNING`, `ERROR`; input is case-normalized. |
| `--discovery-timeout SECONDS` | `60.0` | Positive finite maximum duration of a discovery scan. |

### Target configuration

Explicit positional targets take precedence. With none, the nearest
`pyproject.toml` in the working directory or its parents must contain a nonempty
list of strings:

```toml
[tool.avalanche]
flow_targets = ["src/workflows", "reports=src/report.py"]
```

Relative configured paths resolve against that file's directory, not the shell's
working directory. Missing, empty, invalid, or inaccessible targets fail rather
than falling back to scanning the current directory. An alias names a configured
root used in canonical workflow selectors.

## `ava dev [FLOW ...] [options]`

Start the operator and browser/REST listener, open the browser, watch workflows,
and stop owned services on exit. Uses the same target selection and local/Ray
choice as `operator`.

| Option | Default | Meaning |
| --- | --- | --- |
| `--port` | `7433` | Local gRPC listener port. |
| `--web-port` | `7435` | Local browser/REST port. |
| `--ray` | Off | Use Ray execution. |
| `--log-level` | `WARNING` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |
| `--discovery-timeout SECONDS` | `60.0` | Positive finite discovery deadline. |

Unlike `operator`, `dev` has no `--host`, `--webhook-port`, or `--no-web` options.
Its local webhook port is `7434`. Startup reports discovery, gRPC readiness, and
browser/REST endpoints. Ctrl-C initiates cleanup of owned services. Startup or
shutdown failure returns a nonzero status.

## `ava web [options]`

Serve the browser UI and REST/gRPC-Web proxy for an **already running** operator;
it does not start a second workflow engine.

| Option | Default | Meaning |
| --- | --- | --- |
| `--connect HOST:PORT` | `localhost:7433` | Upstream operator gRPC address. |
| `--host` | `127.0.0.1` | HTTP listen host. |
| `--port` | `7435` | HTTP listen port. |
| `--trusted-proxy` | Off | Explicitly acknowledge that non-loopback browser access is protected by a trusted proxy; does not implement authentication itself. |

Browser UI is at `/`; REST endpoints at `/api/v1`; interactive documentation at
`/api/docs`; OpenAPI JSON at `/api/openapi.json`.

## `ava run FLOW [options]`

Submit one run to an existing operator and print its run ID. Submission does not
wait for workflow completion. `FLOW` identifies the discovered workflow; prefer
the operator's canonical selector when names are ambiguous.

| Option | Default | Meaning |
| --- | --- | --- |
| `--connect HOST:PORT` | `localhost:7433` | Operator address. |
| `--input JSON` | Omitted | JSON **object** for the declared `BaseInput` model. |
| `--context JSON` | Omitted | JSON **object** for declared run context. |
| `--file FIELD=PATH` | None; repeatable | Read local bytes and attach a top-level `File` input. |
| `--workspace FIELD=DIR` | None; repeatable | Capture a local directory as a top-level `Workspace` input. |

The same field cannot be supplied by JSON, a file attachment, and/or a workspace.
Invalid JSON, assignment syntax, duplicate fields, or an operator submission error
fails the command; it does not silently omit inputs. Successful submission returns
`0`; handled parsing/submission errors return `1` and print their message to stderr.
Submission success does not mean the input/context models have validated or the
workflow has succeeded. Syntactically valid object JSON with missing required
fields or invalid model values can receive a run ID and exit status `0`, then fail
the asynchronous run. Inspect the run outcome, for example with
`ava result RUN_ID --wait --output-dir PATH`, before treating it as successful.

## `ava result RUN_ID --output-dir PATH [options]`

Download a successful terminal result. This is separate from run submission.

| Argument / option | Default | Meaning |
| --- | --- | --- |
| `RUN_ID` | Required | Submitted run identifier. |
| `--output-dir PATH` | Required | **New**, nonexistent destination directory; parent is a caller-owned local namespace. Existing destinations are not overwritten. |
| `--connect HOST:PORT` | `localhost:7433` | Operator address. |
| `--wait` | Off | Wait for a nonterminal run before attempting retrieval. |
| `--timeout SECONDS` | `300.0` | Positive finite wait limit, used with `--wait`; validated even without that flag. |

Writes result metadata and file/workspace outputs into the new destination,
then prints JSON metadata rather than binary contents. Existing destinations are
not overwritten. Failed, cancelled, unavailable, or still-running results cannot
be downloaded as successful results. Success returns `0`, handled retrieval or
filesystem failures `1`, and invalid timeout `2`. The output parent must be a
caller-owned local directory.

## `ava webhooks list [--connect HOST:PORT]`

List discovered local webhook routes as JSON. Default connection is
`localhost:7433`.

## `ava webhooks get SELECTOR [--connect HOST:PORT]`

Look up a canonical workflow selector's webhook route as JSON. Default connection
is `localhost:7433`. These inspect configuration; they do not invoke the webhook.
Declare webhooks using `avalanche.Webhook` on `@workflow`; see
[workflow and webhook declarations](workflows.md).

## `ava tui [FLOW[/NODE]] [options]`

Launch the Textual terminal UI. Without `--connect`, use the mock provider; with
it, connect to an operator. The optional positional selector deep-links a workflow
and optionally a node.

| Option | Default | Meaning |
| --- | --- | --- |
| `--connect HOST:PORT` | Omitted | Select connected rather than mock mode. |
| `--token` | `AVALANCHE_TUI_GRPC_TOKEN`, otherwise omitted | Client bearer token for a server/proxy that understands it. |
| `--tls` | `AVALANCHE_TUI_GRPC_TLS`, otherwise off | Enable TLS; environment values `1`, `true`, and `yes` enable it case-insensitively. |
| `--insecure` | Off | Override `--tls` and the TLS environment setting to disable TLS, unless an explicit `--tls-ca-cert` is supplied. |
| `--tls-ca-cert PATH` | `AVALANCHE_TUI_GRPC_TLS_CA_CERT`, otherwise omitted | CA certificate for verification. An explicit flag enables TLS even with `--insecure`, regardless of argument order. |

These are client transport options, not evidence that the local operator provides
a production authentication service. Do not expose a local operator solely on
the assumption that a CLI token flag secures the server.
