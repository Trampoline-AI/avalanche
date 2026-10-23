# File references

`ava.File` identifies a file in a workspace. Its body is never embedded in run
JSON or new operator result attachments. The same public type is used by Python
steps and agent signatures.

```python
from pathlib import Path
import avalanche as ava

file = ava.File.from_path(Path("report.pdf"), content_type="application/pdf")
print(file.size)                 # stat only; no file read yet
with file.open("rb") as handle:  # a real streaming file handle
    header = handle.read(4)
body = file.read_bytes()         # explicit whole-file materialization
wire = file.model_dump()         # streams SHA-256; never embeds the body or host path
```

`open()` accepts only `r`, `rt`, and `rb`; write, append, exclusive-create and
update modes raise `ValueError`. `.path` is the bound host `Path`, not a serialized
field. Read-only handles are an API restriction, not kernel-enforced isolation:
Python code with access to the host path can open it separately.

`File.from_path(path, *, content_type=None, workspace=None)` binds without reading.
The optional workspace must be a bound reference workspace containing the path.
Without it, Avalanche registers a local workspace capability for the parent
directory. `snapshot()` streams and verifies size/SHA-256 before publication.
Changing bound content after its identity has been established raises an error
when snapshotting/serializing; create a new File for changed content.

## Wire contract

```json
{
  "kind": "file",
  "version": 1,
  "ref": {"workspace": "delta-ws:v1:snapshot-id", "path": ".inputs/document/report.pdf"},
  "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "size": 12345,
  "name": "report.pdf",
  "content_type": "application/pdf"
}
```

`ref` has one target discriminator, `workspace`, and its relative POSIX `path`.
Only those two keys are accepted. Absolute paths, traversal, backslashes, missing
SHA-256, unknown keys, and prospective `object` targets are rejected. The outer
`kind` and version are required on wire values. No shape-based type detection is
used for ordinary dictionaries.

Reference workspaces serialize as:

```json
{"kind": "workspace", "version": 1, "reference": "delta-ws:v1:snapshot-id"}
```

The old untagged `{"version": 1, "reference": "..."}` form is accepted with
`DeprecationWarning` for this release only. Value workspace manifests are
unchanged; unlike File references, they still explicitly carry their tree data.

Deserialized File/Workspace values are unbound. Accessing an unbound `.path`,
`open()`, or `read_bytes()` fails clearly. A reference contains identity, not
permission to open an arbitrary host path.

## Host integration: rebuild arguments at every node

Import these APIs from `avalanche` (or `avalanche.references`):

```python
class ReferenceBinder(Protocol):
    def bind_file(self, reference: ava.File) -> Path: ...
    def bind_workspace(self, reference: ava.Workspace) -> Path: ...

ava.rebind_arguments(fn, resolved_kwargs, binder)  # returns rebuilt keyword arguments
```

`rebind_arguments` uses the function's declared parameter annotations to validate
File, Workspace, Pydantic models and typed collections, then recursively binds
references. Call it **for every node after resolving upstream values**, not just
for run inputs. A custom binder is consulted even for already bound native refs;
it must authorize the workspace snapshot against the current run/ancestry,
reject path escapes, and return existing host paths. Avalanche checks file size
when binding. The host owns snapshot integrity, mount permissions and lifetime.
Unannotated parameters retain their values; they are not inferred from dict shape.

For example, a Delta host can resolve a workspace ID to an authorized Mesa mount,
join `reference.ref.path` beneath it, and return that file from `bind_file`.
`bind_workspace` returns the corresponding authorized root. Do not deserialize a
client-supplied reference directly into a host path without authorization.

Local DAG execution calls the same helper at every step. `LocalReferenceBinder()`
resolves only registered `local-ws:v1:<opaque-id>` capabilities on the same host;
it is not a remote snapshot resolver. The POSIX user's private temporary registry
allows the loopback operator's spawned processes and CLI to share refs without
putting host paths on the wire. These local references and local run folders have
temporary host lifetime, not durable archive guarantees; deleting the source or
temporary storage invalidates them. Remote consumers supply their own binder.

## Agent files

Declare `ava.File` and `ava.Workspace` in `ava.Signature`. Avalanche translates
these internally to PredictRLM path types, File mount adapters and read-only
direct Workspace inputs. The public signature and returned files stay Avalanche
types. Input File lists/nullable lists use the same adapter. Mounts never silently
fall back to copying: unsupported JSPI and pooled/named SBX backends fail closed.
DirectPythonBackend passes through host input paths; its read-only setting is
advisory. Use the host's isolation boundary for enforcement.

The host sets the output workspace around a node invocation:

```python
workspace = ava.Workspace.from_directory("/ws", reference="delta-ws:v1:seed")
with ava.file_reference_context(workspace, attempt="attempt-id"):
    kwargs = ava.rebind_arguments(fn, resolved_kwargs, binder)
    result = fn(**kwargs)  # await here for async nodes, before exiting the context
```

`file_reference_context(workspace, *, attempt=None)` requires a bound reference
workspace. Outputs are reserved beneath
`.outputs/<step>/<attempt>/<invocation>/` and passed to PredictRLM as `output_dir`.
A fresh invocation UUID prevents multiple agent calls within one attempt from
overwriting earlier file references. The default attempt is a fresh UUID. Each returned
PredictRLM File path is checked against that reservation and converted to
`ava.File` with the run-workspace reference. The host commits its snapshot and
rewrites snapshot identity as needed. Without a host context, embedded execution
uses a local run workspace keyed by `RunContext.run_id`.

The agent owns its output reservation: do not pass an independent `output_dir`
to file-bearing agent steps. PredictRLM still expects submitted output paths in
its reserved sandbox namespace; DirectPythonBackend rewrites `/sandbox` paths in
generated code, so preserve the virtual path when submitting an output.

## Migration for 0.14.0

- Replace `File(content=data)`/byte inputs with a written file and `File.from_path`.
  Byte constructors remain only as deprecated, temporary-file-backed references
  until the next minor release. No byte-valued File variant is emitted.
- Replace `file.content` with explicit `file.read_bytes()` (the compatibility
  property also warns and will be removed next minor).
- Replace `ava.agent.File` and direct PredictRLM File imports in Avalanche flows
  with `ava.File`. The deprecated `ava.agent.File` name now resolves to `ava.File`.
- Operator clients put file references in input JSON; new result publications
  contain references and no file attachments, regardless of file body size.
  The operator can still read legacy attachment bundles during migration.
- CLI file inputs use `File.from_path` without reading; result export streams
  files. Bound reference workspace export copies the actual tree, not its empty
  `entries` field. Unbound foreign references require host binding before export.
