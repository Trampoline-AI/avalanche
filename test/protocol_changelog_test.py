"""Exercise the release gate against real git history without importing Avalanche."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_protocol_changelog.py"


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def _write_protocol(root: Path, major: int, minor: int) -> None:
    path = root / "src" / "avalanche" / "protocol.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Importing this fixture is deliberately impossible; the gate must use static parsing.
    path.write_text(
        "raise RuntimeError('do not import')\n"
        f"PROTOCOL_VERSION = ProtocolVersion({major}, {minor})\n"
    )


@pytest.mark.parametrize(
    "base_version,head_version,changelog,expected",
    [
        (None, (1, 0), "## Unreleased\n", 0),
        ((1, 0), (1, 0), "## Unreleased\n", 0),
        ((1, 0), (1, 1), "## Unreleased\n", 0),
        ((1, 0), (2, 0), "## Unreleased\n", 1),
        ((2, 0), (1, 0), "## Unreleased\n", 1),
        ((1, 0), (2, 0), "## Unreleased\n### Protocol\n- BREAKING-PROTOCOL: changed\n", 0),
        ((1, 0), (2, 0), "## Unreleased\n## 0.15.0 — today\n- BREAKING-PROTOCOL: changed\n", 0),
        ((1, 0), (2, 0), "## Unreleased\n## 0.14.0\n- BREAKING-PROTOCOL: old\n", 1),
        ((1, 0), (2, 0), "## Unreleased\n## 0.15.0rc1\n- BREAKING-PROTOCOL: other\n", 1),
    ],
)
def test_protocol_changelog_gate(
    tmp_path: Path,
    base_version: tuple[int, int] | None,
    head_version: tuple[int, int],
    changelog: str,
    expected: int,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Protocol gate test")
    _git(root, "config", "user.email", "protocol-test@example.invalid")
    (root / "pyproject.toml").write_text('[project]\nversion = "0.15.0"\n')
    if base_version is not None:
        _write_protocol(root, *base_version)
    _git(root, "add", ".")
    _git(root, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "baseline")
    base = _git(root, "rev-parse", "HEAD")
    _write_protocol(root, *head_version)
    (root / "CHANGELOG.md").write_text(changelog)
    scripts = root / "scripts"
    scripts.mkdir()
    shutil.copyfile(SCRIPT, scripts / SCRIPT.name)
    result = subprocess.run(
        [sys.executable, str(scripts / SCRIPT.name), "--base", base],
        cwd=root,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == expected, result.stdout + result.stderr
    assert ("::error::" in result.stdout) == (expected != 0)
