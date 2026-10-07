import os
from pathlib import Path

from model_selection import select_models

PACKAGE_ROOT = Path(__file__).parent
_models = select_models(
    lm_env="CUSTOMER_FEEDBACK_REVIEW_MODEL", sub_lm_env="CUSTOMER_FEEDBACK_REVIEW_SUB_MODEL"
)
MODEL = _models.lm
SUB_MODEL = _models.sub_lm
FEEDBACK_WORKBOOK_PATH = PACKAGE_ROOT / "feedback_workbook.xlsx"

_example_root = os.getenv("AVALANCHE_EXAMPLE_ROOT")
ARTIFACT_ROOT = (
    Path(_example_root) / "customer_feedback_review"
    if _example_root is not None
    else Path(".avalanche/outputs/feedback_review") / str(os.getpid())
)
WORKBOOK_OUTPUT_DIR = ARTIFACT_ROOT / "workbook"
BRIEF_OUTPUT_DIR = ARTIFACT_ROOT / "brief"
PUBLISHED_OUTPUT_DIR = ARTIFACT_ROOT / "published"
