"""PredictRLM's own skills, passed with ``ava.runtimes.PredictRLM(skills=[...])``.

A PredictRLM ``Skill`` bundles prompt instructions with sandbox packages, Python
modules and host tools, and is always in the agent's prompt. It is not the
Agent Skills format that ``ava.agent.Skill`` follows. ``pdf``, ``spreadsheet``
and ``docx`` are PredictRLM's built-in skills.
"""

from __future__ import annotations

from predict_rlm import Skill
from predict_rlm.skills import docx, pdf, spreadsheet

__all__ = ["Skill", "docx", "pdf", "spreadsheet"]
