from __future__ import annotations

import hashlib

import pytest
from dgx_agent.packages.providers import ComponentDescriptor as AgentComponent
from dgx_agent.workload_runtime import protocol_component
from dgx_agent_protocol import AgentProtocolError


def _component(source: dict[str, object]) -> dict[str, object]:
    payload = b"payload"
    return {
        "name": "runtime",
        "kind": "native-runtime",
        "media_type": "application/octet-stream",
        "sources": [source],
        "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
        "unpacked_size": len(payload),
        "platforms": ["linux/arm64"],
        "materialization": {"method": "file"},
        "evidence": [],
    }


def test_protocol_component_conversion_preserves_digest_and_source_binding() -> None:
    from dgx_agent_protocol.workload_packages import ComponentDescriptor

    protocol = ComponentDescriptor.parse(
        _component(
            {
                "provider": "https",
                "url": "https://downloads.example.invalid/runtime.bin",
            }
        )
    )
    converted = protocol_component(protocol)

    assert type(converted) is AgentComponent
    assert converted.digest == protocol.digest
    assert converted.sources[0].provider == "https"
    assert converted.sources[0].immutable_id == protocol.digest
    assert converted.sources[0].allowed_domains == ("downloads.example.invalid",)


def test_protocol_component_rejects_platform_mismatch() -> None:
    from dgx_agent_protocol.workload_packages import ComponentDescriptor

    protocol = ComponentDescriptor.parse(
        _component(
            {
                "provider": "https",
                "url": "https://downloads.example.invalid/runtime.bin",
            }
        )
    )

    with pytest.raises(AgentProtocolError, match="platform"):
        protocol_component(protocol, platform="linux/x86-64")
