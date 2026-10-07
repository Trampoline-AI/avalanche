from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture
def select_models(monkeypatch, tmp_path):
    # Isolate Codex LM auth (resolved under HOME) and the project .env lookup.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    for name in (
        "CODEX_LM_AUTH_PROFILE",
        "CODEX_LM_ENABLE_LEGACY_AUTH_FALLBACK",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.syspath_prepend(str(EXAMPLES))
    from model_selection import select_models

    return select_models


def _log_in_to_codex(monkeypatch, home: Path) -> None:
    auth = home / ".codex" / "auth.json"
    auth.parent.mkdir()
    auth.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CODEX_LM_ENABLE_LEGACY_AUTH_FALLBACK", "1")


@pytest.mark.parametrize(
    ("codex", "env", "provider"),
    [
        (True, {"OPENAI_API_KEY": "k", "ANTHROPIC_API_KEY": "k"}, "codex"),
        (False, {"OPENAI_API_KEY": "k", "ANTHROPIC_API_KEY": "k"}, "openai"),
        (False, {"ANTHROPIC_API_KEY": "k"}, "anthropic"),
        (False, {}, "codex"),
    ],
)
def test_select_models_prefers_codex_then_openai_then_anthropic(
    select_models, monkeypatch, tmp_path, codex, env, provider
):
    if codex:
        _log_in_to_codex(monkeypatch, tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    selection = select_models()

    assert selection.provider == provider
    if provider == "openai":
        assert selection.lm.startswith("openai/")
    if provider == "anthropic":
        assert selection.lm.startswith("anthropic/")


def test_select_models_reads_keys_from_project_dotenv(select_models, monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=from-dotenv\n", encoding="utf-8")

    assert select_models().provider == "anthropic"
