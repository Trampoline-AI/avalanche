from pathlib import Path
from shutil import copy2

from predict_rlm import File

from .schema import PublishedReviewPack


def _validated_file_path(file: File, *, label: str, suffix: str) -> Path:
    if file.path is None:
        raise ValueError(f"{label} did not return a host file path")
    path = Path(file.path)
    if not path.is_file():
        raise ValueError(f"{label} output does not exist: {path}")
    if path.suffix.lower() != suffix:
        raise ValueError(f"{label} must be a {suffix} file, received: {path.name}")
    return path


def publish_review_pack_files(
    workbook: File,
    brief: File,
    *,
    destination: Path,
) -> PublishedReviewPack:
    workbook_source = _validated_file_path(workbook, label="workbook", suffix=".xlsx")
    brief_source = _validated_file_path(brief, label="brief", suffix=".docx")
    destination.mkdir(parents=True, exist_ok=True)

    workbook_target = destination / "product_review.xlsx"
    brief_target = destination / "executive_brief.docx"
    copy2(workbook_source, workbook_target)
    copy2(brief_source, brief_target)

    return PublishedReviewPack(
        workbook_path=str(workbook_target.resolve()),
        brief_path=str(brief_target.resolve()),
    )
