"""Compatibility decisions for already-built Delta deployments."""

from dataclasses import FrozenInstanceError

import pytest

from avalanche import PROTOCOL_VERSION, ProtocolVersion
from avalanche.protocol import supports


def test_protocol_admission_and_immutable_version() -> None:
    required = ProtocolVersion.parse("1.0")
    assert required == PROTOCOL_VERSION
    assert str(required) == "1.0"
    assert supports(required)
    assert supports(required, ProtocolVersion(1, 2))
    assert not supports(ProtocolVersion(1, 2), required)
    assert not supports(required, ProtocolVersion(2, 0))
    assert not supports(ProtocolVersion(2, 0), required)
    with pytest.raises(FrozenInstanceError):
        setattr(required, "minor", 2)


@pytest.mark.parametrize("value", ["1", "1.0.0", "v1.0", "-1.0", "1.-1", " 1.0", "1.0\n"])
def test_protocol_parse_rejects_invalid_version(value: str) -> None:
    with pytest.raises(ValueError):
        ProtocolVersion.parse(value)


@pytest.mark.parametrize("major,minor", [(-1, 0), (1, -1)])
def test_protocol_rejects_negative_components(major: int, minor: int) -> None:
    with pytest.raises(ValueError):
        ProtocolVersion(major, minor)
