"""One version for everything Delta relies on from Avalanche; see docs/compatibility.md."""

from typing import NamedTuple


class ProtocolVersion(NamedTuple):
    major: int
    minor: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}"


PROTOCOL_VERSION = ProtocolVersion(2, 0)


def supports(required: ProtocolVersion, provided: ProtocolVersion = PROTOCOL_VERSION) -> bool:
    """A worker runs a deployment iff the major matches and its minor is not older."""
    return provided.major == required.major and provided.minor >= required.minor
