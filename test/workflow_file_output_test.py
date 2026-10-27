"""Embedded file values survive workflow output handling without data loss."""

import hashlib

import pytest
from pydantic import BaseModel

import avalanche as ava


class FileBundle(BaseModel):
    label: str
    files: list[ava.File]


def test_agent_file_mount_and_reserved_outputs(tmp_path):
    """Use the real PredictRLM adapters and DirectPython backend without a network LM."""
    from dspy import JSONAdapter
    from dspy.utils import DummyLM
    from predict_rlm.backends import DirectPythonBackend

    from avalanche.agent import capture_agent_evidence
    from avalanche.agent.live import AgentTraceFinished
    from avalanche.agent.trace import file_input_evidence

    class Convert(ava.Signature):
        document: ava.File = ava.InputField()
        directory: ava.Workspace = ava.InputField()
        documents: list[ava.File | None] = ava.InputField()
        result: ava.File = ava.OutputField()

    source = tmp_path / "input.pdf"
    source.write_bytes(b"%PDF-\xff")
    workspace = ava.Workspace.from_directory(tmp_path, reference="snapshot:test")
    file = ava.File.from_path(source, workspace=workspace)
    lm = DummyLM(
        [
            {
                "plan": "Read the mounted input and write the reserved output.",
                "code": (
                    "from pathlib import Path\n"
                    f"assert document == {str(source)!r}\n"
                    f"assert directory == {str(tmp_path)!r}\n"
                    "assert documents == [document, None]\n"
                    "p = Path('/sandbox/output/result/converted.pdf')\n"
                    "p.parent.mkdir(parents=True, exist_ok=True)\n"
                    "p.write_bytes(Path(document).read_bytes() + b'converted')\n"
                    "SUBMIT(result='/sand' + 'box/output/result/converted.pdf')"
                ),
            }
        ]
        * 2,
        adapter=JSONAdapter(),
    )

    @ava.agent_step(
        Convert,
        runtime=ava.runtimes.PredictRLM(
            lm=lm,
            sub_lm=lm,
            interpreter=DirectPythonBackend(
                workdir=str(tmp_path),
                runner_path=str(tmp_path / "runner.py"),
            ),
            max_iterations=1,
        ),
    )
    async def convert(document: ava.File, *, agent: ava.Agent) -> ava.File:
        first = await agent(document=document, directory=workspace, documents=[document, None])
        second = await agent(document=document, directory=workspace, documents=[document, None])
        assert first.outputs.result.path != second.outputs.result.path
        assert first.outputs.result.read_bytes() == second.outputs.result.read_bytes()
        return second.outputs.result

    @ava.step
    def consume(converted: ava.File) -> ava.File:
        assert converted.read_bytes().endswith(b"converted")
        return converted

    @ava.workflow
    def flow():
        return consume(convert(file))

    observed = []
    with (
        capture_agent_evidence(observed.append, errors="raise"),
        ava.file_reference_context(workspace, attempt="attempt-1"),
    ):
        result = flow().run(executor=ava.LocalExecutor()).result(timeout=20)
    assert isinstance(result, ava.File)
    # The final event describes the output as the reference the step returned.
    succeeded = [event for event in observed if isinstance(event, AgentTraceFinished)]
    assert succeeded[-1].outputs["result"] == file_input_evidence(result).model_dump(
        mode="json"
    )
    assert str(tmp_path) not in succeeded[-1].model_dump_json()
    assert result.path.name == "converted.pdf"
    assert result.path.parent.name == "result"
    assert result.path.is_relative_to(tmp_path / ".outputs" / "convert" / "attempt-1")
    assert result.read_bytes() == b"%PDF-\xffconverted"
    assert result.ref.workspace == "snapshot:test"
    assert result.ref.path.startswith(".outputs/convert/attempt-1/")
    assert result.ref.path.endswith("/result/converted.pdf")
    # The source ref remains streaming/read-only, and never became a byte payload.
    assert file.path == source
    assert source.read_bytes() == b"%PDF-\xff"


def test_result_preserves_direct_files_and_files_nested_in_models_and_containers():
    @ava.source
    def build_file():
        return ava.File(name="summary.txt", content=b"embedded", content_type="text/plain")

    @ava.step
    def bundle(file):
        return {
            "bundle": FileBundle(label="documents", files=[file]),
            "tail": (ava.File(name="two.txt", content=b"two"), 2),
        }

    @ava.workflow
    def flow():
        file = build_file()
        return file, bundle(file)

    file, result = flow().run(executor=ava.LocalExecutor()).result(timeout=5)
    assert file.read_bytes() == b"embedded"
    assert file.name == "summary.txt"
    assert file.content_type == "text/plain"
    assert isinstance(result["bundle"], FileBundle)
    assert result["bundle"].files[0].read_bytes() == b"embedded"
    assert isinstance(result["tail"], tuple)
    assert result["tail"][0].read_bytes() == b"two"


