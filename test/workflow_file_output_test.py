"""Embedded file values survive workflow output handling without data loss."""

import hashlib

import pytest
from pydantic import BaseModel

import avalanche as ava


class FileBundle(BaseModel):
    label: str
    files: list[ava.File]


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


def test_large_file_roundtrip_checks_content_hash(tmp_path):
    content = b"x" * (4 * 1024 * 1024 + 1)
    digest = hashlib.sha256(content).hexdigest()
    path = tmp_path / "large.bin"
    path.write_bytes(content)

    file = ava.File.from_path(path)
    assert file.read_bytes() == content
    assert file.sha256 == digest
    assert ava.File(content=content, sha256=digest.upper()).sha256 == digest
    with pytest.raises(ValueError, match="sha256"):
        ava.File(content=content, sha256="0" * 64)
