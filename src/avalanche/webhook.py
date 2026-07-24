"""Public configuration for HTTP-triggered workflows."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Webhook:
    """Enable a workflow's local POST webhook, optionally at ``path``."""

    path: str | None = None

    def __post_init__(self) -> None:
        if self.path is None:
            return
        if (
            not isinstance(self.path, str)
            or not self.path.startswith("/")
            or self.path.startswith("//")
            or len(self.path) > 1024
            or not self.path.isprintable()
            or "\\" in self.path
        ):
            raise ValueError("Webhook path must be a printable absolute URL path")
        if "?" in self.path or "#" in self.path:
            raise ValueError("Webhook path must not contain a query or fragment")
        if self.path != "/" and any(
            segment in {"", ".", ".."} for segment in self.path.removeprefix("/").split("/")
        ):
            raise ValueError("Webhook path must use non-empty, non-traversing segments")
