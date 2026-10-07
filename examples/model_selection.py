"""Pick example agent models from whichever credentials this machine has.

Per-example override variables win when set. Otherwise, in preference order:

1. Codex LM, when a usable ``codex-lm`` auth profile resolves
   (``codex-lm auth login NAME``). A disabled or broken profile counts as unavailable.
2. OpenAI, when ``OPENAI_API_KEY`` is set.
3. Anthropic, when ``ANTHROPIC_API_KEY`` is set.

Keys are read from the environment or the project ``.env``. With none of these
available, Codex LM stays selected so the first agent call fails with Codex LM's
own setup error instead of a vague provider error.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from dotenv import find_dotenv, load_dotenv
from dspy import LM
from dspy_codex_lm import CodexLM
from dspy_codex_lm.auth import resolve_auth_source

CODEX_MODEL = "gpt-5.6-terra"
OPENAI_MODEL = "openai/gpt-5.6-terra"
ANTHROPIC_MODEL = "anthropic/claude-sonnet-5-5"

Provider = Literal["override", "codex", "openai", "anthropic"]


@dataclass(frozen=True)
class ModelSelection:
    provider: Provider
    lm: LM | str
    sub_lm: LM | str


def codex_is_configured() -> bool:
    try:
        source = resolve_auth_source(advance_rotation=False)
    # FileNotFoundError: no profile selected. ValueError: the selected profile is
    # disabled, rotation has no enabled profiles, or the profile name is invalid.
    except (FileNotFoundError, ValueError):
        return False
    return source.path.is_file()


def select_models(
    *, lm_env: str | None = None, sub_lm_env: str | None = None
) -> ModelSelection:
    """Return the main and sub models, honoring the named override variables first."""
    # Agent LMs read keys from the process environment; load the project .env the
    # same way classifier steps do, without overriding exported values.
    load_dotenv(find_dotenv(usecwd=True), override=False)
    lm = os.environ.get(lm_env) if lm_env is not None else None
    sub_lm = os.environ.get(sub_lm_env) if sub_lm_env is not None else None
    if lm and sub_lm:
        return ModelSelection("override", lm, sub_lm)
    selected = _auto_select()
    return ModelSelection(selected.provider, lm or selected.lm, sub_lm or selected.sub_lm)


def _auto_select() -> ModelSelection:
    if codex_is_configured():
        return _codex()
    if os.environ.get("OPENAI_API_KEY"):
        return ModelSelection("openai", OPENAI_MODEL, OPENAI_MODEL)
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ModelSelection("anthropic", ANTHROPIC_MODEL, ANTHROPIC_MODEL)
    return _codex()


def _codex() -> ModelSelection:
    return ModelSelection("codex", CodexLM(model=CODEX_MODEL), CodexLM(model=CODEX_MODEL))
