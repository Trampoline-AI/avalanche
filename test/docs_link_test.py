from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LINK_PATTERN = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
USER_FACING_DOCS = (
    "README.md",
    "ARCHITECTURE.md",
    "CONTRIBUTING.md",
    "docs/getting-started.md",
    "examples/README.md",
)
HISTORICAL_ROOTS = ("doc/", "spec/", "journal/", "scratch/", "poc/", "tasks/")


def test_local_markdown_links_resolve():
    missing: list[str] = []

    for markdown_path in REPO_ROOT.rglob("*.md"):
        if any(part.startswith(".") for part in markdown_path.relative_to(REPO_ROOT).parts):
            continue
        for match in LINK_PATTERN.finditer(markdown_path.read_text()):
            target = match.group(1).strip()
            if _is_external_or_anchor(target):
                continue
            target_path = _resolve_link(markdown_path, target)
            if not target_path.exists():
                rel = markdown_path.relative_to(REPO_ROOT).as_posix()
                missing.append(f"{rel}: {target}")

    assert missing == []


def test_user_facing_docs_do_not_point_to_historical_roots():
    hits: list[str] = []

    for rel in USER_FACING_DOCS:
        text = (REPO_ROOT / rel).read_text()
        for root in HISTORICAL_ROOTS:
            if root in text:
                hits.append(f"{rel}: {root}")

    assert hits == []


def _is_external_or_anchor(target: str) -> bool:
    return (
        target.startswith("http://")
        or target.startswith("https://")
        or target.startswith("mailto:")
        or target.startswith("#")
    )


def _resolve_link(markdown_path: Path, target: str) -> Path:
    path_part = target.split("#", 1)[0]
    return (markdown_path.parent / path_part).resolve()
