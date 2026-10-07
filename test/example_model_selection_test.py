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


def _enable_rotation_without_profiles(home: Path) -> None:
    # codex-lm raises ValueError for this unusable state, as for a disabled profile.
    state = home / ".codex-lm" / "rotation.json"
    state.parent.mkdir()
    state.write_text('{"enabled": true}', encoding="utf-8")


def test_unusable_codex_profile_falls_back_to_api_key(select_models, monkeypatch, tmp_path):
    _enable_rotation_without_profiles(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "k")

    assert select_models().provider == "openai"


def test_overrides_win_without_probing_codex(select_models, monkeypatch, tmp_path):
    _enable_rotation_without_profiles(tmp_path)
    monkeypatch.setenv("EXAMPLE_MODEL", "openai/custom")
    monkeypatch.setenv("EXAMPLE_SUB_MODEL", "openai/custom-mini")

    selection = select_models(lm_env="EXAMPLE_MODEL", sub_lm_env="EXAMPLE_SUB_MODEL")

    assert (selection.provider, selection.lm, selection.sub_lm) == (
        "override",
        "openai/custom",
        "openai/custom-mini",
    )


def test_single_override_keeps_automatic_choice_for_the_other_model(select_models, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("EXAMPLE_SUB_MODEL", "openai/custom-mini")

    selection = select_models(lm_env="EXAMPLE_MODEL", sub_lm_env="EXAMPLE_SUB_MODEL")

    assert (selection.provider, selection.sub_lm) == ("anthropic", "openai/custom-mini")
    assert selection.lm.startswith("anthropic/")
