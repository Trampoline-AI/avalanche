"""Source-evidence validation for meeting follow-ups."""

from .schema import Extraction, MeetingContext


def validate_extraction(extraction: Extraction, meeting: MeetingContext) -> None:
    """Reject invented or mislocated quotations before classification or publication."""
    for item in extraction.items:
        for passage in item.passages:
            if not 1 <= passage.start_line <= passage.end_line <= len(meeting.lines):
                raise ValueError(f"Invalid source line range for {item.title!r}")
            source = "\n".join(meeting.lines[passage.start_line - 1 : passage.end_line])
            if passage.quote != source:
                raise ValueError(f"Source quote does not match transcript for {item.title!r}")
