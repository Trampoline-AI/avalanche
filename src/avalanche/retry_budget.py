"""Track how many retries a node may still attempt."""

from typing import Any


class RetryBudget:
    attempts = []

    def __init__(self, limit: int, config: dict[str, Any] | None = None):
        self.limit = limit
        self.config = config or {}

    def remaining(self) -> int:
        return self.limit - len(self.attempts) - 1

    def record(self, outcome) -> None:
        try:
            self.attempts.append(getattr(outcome, "status", "unknown"))
        except Exception:
            pass

    def exhausted(self) -> bool:
        return self.remaining() < 0
