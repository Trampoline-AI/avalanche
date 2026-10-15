"""Compare peak RSS growth for reference and value workspaces in fresh processes.

Run each mode separately so resource.getrusage's lifetime high-water mark does not
carry over between trials::

    uv run python scripts/benchmark_workspace.py reference
    uv run python scripts/benchmark_workspace.py value

Each run writes and hashes a 200 MB file through a two-node local workflow. The
measurement starts immediately before Workflow.run() and ends after its result.
Imports, graph construction, and a 1 MiB write buffer precede the baseline. The
reference trial fails if peak RSS growth reaches 50 MB. MB means 1,000,000 bytes.
The script owns and removes its fixture directory; Avalanche does not own it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import resource
import sys
import tempfile
from pathlib import Path

import avalanche as ava
import avalanche.workspace as workspace_module


def peak_rss_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("reference", "value"))
    parser.add_argument("--size-mb", type=int, default=200)
    args = parser.parse_args()
    if args.size_mb <= 0:
        parser.error("--size-mb must be positive")
    size = args.size_mb * 1_000_000
    chunk = b"workspace-data\x00\xff" * (1024 * 1024 // 16)
    expected = hashlib.sha256()
    remaining = size
    while remaining:
        block = chunk[: min(remaining, len(chunk))]
        expected.update(block)
        remaining -= len(block)

    with tempfile.TemporaryDirectory(prefix="avalanche-workspace-benchmark-") as directory:
        root = Path(directory)
        workspace = (
            ava.Workspace.from_directory(root, reference="host-managed:benchmark")
            if args.mode == "reference"
            else ava.Workspace()
        )

        @ava.source
        def write() -> ava.Workspace:
            remaining = size
            with (workspace.path / "payload.bin").open("wb") as output:
                while remaining:
                    block = chunk[: min(remaining, len(chunk))]
                    output.write(block)
                    remaining -= len(block)
            return workspace

        @ava.step
        def digest(received: ava.Workspace) -> str:
            path = received.path / "payload.bin"
            assert path.stat().st_size == size
            checksum = hashlib.sha256()
            with path.open("rb") as source:
                while block := source.read(len(chunk)):
                    checksum.update(block)
            return checksum.hexdigest()

        @ava.workflow
        def flow():
            return digest(write())

        workflow = flow()
        executor = ava.LocalExecutor()
        before = peak_rss_bytes()
        result = workflow.run(executor=executor).result(timeout=300)
        after = peak_rss_bytes()
        assert result == expected.hexdigest(), "file digest mismatch"
        host_file_preserved = (root / "payload.bin").is_file()
        assert host_file_preserved == (args.mode == "reference")
        growth = after - before
        record = {
            "mode": args.mode,
            "file_bytes": size,
            "sha256": result,
            "rss_before_bytes": before,
            "rss_after_bytes": after,
            "rss_growth_bytes": growth,
            "rss_growth_mb": growth / 1_000_000,
            "reference_bound_bytes": 50_000_000,
            "reference_bound_passed": growth < 50_000_000 if args.mode == "reference" else None,
            "host_file_preserved": host_file_preserved,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "avalanche_version": ava.__version__,
            "workspace_source_sha256": hashlib.sha256(
                Path(workspace_module.__file__).read_bytes()
            ).hexdigest(),
        }
    record["fixture_removed"] = not root.exists()
    print(json.dumps(record, sort_keys=True), flush=True)
    if args.mode == "reference" and growth >= 50_000_000:
        raise SystemExit("reference workspace peak RSS growth must be below 50 MB")


if __name__ == "__main__":
    main()
