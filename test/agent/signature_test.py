"""Tests for class-style and dynamic Avalanche agent signatures."""
from __future__ import annotations

import dspy
from pydantic import BaseModel

import avalanche as ava


class Report(BaseModel):
    title: str


class TestSignatureContracts:
    def test_root_and_agent_signature_are_one_subclassable_dspy_contract(self):
        """Class declarations expose one root/module Signature type to DSPy."""

        class Review(ava.Signature):
            """Review the supplied document for compliance."""

            document: str = ava.InputField(desc="document to review")
            report: Report = ava.OutputField(desc="structured report")
            verdict: str = ava.OutputField(desc="plain-language verdict")

        assert ava.Signature is ava.agent.Signature
        assert issubclass(Review, dspy.Signature)
        assert list(Review.input_fields) == ["document"]
        assert Review.input_fields["document"].annotation is str
        assert Review.input_fields["document"].json_schema_extra["desc"] == "document to review"
        assert list(Review.output_fields) == ["report", "verdict"]
        assert Review.output_fields["report"].annotation is Report
        assert Review.output_fields["report"].json_schema_extra["desc"] == "structured report"
        assert Review.output_fields["verdict"].annotation is str
        assert Review.output_fields["verdict"].json_schema_extra["desc"] == "plain-language verdict"
        assert Review.instructions == "Review the supplied document for compliance."

    def test_dynamic_signature_string_preserves_model_boundary_and_instructions(self):
        """The dynamic form parses a multi-output DSPy contract for an Agent."""
        signature = ava.agent.Signature(
            "document: str -> verdict: str, note: str",
            "Return a verdict and review note.",
        )

        assert list(signature.input_fields) == ["document"]
        assert signature.input_fields["document"].annotation is str
        assert list(signature.output_fields) == ["verdict", "note"]
        assert signature.output_fields["verdict"].annotation is str
        assert signature.output_fields["note"].annotation is str
        assert signature.instructions == "Return a verdict and review note."


class TestAgentImports:
    def test_skills_imports_the_provider_only_when_accessed(self):
        """The provider-owned skills namespace resolves on demand."""
        from avalanche.agent import skills
        import predict_rlm.skills

        assert skills is predict_rlm.skills
