"""Embedded workflow result behavior for public File values."""

from pydantic import BaseModel

import avalanche as ava


class FileBundle(BaseModel):
    label: str
    files: list[ava.File]


def test_run_handle_result_returns_direct_file_without_wrapping():
    @ava.source
    def build_file():
        return ava.File(
            name="summary.txt",
            content=b"embedded",
            content_type="text/plain",
        )

    @ava.workflow
    def file_workflow():
        return build_file()

    result = file_workflow().run(executor=ava.LocalExecutor()).result()

    assert isinstance(result, ava.File)
    assert result.name == "summary.txt"
    assert result.content_type == "text/plain"
    assert result.read_bytes() == b"embedded"


def test_run_handle_result_preserves_files_nested_in_pydantic_and_containers():
    @ava.source
    def build_bundle():
        return {
            "bundle": FileBundle(
                label="documents",
                files=[ava.File(name="one.txt", content=b"one")],
            ),
            "tail": (ava.File(name="two.txt", content=b"two"), 2),
        }

    @ava.workflow
    def bundle_workflow():
        return build_bundle()

    result = bundle_workflow().run(executor=ava.LocalExecutor()).result()

    assert isinstance(result["bundle"], FileBundle)
    assert isinstance(result["bundle"].files[0], ava.File)
    assert result["bundle"].files[0].read_bytes() == b"one"
    assert isinstance(result["tail"], tuple)
    assert isinstance(result["tail"][0], ava.File)
    assert result["tail"][0].read_bytes() == b"two"
