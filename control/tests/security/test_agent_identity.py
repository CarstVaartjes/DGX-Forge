from __future__ import annotations

import pytest

from dgx_control.auth import AgentIdentity, AuthError, agent_identity_from_scope


NODE = "spk_" + "a" * 32


def test_agent_scope_identity_must_be_typed_and_verified() -> None:
    assert agent_identity_from_scope({"dgx.agent_identity": {"node_id": NODE}}) is None
    identity = AgentIdentity(NODE, "serial", "fingerprint", True)
    assert agent_identity_from_scope({"dgx.agent_identity": identity}) == identity


@pytest.mark.parametrize("node,verified", (("not-a-node", True), (NODE, False)))
def test_agent_identity_rejects_noncanonical_or_unverified_values(node: str, verified: bool) -> None:
    with pytest.raises(AuthError):
        AgentIdentity(node, "serial", "fingerprint", verified)
