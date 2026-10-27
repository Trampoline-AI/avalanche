"""Four Claude agent steps that build Office files, in series and in parallel.

A writer drafts a Word memo, two designers turn that memo into competing PowerPoint
decks at the same time, and a judge reads both decks, writes a scorecard and picks
a winner. Every agent runs on the ClaudeAgent runtime and works on real files with
Claude Code's Bash, Read and Write tools.

Needs the `claude` extra, the Claude Code CLI signed in, and `uv` on PATH: the
agents build and read .docx/.pptx files with `uv run --with python-docx ...`.

Run: uv run python examples/claude_deck_bakeoff.py
Inspect: uv run ava dev examples/claude_deck_bakeoff.py
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

import avalanche as ava

MODEL = os.environ.get("CLAUDE_DECK_BAKEOFF_MODEL", "claude-sonnet-5-5")
OUTPUT_DIR = (
    Path(os.environ.get("AVALANCHE_EXAMPLE_ROOT", ".avalanche")) / "claude-deck-bakeoff"
)
TOPIC = "Why every office should replace its coffee machine with a tea sommelier"

OFFICE_TOOLING = (
    "Build and inspect Office files with Python run through Bash, for example "
    "`uv run --no-project --with python-docx --with python-pptx python script.py`. "
    "Write helper scripts in the current directory, never next to the output files."
)


def claude(*, max_turns: int = 30) -> ava.runtimes.ClaudeAgent:
    return ava.runtimes.ClaudeAgent(
        model=MODEL,
        max_turns=max_turns,
        builtin_tools=["Bash", "Read", "Write", "Edit", "Glob"],
        system_prompt_append=OFFICE_TOOLING,
        max_budget_usd=2.0,
    )


class WriteMemo(ava.Signature):
    """Write a persuasive one-to-two page Word memo on the topic. Use a title, an
    executive summary, three or four headed sections with concrete (invented)
    numbers, and a closing recommendation."""

    topic: str = ava.InputField()
    memo: ava.File = ava.OutputField(desc="The memo as a .docx file.")


class BuildDeck(ava.Signature):
    """Turn the Word memo into a PowerPoint deck of five to eight slides that a
    busy executive could present without the memo. Read the memo first."""

    memo: ava.File = ava.InputField(desc="The source memo (.docx).")
    style: str = ava.InputField(desc="The visual and narrative style the deck must follow.")
    deck: ava.File = ava.OutputField(desc="The deck as a .pptx file.")


class Verdict(BaseModel):
    winner: Literal["deck_a", "deck_b"]
    deck_a_score: int
    deck_b_score: int
    reasoning: str


class JudgeDecks(ava.Signature):
    """Two designers built competing decks from the same memo. Extract and read the
    text and layout of both decks, check each against the memo for accuracy and
    coverage, and score each from 1 to 10 for clarity, faithfulness and visual
    structure. Write a Markdown scorecard comparing them slide by slide, then pick
    the better deck."""

    memo: ava.File = ava.InputField(desc="The source memo (.docx).")
    deck_a: ava.File = ava.InputField(desc="The first candidate deck (.pptx).")
    deck_b: ava.File = ava.InputField(desc="The second candidate deck (.pptx).")
    scorecard: ava.File = ava.OutputField(desc="The comparison as a Markdown file.")
    verdict: Verdict = ava.OutputField()


class Judgement(BaseModel):
    verdict: Verdict
    scorecard: ava.File


class BakeoffResult(BaseModel):
    winner: Literal["deck_a", "deck_b"]
    reasoning: str
    files: list[str]


@ava.source
def pick_topic() -> str:
    return TOPIC


@ava.agent_step(WriteMemo, runtime=claude())
async def write_memo(topic: str, *, agent: ava.Agent) -> ava.File:
    return (await agent(topic=topic)).outputs.memo


@ava.agent_step(BuildDeck, runtime=claude())
async def build_minimal_deck(memo: ava.File, *, agent: ava.Agent) -> ava.File:
    style = (
        "Minimalist: one idea per slide, at most three short bullets, generous white "
        "space, a single accent colour."
    )
    return (await agent(memo=memo, style=style)).outputs.deck


@ava.agent_step(BuildDeck, runtime=claude())
async def build_dense_deck(memo: ava.File, *, agent: ava.Agent) -> ava.File:
    style = (
        "Consulting style: action titles that state the takeaway, dense supporting "
        "bullets, at least one table of the memo's numbers, and speaker notes."
    )
    return (await agent(memo=memo, style=style)).outputs.deck


@ava.agent_step(JudgeDecks, runtime=claude(max_turns=40))
async def judge_decks(
    memo: ava.File, deck_a: ava.File, deck_b: ava.File, *, agent: ava.Agent
) -> Judgement:
    outputs = (await agent(memo=memo, deck_a=deck_a, deck_b=deck_b)).outputs
    return Judgement(verdict=outputs.verdict, scorecard=outputs.scorecard)


@ava.dest
def publish_bakeoff(
    memo: ava.File, deck_a: ava.File, deck_b: ava.File, judgement: Judgement
) -> BakeoffResult:
    """Copy the memo, both decks and the scorecard to OUTPUT_DIR, marking the winner."""
    verdict = judgement.verdict
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    files = {
        "memo.docx": memo,
        f"deck_a{'_WINNER' if verdict.winner == 'deck_a' else ''}.pptx": deck_a,
        f"deck_b{'_WINNER' if verdict.winner == 'deck_b' else ''}.pptx": deck_b,
        "scorecard.md": judgement.scorecard,
    }
    for name, file in files.items():
        with file.open() as source, (OUTPUT_DIR / name).open("wb") as target:
            shutil.copyfileobj(source, target)
    return BakeoffResult(
        winner=verdict.winner,
        reasoning=verdict.reasoning,
        files=[str(OUTPUT_DIR / name) for name in files],
    )


@ava.workflow
def claude_deck_bakeoff():
    memo = write_memo(pick_topic())
    deck_a = build_minimal_deck(memo)
    deck_b = build_dense_deck(memo)
    return publish_bakeoff(memo, deck_a, deck_b, judge_decks(memo, deck_a, deck_b))


if __name__ == "__main__":
    result = claude_deck_bakeoff().run(executor=ava.LocalExecutor()).result()
    print(result)
