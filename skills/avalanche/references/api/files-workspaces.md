# Files and workspaces

```python
import avalanche as ava
```

Use these values in [input models](inputs-context.md#input-and-context-declarations), node arguments, and workflow results. A `File` carries one in-memory byte body; a `Workspace` carries a portable directory tree and exposes a temporary working path inside nodes.

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

Author-facing construction forms (not the complete model constructor):

```python
ava.Workspace()                              # empty tree
ava.Workspace.from_path(path: str | pathlib.Path) -> ava.Workspace
workspace.path -> pathlib.Path
workspace.snapshot() -> ava.Workspace
```

| API | Contract |
| --- | --- |
| `Workspace()` | Returns an empty portable tree. Empty directories are supported. |
| `Workspace.from_path(path)` | Captures a directory tree and all file bytes immediately; later edits to the source directory do not change it. |
| `workspace.path` | Lazily materializes a writable temporary directory while user node code is executing. Repeated accesses reuse that invocation's directory. Outside node execution, raises `RuntimeError`. |
| `workspace.snapshot()` | Returns a new portable workspace capturing current changes made through `.path`. Without a live materialization, returns a validated copy of the portable tree. |

Workspace model fields are frozen, but files under `.path` are writable. Inputs are isolated per node invocation; modifying one invocation's tree does not mutate another invocation's input. Returning the workspace captures writes automatically. Returned workspaces can be nested in tuples, lists, dictionary values, or Pydantic fields. Temporary paths are cleaned up after success or failure: do not retain/use them after the node ends. A returned workspace outside a node is portable data, not an accessible live path.

`from_path` rejects non-directory roots, symlinks, special files, unsafe reads, and detected concurrent changes with `ValueError`; filesystem failures can also propagate. Safe capture requires descriptor-anchored, no-follow filesystem support (`RuntimeError` when unavailable). Relative file paths may have at most eight components; directories count one additional component toward that limit. Permissions, timestamps, ownership, symlinks, and special-file metadata are not preserved. The same capture constraints apply when snapshotting edits, including at node return.

## Input and output boundaries

Direct Python runs accept native `File` and `Workspace` values without remote transport limits. A `File` is fully in memory and `Workspace.from_path` eagerly captures its tree, so neither is a lazy reference to a source path.

The [CLI](cli.md) accepts file/directory input options and materializes requested output into a destination that must not already exist. Output file paths are generated safely rather than trusting `File.name` as a filesystem path. Without requested filesystem output, file results remain values rather than being written automatically.

For CLI/remote results, supported result values include JSON-compatible scalars, lists, tuples, string-keyed dictionaries, Pydantic models, files, and workspaces. Pydantic results are serialized using field aliases; their original Python model class is not preserved. Arbitrary Python objects supported by direct local runs are not necessarily transportable.

Transported results are bounded: at most 8 MiB per file, 1,024 file attachments, 32 MiB total attachment bytes, 4 MiB encoded JSON, and 32 MiB JSON plus attachments. File names are limited to 1,024 characters and media types to 255. These are output transport constraints, not `File` constructor limits. Invalid/oversized outputs fail rather than being silently truncated.
