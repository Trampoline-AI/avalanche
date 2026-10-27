"""ava.agent.Skill follows the Agent Skills format, from a folder or built in code."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

import avalanche as ava
from avalanche.agent.skills import Skill, SkillFormatError


def _write_skill(
    root: Path, name: str, skill_md: str, files: dict[str, str] | None = None
) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(skill_md)
    for relative, text in (files or {}).items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return folder


def test_from_dir_reads_frontmatter_instructions_and_every_other_file(tmp_path):
    folder = _write_skill(
        tmp_path,
        "pdf-processing",
        "---\n"
        "name: pdf-processing\n"
        "description: Extract PDF text. Use when handling PDFs.\n"
        "license: Apache-2.0\n"
        "compatibility: Requires pdftotext\n"
        "metadata:\n"
        "  author: example-org\n"
        '  version: "1.0"\n'
        "allowed-tools: Bash(pdftotext:*) Read\n"
        "---\n"
        "\n"
        "# PDF processing\n"
        "\n"
        "See references/REFERENCE.md.\n",
        {"references/REFERENCE.md": "reference", "scripts/extract.py": "print()"},
    )

    skill = Skill.from_dir(folder)

    assert skill.name == "pdf-processing"
    assert skill.description == "Extract PDF text. Use when handling PDFs."
    assert skill.instructions == "# PDF processing\n\nSee references/REFERENCE.md."
    assert dict(skill.files) == {
        "references/REFERENCE.md": folder / "references/REFERENCE.md",
        "scripts/extract.py": folder / "scripts/extract.py",
    }
    assert (skill.license, skill.compatibility, skill.allowed_tools) == (
        "Apache-2.0",
        "Requires pdftotext",
        "Bash(pdftotext:*) Read",
    )
    assert dict(skill.metadata) == {"author": "example-org", "version": "1.0"}


def test_a_minimal_skill_needs_only_a_name_and_description(tmp_path):
    folder = _write_skill(tmp_path, "hello", "---\nname: hello\ndescription: Greets.\n---\n")

    skill = Skill.from_dir(str(folder))

    assert (skill.instructions, dict(skill.files), skill.license) == ("", {}, None)


@pytest.mark.parametrize(
    ("folder_name", "skill_md", "message"),
    [
        ("x", "name: x\ndescription: d\n", "must start with a '---'"),
        ("x", "---\nname: x\ndescription: d\n", "no closing"),
        ("x", "---\nname: [x\n---\n", "not valid YAML"),
        ("x", "---\n- name\n---\n", "YAML mapping"),
        ("x", "---\ndescription: d\n---\n", "must match its folder"),
        ("x", "---\nname: y\ndescription: d\n---\n", "must match its folder"),
        ("x", "---\nname: x\n---\n", "description"),
        ("x", "---\nname: x\ndescription: ''\n---\n", "description"),
        ("x", f"---\nname: x\ndescription: {'d' * 1025}\n---\n", "description"),
        ("PDF", "---\nname: PDF\ndescription: d\n---\n", "name"),
        ("-pdf", "---\nname: -pdf\ndescription: d\n---\n", "name"),
        ("pdf-", "---\nname: pdf-\ndescription: d\n---\n", "name"),
        ("pdf--x", "---\nname: pdf--x\ndescription: d\n---\n", "name"),
        ("a" * 65, f"---\nname: {'a' * 65}\ndescription: d\n---\n", "name"),
        ("x", "---\nname: x\ndescription: d\ncompatibility: ''\n---\n", "compatibility"),
        (
            "x",
            f"---\nname: x\ndescription: d\ncompatibility: {'c' * 501}\n---\n",
            "compatibility",
        ),
        ("x", "---\nname: x\ndescription: d\nmetadata:\n  version: 1.0\n---\n", "metadata"),
        ("x", "---\nname: x\ndescription: d\nmetadata: plain\n---\n", "metadata"),
        ("x", "---\nname: x\ndescription: d\nallowed-tools: [Read]\n---\n", "allowed_tools"),
        ("x", "---\nname: x\ndescription: d\nmodel: opus\n---\n", r"\['model'\]"),
        ("x", "---\nname: x\ndescription: d\n1: one\n---\n", "does not define"),
    ],
)
def test_from_dir_rejects_what_the_specification_rejects(
    tmp_path, folder_name, skill_md, message
):
    folder = _write_skill(tmp_path, folder_name, skill_md)

    with pytest.raises(SkillFormatError, match=message):
        Skill.from_dir(folder)


def test_an_indented_rule_inside_a_yaml_value_does_not_end_the_frontmatter(tmp_path):
    description = "Use for reports.\n---\nMore text."
    folder = _write_skill(
        tmp_path,
        "reports",
        "---\nname: reports\ndescription: |-\n  Use for reports.\n  ---\n  More text.\n"
        "---\nWrite the report.\n",
    )

    skill = Skill.from_dir(folder)

    assert (skill.description, skill.instructions) == (description, "Write the report.")
    (folder / "SKILL.md").write_text(skill.skill_md())
    assert Skill.from_dir(folder).description == description


def test_from_dir_needs_a_skill_file(tmp_path):
    with pytest.raises(SkillFormatError, match="no SKILL.md"):
        Skill.from_dir(tmp_path)


def test_a_skill_built_in_code_writes_the_skill_file_it_loads_from(tmp_path):
    reference = tmp_path / "style.md"
    reference.write_text("Use short sentences.")
    skill = Skill(
        name="house-style",
        description="Applies the house style. Use when writing reports.",
        instructions="Follow references/style.md.",
        files={"references/style.md": reference},
        metadata={"owner": "docs"},
    )
    folder = tmp_path / "house-style"
    (folder / "references").mkdir(parents=True)
    (folder / "SKILL.md").write_text(skill.skill_md())
    (folder / "references" / "style.md").write_text(reference.read_text())

    loaded = Skill.from_dir(folder)

    assert loaded.model_dump(exclude={"files"}) == skill.model_dump(exclude={"files"})
    assert list(loaded.files) == ["references/style.md"]


def test_a_skill_is_frozen_and_exported_from_ava_agent():
    skill = ava.agent.Skill(name="hello", description="Greets.", instructions="Say hi.")

    assert ava.agent.skills.Skill is ava.agent.Skill is Skill
    with pytest.raises(ValidationError):
        skill.name = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    "relative",
    ["", "/abs.md", "../up.md", "a/../b.md", "./a.md", "a//b.md", "a\\b.md", "SKILL.md"],
)
def test_resource_paths_stay_inside_the_skill_folder(tmp_path, relative):
    source = tmp_path / "file.md"
    source.write_text("x")

    with pytest.raises(ValidationError, match="file"):
        Skill(name="x", description="d", instructions="", files={relative: source})


def test_resource_files_must_exist_and_not_be_another_files_folder(tmp_path):
    source = tmp_path / "file.md"
    source.write_text("x")

    with pytest.raises(ValidationError, match="not a file"):
        Skill(name="x", description="d", instructions="", files={"a.md": tmp_path / "gone"})
    with pytest.raises(ValidationError, match="also the folder"):
        Skill(name="x", description="d", instructions="", files={"a": source, "a/b.md": source})
