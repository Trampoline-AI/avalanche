"""Agent Skills: reusable instructions and files an agent loads when a task needs them.

``Skill`` follows the Agent Skills format (https://agentskills.io/specification):
a skill is a folder with a ``SKILL.md`` file, whose YAML frontmatter names and
describes the skill and whose Markdown body holds its instructions, plus any
other files the instructions refer to. Skills hold no tools; a step's functions
stay in its ``tools=``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Annotated, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

SKILL_FILE = "SKILL.md"

_FRONTMATTER_DELIMITER = "---"
# Frontmatter keys the specification defines, and the field each one fills.
_FRONTMATTER_FIELDS = {
    "name": "name",
    "description": "description",
    "license": "license",
    "compatibility": "compatibility",
    "metadata": "metadata",
    "allowed-tools": "allowed_tools",
}

SkillName = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
]


class SkillFormatError(ValueError):
    """A skill folder does not follow the Agent Skills format."""


class Skill(BaseModel):
    """One Agent Skill: a name, a description, instructions and resource files.

    ``name`` uses lowercase letters, digits and single hyphens, at most 64
    characters. ``description`` says what the skill does and when to use it; the
    agent sees it before deciding to load the skill. ``instructions`` is the
    Markdown body the agent reads when it uses the skill. ``files`` maps a path
    relative to the skill folder, such as ``"references/style.md"``, to the host
    file the agent may read there. ``license``, ``compatibility``, ``metadata``
    and ``allowed_tools`` are the specification's optional frontmatter fields.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    name: SkillName
    description: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    instructions: str
    files: Mapping[str, Path] = Field(default_factory=dict)
    license: str | None = None
    compatibility: Annotated[str, StringConstraints(min_length=1, max_length=500)] | None = None
    metadata: Mapping[str, str] = Field(default_factory=dict)
    allowed_tools: str | None = None

    @model_validator(mode="after")
    def _check_files(self) -> Self:
        for relative, host_path in self.files.items():
            parts = PurePosixPath(relative).parts
            if (
                not relative
                or "\\" in relative
                or relative.startswith("/")
                or PurePosixPath(relative).as_posix() != relative
                or any(part in {".", ".."} for part in parts)
            ):
                raise ValueError(
                    f"skill {self.name!r} file {relative!r} must be a relative path inside "
                    "the skill folder, written with '/' and without '.' or '..'"
                )
            if relative == SKILL_FILE:
                raise ValueError(
                    f"skill {self.name!r} file {SKILL_FILE} is generated from its name, "
                    "description and instructions; it cannot be a resource file"
                )
            if not host_path.is_file():
                raise ValueError(
                    f"skill {self.name!r} file {relative!r} is not a file: {host_path}"
                )
            for depth in range(1, len(parts)):
                folder = "/".join(parts[:depth])
                if folder in self.files:
                    raise ValueError(
                        f"skill {self.name!r} file {folder!r} is also the folder of "
                        f"{relative!r}"
                    )
        return self

    @classmethod
    def from_dir(cls, path: Path | str) -> Skill:
        """Load the skill in a folder holding a ``SKILL.md`` file.

        Every other file in the folder becomes a resource file. The frontmatter
        must hold only the specification's fields, and ``name`` must match the
        folder's name.
        """
        directory = Path(path).resolve()
        skill_file = directory / SKILL_FILE
        if not skill_file.is_file():
            raise SkillFormatError(f"skill folder {directory} has no {SKILL_FILE} file")
        frontmatter, body = _split_frontmatter(skill_file)
        unknown = sorted(set(frontmatter) - set(_FRONTMATTER_FIELDS), key=str)
        if unknown:
            raise SkillFormatError(
                f"{skill_file}: frontmatter has fields the Agent Skills specification does "
                f"not define: {unknown}; allowed fields are {sorted(_FRONTMATTER_FIELDS)}"
            )
        name = frontmatter.get("name")
        if name != directory.name:
            raise SkillFormatError(
                f"{skill_file}: skill name {name!r} must match its folder name "
                f"{directory.name!r}"
            )
        files = {
            file.relative_to(directory).as_posix(): file
            for file in sorted(directory.rglob("*"))
            if file.is_file() and file != skill_file
        }
        try:
            return cls.model_validate(
                {
                    **{_FRONTMATTER_FIELDS[key]: value for key, value in frontmatter.items()},
                    "instructions": body,
                    "files": files,
                }
            )
        except ValueError as exc:
            raise SkillFormatError(f"{skill_file}: {exc}") from exc

    def skill_md(self) -> str:
        """This skill's ``SKILL.md`` text: frontmatter, then the instructions."""
        frontmatter: dict[str, str | dict[str, str]] = {
            "name": self.name,
            "description": self.description,
        }
        if self.license is not None:
            frontmatter["license"] = self.license
        if self.compatibility is not None:
            frontmatter["compatibility"] = self.compatibility
        if self.metadata:
            frontmatter["metadata"] = dict(self.metadata)
        if self.allowed_tools is not None:
            frontmatter["allowed-tools"] = self.allowed_tools
        header = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
        delimiter = _FRONTMATTER_DELIMITER
        return f"{delimiter}\n{header}{delimiter}\n\n{self.instructions}\n"


def _split_frontmatter(skill_file: Path) -> tuple[Mapping[str, object], str]:
    """The frontmatter mapping and the Markdown body of a ``SKILL.md`` file."""
    lines = skill_file.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIMITER:
        raise SkillFormatError(f"{skill_file}: must start with a '---' frontmatter line")
    try:
        end = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == _FRONTMATTER_DELIMITER
        )
    except StopIteration:
        raise SkillFormatError(f"{skill_file}: frontmatter has no closing '---' line") from None
    try:
        frontmatter = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as exc:
        raise SkillFormatError(f"{skill_file}: frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(frontmatter, dict):
        raise SkillFormatError(f"{skill_file}: frontmatter must be a YAML mapping")
    return frontmatter, "\n".join(lines[end + 1 :]).strip()


__all__ = ["Skill", "SkillFormatError", "SkillName"]
