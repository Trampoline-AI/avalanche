# Files and workspaces

```python
import avalanche as ava
```

Use these values in [input models](inputs-context.md#input-and-context-declarations), node arguments, and workflow results. A `File` carries one in-memory byte body; a `Workspace` carries either a portable directory tree or a reference to a directory provided by the host.

## File

```python
ava.File(
    *, content: bytes, name: str | None = None,
    content_type: str | None = None, sha256: str | None = None,
)
ava.File.from_path(
    path: str | pathlib.Path, *, content_type: str | None = None,
) -> ava.File
file.read_bytes() -> bytes
file.open() -> io.BytesIO
```

| Field | Required/default and meaning |
| --- | --- |
| `content: bytes` | Required; complete in-memory file body. |
| `name: str \| None` | `None`; optional original/display filename, not an output path. |
| `content_type: str \| None` | `None`; optional media type, not automatically inferred. |
| `sha256: str \| None` | Optional on input; computed lowercase SHA-256 of the content. A supplied digest must match case-insensitively and is normalized to lowercase. |

Construction returns a file value; invalid field values, unknown fields, or a mismatched digest raise Pydantic validation errors. The model is not frozen. `from_path` reads all bytes immediately, sets `name` to the basename, and propagates filesystem read errors. `read_bytes` returns the stored bytes. Each `open()` returns a fresh in-memory binary stream: writes to it do not modify the `File`.

A file has no local `path` and is not a live filesystem handle. It can be passed/returned directly or inside supported containers/models. For disk-based tools, use a workspace or explicitly write the bytes.

## Workspace

A `Workspace` is a reference to a directory the host keeps. It never holds file
bytes: steps read and write the directory through `.path`, and only the reference
crosses step, process and result boundaries.

```python
ava.Workspace()                              # the run's workspace
ava.Workspace.from_path(path: str | pathlib.Path) -> ava.Workspace
ava.Workspace.from_directory(
    path: str | pathlib.Path, *, reference: str,
) -> ava.Workspace
workspace.reference -> str | None
workspace.is_reference -> bool
workspace.path -> pathlib.Path
workspace.snapshot() -> ava.Workspace
```

| API | Contract |
| --- | --- |
| `Workspace()` | The workspace of the current run: one directory the host creates on first use and keeps with the run. Every `Workspace()` in a run is that same directory. |
| `Workspace.from_path(path)` | Copies the directory once into the store of the host this code runs on and refers to the copy. Later edits to the source do not change it, and steps never edit the source. To pass a directory to an operator from another process, use `ava run --workspace` or `GrpcStateProvider.stage_input_workspace`. |
| `Workspace.from_directory(path, reference=...)` | For hosts: binds a directory in place under a reference the host chose. Nothing is copied. |
| `workspace.path` | The directory. For `Workspace()` it is available while a step runs and raises `RuntimeError` outside a run. |
| `workspace.snapshot()` | Returns the same workspace. |

### The run workspace

Create the workspace in the workflow body and pass it to the steps that share files:

```python
@ava.source
def fetch(request: Request, checkout: ava.Workspace) -> Snapshot:
    (checkout.path / "README.md").write_text(download(request.url))
    return Snapshot(checkout=checkout)

@ava.step
def review(snapshot: Snapshot) -> int:
    return len((snapshot.checkout.path / "README.md").read_text())

@ava.workflow(input=Request)
def flow():
    return (snapshot := fetch(checkout=ava.Workspace())) >> review(snapshot)
```

Steps edit the directory in place. A step boundary passes the reference, so the
size of the tree never counts against result limits and no step pays for a copy.
The directory is shared: a step that must not see or disturb another step's files
works in its own subfolder. Locally, steps that run in parallel see each other's
writes as they happen.

The local operator keeps run workspaces in `workspaces/` next to its state
database, so they survive a restart and later state entries of a machine see earlier edits.
That folder is the only record of them: a reference names a folder inside it, so
you can move or copy the project with its state and every stored reference still
resolves. `ava run --workspace FIELD=DIR` sends `DIR` to the operator, which keeps
the copy in the same folder and removes it with the run.
`ava state delete`, `prune` and `clear` remove a run's workspace with the run.
A direct `Workflow.run()` without an operator keeps them in the system temporary
folder. Set `AVALANCHE_LOCAL_WORKSPACE_ROOT` to keep them elsewhere.

A workspace returned from a step, including one created inside the step with
`ava.Workspace()`, is stored in results as its reference.
`ava result RUN_ID --output-dir PATH` copies the files out.

### Inline file trees were removed

Earlier releases let a `Workspace` carry its files as bytes (`entries`,
`Workspace.from_path` reading a tree into memory, a `snapshot()` that did the
same). That form is gone. A manifest with a non-empty `entries` list fails
validation with `workspace_value_unsupported` on every host. The local operator
still reads results and run inputs that earlier releases stored with an inline
tree. A result's tree is written once to `workspaces/stored-trees/` and returned
as a reference, for reading. A run input's tree is written to a folder of its own
at every start, so runs started from the same stored input never share files.

### Directory-backed reference workspaces

Use `Workspace.from_directory(path, reference=...)` when the host manages storage
and its lifetime. The path must be absolute and name an existing directory at
construction; otherwise construction raises `ValueError`. `.path` returns
`Path(path)` without resolving it, including directory symlinks, both inside and
outside node execution. Nothing is copied, captured, or temporarily materialized.
Avalanche never removes the host directory, including after node failure.

```python
# The host has already created and owns this directory.
workspace = ava.Workspace.from_directory(
    "/absolute/host/workspace", reference="host-managed:example",
)
workspace.path.joinpath("result.txt").write_text("ready")
assert workspace.snapshot() is workspace
```

`reference` is an opaque, non-empty string of at most 1,024 characters with no NUL.
Whitespace and Unicode are preserved. `.reference` returns that string and
`.is_reference` is `True`; `Workspace()` returns `None` and `False` until the host
names it.

A bound reference's `snapshot()` returns the same workspace. Local execution passes references through
unchanged, including supported nested return shapes. These are shared directories:
the host owns persistence, access, and concurrent writes.

Python/JSON model dumps and pickle state contain exactly
`{"version": 1, "reference": "host-managed:example"}`, never paths or entries.
Validating this dictionary (including with `from_manifest`) or unpickling gives an
**unbound** reference. Its `.path` and `snapshot()` raise
`RuntimeError("workspace reference is not bound to a directory")`. The original
bound workspace remains usable. A remote executor must resolve the reference and
construct `Workspace.from_directory(directory, reference=reference)` before code
accesses `.path`; serialization does not transfer a host-local directory binding.
There is no implicit reference resolver.

For a repeatable 200 MB two-node write/hash run, use
`uv run python scripts/benchmark_workspace.py`. The script reports peak RSS growth
across the workflow and requires it to stay below 50 MB.

## Input and output boundaries

Direct Python runs accept native `File` and `Workspace` values without remote transport limits. `Workspace.from_path` copies a directory on disk and `from_directory` refers to one in place; neither reads file contents into memory.

Reference workspaces cross CLI/remote boundaries as the reference dictionary with no file attachments. Receiving code must bind the reference through the host before using its directory. Existing transport envelope and JSON size limits still apply.

The [CLI](cli.md) accepts file/directory input options and materializes requested output into a destination that must not already exist. Output file paths are generated safely rather than trusting `File.name` as a filesystem path. Without requested filesystem output, file results remain values rather than being written automatically.

For CLI/remote results, supported result values include JSON-compatible scalars, lists, tuples, string-keyed dictionaries, Pydantic models, files, and workspaces. Pydantic results are serialized using field aliases; their original Python model class is not preserved. Arbitrary Python objects supported by direct local runs are not necessarily transportable.

Transported results are bounded: at most 8 MiB per file, 1,024 file attachments, 32 MiB total attachment bytes, 4 MiB encoded JSON, and 32 MiB JSON plus attachments. File names are limited to 1,024 characters and media types to 255. These are output transport constraints, not `File` constructor limits. Invalid/oversized outputs fail rather than being silently truncated.