def test_large_file_roundtrip_checks_content_hash(tmp_path, monkeypatch):
    content = b"\xff" * (8 * 1024 * 1024 + 1)
    digest = hashlib.sha256(content).hexdigest()
    path = tmp_path / "large.bin"
    path.write_bytes(content)

    def no_read_bytes(self):
        raise AssertionError("from_path/serialization must stream, not materialize")

    monkeypatch.setattr(type(path), "read_bytes", no_read_bytes)
    file = ava.File.from_path(path)
    assert file.sha256 is None
    assert file.path == path
    assert file.size == len(content)
    wire = file.model_dump()
    assert wire["kind"] == "file"
    assert wire["sha256"] == digest
    assert "content" not in wire
    assert str(tmp_path) not in str(wire)
    restored = ava.File.model_validate(wire)
    with pytest.raises(RuntimeError, match="not bound"):
        restored.open()
    for mode in ("w", "wb", "a", "x", "r+", "w+b"):
        with pytest.raises(ValueError, match="read"):
            file.open(mode)
    with file.open() as handle:
        assert handle.read(3) == b"\xff" * 3
        assert handle.fileno() >= 0
    with pytest.warns(DeprecationWarning):
        legacy = ava.File(content=content, sha256=digest.upper())
    assert legacy.read_bytes() == content
    assert legacy.sha256 == digest
    with pytest.warns(DeprecationWarning), pytest.raises(ValueError, match="sha256"):
        ava.File(content=content, sha256="0" * 64)


def test_file_reference_validation_and_per_node_rebinding(tmp_path):
    path = tmp_path / "input.pdf"
    path.write_bytes(b"%PDF-\xff")
    file = ava.File.from_path(path)
    wire = file.model_dump()
    for ref in (
        {"object": "a" * 64},
        {**wire["ref"], "unknown": "x"},
        {"workspace": "snapshot", "path": "../escape"},
    ):
        with pytest.raises(ValueError):
            ava.File.model_validate({**wire, "ref": ref})
    with pytest.raises(ValueError):
        ava.File.model_validate({**wire, "kind": "workspace"})

    class Binder:
        def bind_file(self, reference: ava.File):
            assert reference.ref.workspace == wire["ref"]["workspace"]
            return path

        def bind_workspace(self, reference: ava.Workspace):
            return tmp_path

    def consume(bundle: FileBundle, files: list[ava.File], workspace: ava.Workspace):
        return bundle, files, workspace

    args = ava.rebind_arguments(
        consume,
        {
            "bundle": {"label": "typed", "files": [wire]},
            "files": [wire],
            "workspace": {"kind": "workspace", "version": 1, "reference": "snapshot"},
        },
        Binder(),
    )
    assert args["bundle"].files[0].path == path
    assert args["files"][0].read_bytes() == b"%PDF-\xff"
    assert args["workspace"].path == tmp_path

    @ava.source
    def produce():
        return wire

    @ava.step
    def read(file: ava.File):
        return file.read_bytes()

    @ava.workflow
    def flow():
        return read(produce())

    assert flow().run(executor=ava.LocalExecutor()).result(timeout=5) == b"%PDF-\xff"


def test_agent_file_step_traces_record_input_references_not_mounted_paths(tmp_path):
    from dspy import JSONAdapter
    from dspy.utils import DummyLM
    from predict_rlm.backends import DirectPythonBackend

    from avalanche.agent import capture_agent_evidence
    from avalanche.agent.live import AgentLiveEvent, RunStarted

    class Count(ava.Signature):
        document: ava.File = ava.InputField()
        size: int = ava.OutputField()

    source = tmp_path / "input.txt"
    source.write_bytes(b"hello")
    workspace = ava.Workspace.from_directory(tmp_path, reference="snapshot:test")
    file = ava.File.from_path(source, workspace=workspace)
    lm = DummyLM(
        [{"plan": "Count bytes.", "code": "SUBMIT(size=len(open(document, 'rb').read()))"}],
        adapter=JSONAdapter(),
    )

    @ava.agent_step(
        Count,
        runtime=ava.runtimes.PredictRLM(
            lm=lm,
            sub_lm=lm,
            interpreter=DirectPythonBackend(
                workdir=str(tmp_path),
                runner_path=str(tmp_path / "runner.py"),
            ),
            max_iterations=1,
        ),
    )
    async def count(document: ava.File, *, agent: ava.Agent) -> int:
        return (await agent(document=document)).outputs.size

    @ava.workflow
    def flow():
        return count(file)

    observed = []
    with (
        capture_agent_evidence(observed.append, errors="raise"),
        ava.file_reference_context(workspace, attempt="attempt-1"),
    ):
        assert flow().run(executor=ava.LocalExecutor()).result(timeout=20) == 5

    started = next(
        event.common
        for event in observed
        if isinstance(event, AgentLiveEvent) and isinstance(event.common, RunStarted)
    )
    assert started.inputs["document"]["workspace"] == "snapshot:test"
    assert started.inputs["document"]["path"] == "input.txt"
    assert str(tmp_path) not in started.model_dump_json()
