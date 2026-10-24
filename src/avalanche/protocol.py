"""Version of the complete Avalanche contract consumed by Delta workers."""

import re
from dataclasses import dataclass
from typing import Self


@dataclass(frozen=True, order=True)
class ProtocolVersion:
    """A protocol major and additive minor, independent of package versions."""

    major: int
    minor: int

    def __post_init__(self) -> None:
        if self.major < 0 or self.minor < 0:
            raise ValueError("Protocol version components must be non-negative")

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}"

    @classmethod
    def parse(cls, value: str) -> Self:
        """Parse a major.minor string, rejecting prefixes and extra components."""
        if re.fullmatch(r"[0-9]+\.[0-9]+", value) is None:
            raise ValueError(f"Invalid protocol version {value!r}; expected major.minor")
        major, minor = value.split(".")
        return cls(int(major), int(minor))


PROTOCOL_VERSION = ProtocolVersion(1, 0)


def supports(required: ProtocolVersion, provided: ProtocolVersion = PROTOCOL_VERSION) -> bool:
    """Whether a worker's provided contract can run a deployment's requirement."""
    return provided.major == required.major and provided.minor >= required.minor
