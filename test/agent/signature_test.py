"""Tests for agent signature generation."""

from __future__ import annotations

import subprocess
import sys
from typing import Annotated

import dspy
import pytest
from pydantic import BaseModel

from avalanche.agent import Desc, generate_signature


class Report(BaseModel):
    """Report model used in generated signatures."""

    title: str


class Doc(BaseModel):
    """Document model used in generated signatures."""

    path: str


class TestGenerateSignature:
    """Test function-to-DSPy signature generation."""

    def test_basic_signature_generation(self):
        """Test generating a signature from basic parameters and a model return."""

        def audit_rfp(a: str, b: int) -> Report:
            """Audit an RFP."""

        sig = generate_signature(audit_rfp)

        assert issubclass(sig, dspy.Signature)
        assert list(sig.input_fields) == ["a", "b"]
        assert sig.input_fields["a"].annotation is str
        assert sig.input_fields["b"].annotation is int
        assert list(sig.output_fields) == ["output"]
        assert sig.output_fields["output"].annotation is Report
        assert sig.instructions

    def test_annotated_desc_sets_input_field_description(self):
        """Test Annotated Desc metadata is used for input descriptions."""

        def audit_docs(
            docs: Annotated[list[Doc], Desc("docs to audit")],
            reviewer: str,
        ) -> Report:
            """Audit documents."""

        sig = generate_signature(audit_docs)

        assert sig.input_fields["docs"].annotation == list[Doc]
        assert sig.input_fields["docs"].json_schema_extra["desc"] == "docs to audit"
        assert sig.input_fields["reviewer"].json_schema_extra["desc"] != "docs to audit"

    def test_missing_docstring_uses_empty_instructions(self):
        """Test a missing docstring produces empty instructions."""

        def audit_rfp(a: str) -> Report:
            pass

        sig = generate_signature(audit_rfp)

        assert sig.instructions == ""

    def test_list_model_return_is_supported(self):
        """Test list[Model] return annotations are supported."""

        def audit_rfps(a: str) -> list[Report]:
            """Audit RFPs."""

        sig = generate_signature(audit_rfps)

        assert sig.output_fields["output"].annotation == list[Report]

    def test_missing_return_annotation_raises_type_error(self):
        """Test missing return annotations are rejected."""

        def audit_rfp(a: str):
            """Audit an RFP."""

        with pytest.raises(TypeError, match="audit_rfp"):
            generate_signature(audit_rfp)

    def test_missing_parameter_annotation_raises_type_error(self):
        """Test missing parameter annotations are rejected."""

        def audit_rfp(x) -> Report:
            """Audit an RFP."""

        with pytest.raises(TypeError, match="x.*audit_rfp"):
            generate_signature(audit_rfp)

    def test_non_model_return_raises_type_error(self):
        """Test scalar and non-model list returns are rejected."""

        def audit_score(a: str) -> int:
            """Audit a score."""

        def audit_scores(a: str) -> list[int]:
            """Audit scores."""

        with pytest.raises(TypeError, match="audit_score"):
            generate_signature(audit_score)
        with pytest.raises(TypeError, match="audit_scores"):
            generate_signature(audit_scores)

    def test_skip_params_omits_runtime_parameters_before_annotation_check(self):
        """Test skipped parameters are omitted and do not require annotations."""

        def audit_rfp(ctx, x: str) -> Report:
            """Audit an RFP."""

        sig = generate_signature(audit_rfp, skip_params={"ctx"})

        assert list(sig.input_fields) == ["x"]

    def test_custom_output_field_name(self):
        """Test custom output field names are used."""

        def audit_rfp(a: str) -> Report:
            """Audit an RFP."""

        sig = generate_signature(audit_rfp, output_field_name="result")

        assert list(sig.output_fields) == ["result"]

    def test_output_field_name_collision_raises_value_error(self):
        """Test output field names cannot collide with input fields."""

        def audit_rfp(output: str) -> Report:
            """Audit an RFP."""

        with pytest.raises(ValueError, match="audit_rfp.*output"):
            generate_signature(audit_rfp)


class TestAgentImports:
    """Test agent package import behavior."""

    def test_importing_agent_does_not_import_heavy_dependencies(self):
        """Test importing avalanche.agent keeps dspy and predict_rlm lazy."""
        code = """
import sys

import avalanche
import avalanche.agent

ref = avalanche.input.some_field
assert type(ref).__name__ == "InputRef"
assert "dspy" not in sys.modules
assert "predict_rlm" not in sys.modules
assert avalanche.agent.Desc
assert avalanche.agent.generate_signature
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr

    def test_skills_proxy_imports_predict_rlm_skills(self):
        """Test skills is a lazy proxy for predict_rlm.skills."""
        from avalanche.agent import skills  # noqa: I001
        import predict_rlm.skills

        assert skills is predict_rlm.skills
        assert skills.pdf is predict_rlm.skills.pdf
