"""Export the allowlisted release tree."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]

INCLUDE_PATHS = (
    "README.md",
    "ARCHITECTURE.md",
    ".gitignore",
    "Makefile",
    "pyproject.toml",
    "uv.lock",
    "docs/release-readiness.md",
    "docs/release-file-manifest.md",
    "scripts/export_release_tree.py",
    "examples/README.md",
    "examples/complex_dag_pattern.py",
    "examples/cursor_pattern.py",
    "examples/operator_workflow.py",
    "examples/stream_pattern.py",
    "src/avalanche",
    "src/ava_cli",
    "src/runtime",
    "src/tui",
    "test",
)

SKIP_NAMES = {
    "__pycache__",
    ".avalanche",
    ".claude",
    ".hermes",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".git",
    "dist",
    "htmlcov",
}

SKIP_SUFFIXES = {".pyc", ".pyo"}

TEXT_SUFFIXES = {
    "",
    ".cfg",
    ".css",
    ".gitignore",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".proto",
    ".py",
    ".pyi",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}

OLD_SURFACE_TERMS = ("ici" + "cle", "py" + "ici" + "cle", "i" + "cy")


def export_release_tree(dest: Path, *, clean: bool = False) -> list[Path]:
    """Copy allowlisted paths to dest and return copied files relative to dest."""
    dest = dest.resolve()
    if dest == REPO_ROOT or REPO_ROOT in dest.parents:
        raise ValueError("Destination must be outside the source repository")

    if dest.exists():
        if clean:
            shutil.rmtree(dest)
        elif any(dest.iterdir()):
            raise ValueError(f"Destination is not empty: {dest}")

    dest.mkdir(parents=True, exist_ok=True)

    for rel in INCLUDE_PATHS:
        source = REPO_ROOT / rel
        if not source.exists():
            raise FileNotFoundError(f"Allowlisted path is missing: {rel}")
        _copy_path(source, dest / rel)

    manifest = dest / ".release-tree-files.txt"
    manifest.write_text("")
    copied = _list_files(dest)
    manifest.write_text("\n".join(path.as_posix() for path in copied) + "\n")
    return copied


def check_release_surface(dest: Path) -> list[str]:
    """Return text files that still contain predecessor naming."""
    hits: list[str] = []
    for path in _iter_text_files(dest):
        text = path.read_text(errors="ignore").lower()
        if any(term in text for term in OLD_SURFACE_TERMS):
            hits.append(path.relative_to(dest).as_posix())
    return hits


def _copy_path(source: Path, dest: Path) -> None:
    if _should_skip(source):
        return
    if source.is_dir():
        for child in source.iterdir():
            _copy_path(child, dest / child.name)
        return

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)


def _should_skip(path: Path) -> bool:
    return path.name in SKIP_NAMES or path.suffix in SKIP_SUFFIXES


def _list_files(root: Path) -> list[Path]:
    return sorted(path.relative_to(root) for path in root.rglob("*") if path.is_file())


def _iter_text_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if path.is_file() and path.suffix in TEXT_SUFFIXES:
            yield path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export the allowlisted release tree.")
    parser.add_argument("--dest", required=True, type=Path, help="destination directory")
    parser.add_argument("--clean", action="store_true", help="remove destination first")
    parser.add_argument(
        "--skip-surface-check",
        action="store_true",
        help="copy files without checking old release-surface terms",
    )
    args = parser.parse_args(argv)

    files = export_release_tree(args.dest, clean=args.clean)
    if not args.skip_surface_check:
        hits = check_release_surface(args.dest.resolve())
        if hits:
            formatted = "\n".join(f"- {hit}" for hit in hits)
            raise SystemExit(f"Old release-surface terms found:\n{formatted}")

    print(f"Exported {len(files)} files to {args.dest}")
    print(f"Manifest: {args.dest / '.release-tree-files.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
