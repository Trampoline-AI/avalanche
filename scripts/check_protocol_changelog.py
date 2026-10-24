"""Require an explicit release note when the Delta protocol major changes.

Read the checked-out candidate and the base's source statically; never import Avalanche.
"""

import argparse
import ast
import re
import subprocess
import tomllib
from pathlib import Path

PROTOCOL_PATH = "src/avalanche/protocol.py"


def read_protocol(source: str) -> tuple[int, int]:
    """Read the literal ProtocolVersion(major, minor) declaration without execution."""
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "PROTOCOL_VERSION"
            for target in targets
        ):
            continue
        if (
            not isinstance(value, ast.Call)
            or not isinstance(value.func, ast.Name)
            or value.func.id != "ProtocolVersion"
            or len(value.args) != 2
            or value.keywords
        ):
            break
        major, minor = value.args
        if (
            isinstance(major, ast.Constant)
            and type(major.value) is int
            and major.value >= 0
            and isinstance(minor, ast.Constant)
            and type(minor.value) is int
            and minor.value >= 0
        ):
            return major.value, minor.value
        break
    raise ValueError(f"{PROTOCOL_PATH} must declare PROTOCOL_VERSION = ProtocolVersion(M, m)")


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True)


def has_breaking_note(changelog: str, version: str) -> bool:
    """Only Unreleased or this exact package version may acknowledge the change."""
    eligible = False
    for line in changelog.splitlines():
        heading = re.fullmatch(r"##\s+(.+)", line)
        if heading:
            title = heading.group(1).split()[0].strip("[]")
            eligible = title in {"Unreleased", version}
        elif eligible and "BREAKING-PROTOCOL" in line:
            return True
    return False


def check(root: Path, base: str) -> int:
    # Resolve first so a bad/missing ref cannot be mistaken for the initial baseline.
    base_commit = git(root, "rev-parse", "--verify", f"{base}^{{commit}}").strip()
    current = read_protocol((root / PROTOCOL_PATH).read_text())
    if not git(root, "ls-tree", "--name-only", base_commit, "--", PROTOCOL_PATH).strip():
        print(
            f"Protocol {current[0]}.{current[1]}: no protocol file at {base}; "
            "baseline accepted."
        )
        return 0
    previous = read_protocol(git(root, "show", f"{base_commit}:{PROTOCOL_PATH}"))
    if current[0] == previous[0]:
        print(f"Protocol {current[0]}.{current[1]}: major unchanged from {base}.")
        return 0
    with (root / "pyproject.toml").open("rb") as file:
        version = tomllib.load(file)["project"]["version"]
    if not isinstance(version, str):
        raise ValueError("pyproject.toml project.version must be a string")
    if not has_breaking_note((root / "CHANGELOG.md").read_text(), version):
        print(
            f"::error::Protocol major changed {previous[0]} -> {current[0]}; "
            f"add a BREAKING-PROTOCOL line to CHANGELOG.md under ## Unreleased or ## {version}."
        )
        return 1
    print(
        f"Protocol major changed {previous[0]} -> {current[0]}: BREAKING-PROTOCOL note found."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="Git ref to compare against the checkout")
    args = parser.parse_args()
    try:
        return check(Path(__file__).resolve().parents[1], args.base)
    except (OSError, ValueError, SyntaxError, subprocess.CalledProcessError) as error:
        print(f"::error::Cannot check protocol changelog: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
