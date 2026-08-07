from __future__ import annotations

import pytest
from vonk_control.hermes_policy import HermesAgentPolicy, HermesPolicyError

DUAL = "deepseek-agent-dual"
SINGLE = "deepseek-agent-single"
KNOWN = {DUAL, SINGLE}
POLICY = b'''schema_version = 1
alias = "hermes-agent"
local_only = true

[[candidates]]
workload = "deepseek-agent-dual"
priority = 1
minimum_maturity = "accepted"

[[candidates]]
workload = "deepseek-agent-single"
priority = 2
minimum_maturity = "accepted"
'''


def test_policy_selects_only_active_accepted_candidates_in_priority_order() -> None:
    policy = HermesAgentPolicy.parse(POLICY, known_workloads=KNOWN)

    assert policy.schema_version == 1
    assert policy.alias == "hermes-agent"
    assert policy.local_only is True
    assert [candidate.workload for candidate in policy.eligible(
        {DUAL, SINGLE},
        {DUAL: "accepted", SINGLE: "accepted"},
    )] == [DUAL, SINGLE]
    assert [candidate.workload for candidate in policy.eligible(
        {SINGLE},
        {DUAL: "accepted", SINGLE: "accepted"},
    )] == [SINGLE]
    assert policy.eligible(
        {DUAL, SINGLE},
        {DUAL: "verified", SINGLE: "planned"},
    ) == ()


@pytest.mark.parametrize(
    ("content", "message"),
    (
        (POLICY + b'provider = "openai"\n', "fields"),
        (POLICY.replace(b"local_only = true", b"local_only = false"), "local-only"),
        (POLICY.replace(b'alias = "hermes-agent"', b'alias = "remote-agent"'), "local-only"),
        (POLICY.replace(b"schema_version = 1", b"schema_version = 2"), "local-only"),
        (POLICY.replace(b"priority = 2", b"priority = 1"), "unique"),
        (POLICY.replace(b"deepseek-agent-single", b"deepseek-agent-dual"), "unique"),
        (POLICY.replace(b"deepseek-agent-single", b"unknown-agent"), "known"),
        (POLICY.replace(b'minimum_maturity = "accepted"', b'minimum_maturity = "verified"', 1), "accepted"),
        (POLICY.replace(b"priority = 1", b"priority = true"), "priority"),
        (POLICY.replace(b"priority = 1", b"priority = 0"), "priority"),
        (POLICY.split(b"[[candidates]]", 1)[0], "fields"),
        (b"\xff", "TOML"),
    ),
)
def test_policy_rejects_nonlocal_ambiguous_or_unknown_configuration(
    content: bytes,
    message: str,
) -> None:
    with pytest.raises(HermesPolicyError, match=message):
        HermesAgentPolicy.parse(content, known_workloads=KNOWN)


def test_policy_rejects_unknown_candidate_fields() -> None:
    content = POLICY.replace(
        b'workload = "deepseek-agent-dual"',
        b'workload = "deepseek-agent-dual"\nbase_url = "https://api.openai.com/v1"',
    )

    with pytest.raises(HermesPolicyError, match="candidate fields"):
        HermesAgentPolicy.parse(content, known_workloads=KNOWN)
