from pathlib import Path


def test_storage_examples_use_redirectable_gitignored_artifact_root():
    repo = Path(__file__).resolve().parents[1]
    example_files = [
        repo / "examples" / "cursor_pattern.py",
        repo / "examples" / "stream_pattern.py",
    ]

    for path in example_files:
        content = path.read_text()
        assert "AVALANCHE_EXAMPLE_ROOT" in content, path
        assert ".avalanche/examples/" in content, path
        assert "/tmp/" not in content, path

    gitignore = (repo / ".gitignore").read_text()
    assert ".avalanche/" in gitignore


def test_examples_release_surface_has_no_old_product_name_hits():
    repo = Path(__file__).resolve().parents[1]
    release_surface = [repo / "README.md", repo / "examples" / "README.md"]
    release_surface.extend(sorted((repo / "examples").glob("*.py")))
    old_product_name = "ici" + "cle"

    for path in release_surface:
        assert old_product_name not in path.read_text().lower(), path
