# Files and workspaces

```python
import avalanche as ava
```

Use these types in [input models](inputs-context.md#input-and-context-declarations),
node arguments, agent signatures, and workflow results. A `File` is a reference to
one file in a workspace; a `Workspace` is a portable tree or host directory reference.

## File

```python
ava.File.from_path(
    path: str | pathlib.Path, *, content_type: str | None = None,
    workspace: ava.Workspace | None = None,
) -> ava.File
file.path -> pathlib.Path
file.open(mode="rb")  # real handle, read modes only: r, rt, rb
file.read_bytes() -> bytes
file.snapshot() -> ava.File
file.bind(path: str | pathlib.Path) -> ava.File
```

`from_path` binds without reading; `size` comes from stat, and SHA-256 is streamed
lazily when snapshotting/serializing. Fields are frozen: `ref` identifies the
workspace and relative POSIX path, `size` is required, `sha256` is initially lazy
for local construction, and `name`/`content_type` are optional display metadata.
Deserialized refs have no host path; access fails until a host authorizes a binding.
Write modes (`w`, `a`, `x`, `+`) are rejected by `open()`.

`File(content=...)`, `.content`, byte inputs and `ava.agent.File` are deprecated
until the next minor release. They do not create a byte-valued wire form.
See [File references](../file-references.md) for the tagged wire contract,
`ReferenceBinder`, `rebind_arguments`, agent mounting/output reservations and
migration examples.

## Workspace

Author-facing construction forms (not the complete model constructor):

```python
ava.Workspace()                              # empty tree
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
| `Workspace()` | Returns an empty portable tree. Empty directories are supported. |
| `Workspace.from_path(path)` | Captures a directory tree and all file bytes immediately; later edits to the source directory do not change it. |
| `workspace.path` (value workspace) | Lazily materializes a writable temporary directory while user node code is executing. Repeated accesses reuse that invocation's directory. Outside node execution, raises `RuntimeError`. |
| `workspace.snapshot()` (value workspace) | Returns a new portable workspace capturing current changes made through `.path`. Without a live materialization, returns a validated copy of the portable tree. |

Workspace model fields are frozen, but files under `.path` are writable. Value workspace inputs are isolated per node invocation; modifying one invocation's tree does not mutate another invocation's input. Returning the workspace captures writes automatically. Returned workspaces can be nested in tuples, lists, dictionary values, or Pydantic fields. Temporary paths are cleaned up after success or failure: do not retain/use them after the node ends. A returned workspace outside a node is portable data, not an accessible live path.

`from_path` rejects non-directory roots, symlinks, special files, unsafe reads, and detected concurrent changes with `ValueError`; filesystem failures can also propagate. Safe capture requires descriptor-anchored, no-follow filesystem support (`RuntimeError` when unavailable). Relative file paths may have at most eight components; directories count one additional component toward that limit. Permissions, timestamps, ownership, symlinks, and special-file metadata are not preserved. The same capture constraints apply when snapshotting edits, including at node return.

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
`.is_reference` is `True`; value workspaces return `None` and `False` respectively.
Reference workspaces have `entries == ()`; explicitly supplying both `entries`
and a reference is invalid, even with empty entries.

A bound reference's `snapshot()` retains the same workspace, directory, and
reference rather than capturing bytes. Local execution passes references through
unchanged, including supported nested return shapes. These are shared directories,
not isolated value copies: the host owns persistence, access, and concurrent writes.

Python/JSON model dumps and pickle state contain exactly
`{"kind": "workspace", "version": 1, "reference": "host-managed:example"}`,
never paths or entries. The untagged legacy form warns for one release.
Validating this dictionary (including with `from_manifest`) or unpickling gives an
**unbound** reference. Its `.path` and `snapshot()` raise
`RuntimeError("workspace reference is not bound to a directory")`. The original
bound workspace remains usable. A remote executor must resolve the reference and
construct `Workspace.from_directory(directory, reference=reference)` before code
accesses `.path`; serialization does not transfer a host-local directory binding.
Local DAGs use `LocalReferenceBinder` for registered local file capabilities.
Remote hosts supply their own authorized binder; see the reference guide.

For a repeatable 200 MB two-node write/hash comparison in separate processes, run
`uv run python scripts/benchmark_workspace.py reference` and
`uv run python scripts/benchmark_workspace.py value`. The script reports peak RSS
growth across each workflow and requires the reference run to stay below 50 MB.

## Input and output boundaries

Direct Python runs accept native File and Workspace values. File inputs are
lazy path references. `Workspace.from_path` still eagerly captures a value tree;
`from_directory` retains a host-managed reference without reading file contents.

Reference workspaces cross CLI/remote boundaries as the reference dictionary with no file attachments. Receiving code must bind the reference through the host before using its directory. Existing transport envelope and JSON size limits still apply.

The [CLI](cli.md) accepts file/directory input options and materializes requested output into a destination that must not already exist. Output file paths are generated safely rather than trusting `File.name` as a filesystem path. Without requested filesystem output, file results remain values rather than being written automatically.

For CLI/remote results, supported result values include JSON-compatible scalars, lists, tuples, string-keyed dictionaries, Pydantic models, files, and workspaces. Pydantic results are serialized using field aliases; their original Python model class is not preserved. Arbitrary Python objects supported by direct local runs are not necessarily transportable.

New File results contain references only, not byte attachments; their body size
does not count against the 4 MiB result JSON limit. The limit still applies to
metadata and value-workspace manifests. Legacy attachment bundles remain readable
with their previous size limits. File names are limited to 1,024 characters and
media types to 255. Invalid outputs fail rather than being silently truncated.
