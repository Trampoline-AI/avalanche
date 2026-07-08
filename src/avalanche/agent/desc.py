"""Annotation metadata for generate_signature field descriptions."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Desc:
    """Metadata for ``Annotated[T, Desc("...")]`` descriptions used by generate_signature."""

    description: str
