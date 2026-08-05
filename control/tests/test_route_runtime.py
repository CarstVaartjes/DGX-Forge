from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from dgx_control.models import AgentNode, Base
from dgx_control.presence import AgentPresenceService, ManagementAddressPolicy
from dgx_control.route_runtime import ProductionRouteManager, RouteRuntimeError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

NODE_ID = "spk_" + "0" * 31 + "1"
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
ROUTES = {
    "deepseek": {
        "node_id": NODE_ID,
        "workload": "deepseek-agent-single",
        "requests_per_minute": 30,
        "tokens_per_minute": 10_000,
    }
}


def _manager(
    tmp_path,
    *,
    probe=lambda _url, _key: None,
    observed_at=NOW,
    clock=lambda: NOW,
):
    repository = tmp_path / "repository"
    workload = repository / "config/workloads/deepseek-agent-single.toml"
    workload.parent.mkdir(parents=True)
    workload.write_text(
        'id = "deepseek-agent-single"\n[endpoint]\nhost = "127.0.0.1"\nport = 8888\n'
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'routes.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(AgentNode(node_id=NODE_ID, state="active", capabilities=[]))
    policy = ManagementAddressPolicy.parse("10.0.0.0/24")
    presence = AgentPresenceService(sessions, policy)
    presence.observe(NODE_ID, "10.0.0.42", observed_at)
    live = tmp_path / "live/config.yaml"
    manager = ProductionRouteManager(
        repository,
        state_root=tmp_path / "state",
        live_config=live,
        presence=presence,
        management_policy=policy,
        upstream_key="upstream-test-key",
        probe=probe,
        clock=clock,
        maximum_age_seconds=150,
        refresh_interval_seconds=60,
    )
    return manager, live, presence


def test_successful_reconciliation_publishes_probed_presence_route(tmp_path) -> None:
    probes = []
    manager, live, _presence = _manager(
        tmp_path,
        probe=lambda url, key: probes.append((url, key)),
    )
    manager.withdraw((NODE_ID,))
    assert json.loads(live.read_bytes())["model_list"] == []

    result = manager.publish(
        commit="a" * 40,
        profile="agent-single",
        targets=(NODE_ID,),
        routes=ROUTES,
    )

    assert probes == [("http://10.0.0.42:8888/v1/models", "upstream-test-key")]
    config = json.loads(live.read_bytes())
    assert config["model_list"][0]["model_name"] == "deepseek"
    assert config["model_list"][0]["litellm_params"]["api_base"] == "http://10.0.0.42:8888/v1"
    assert b"upstream-test-key" not in live.read_bytes()
    assert result.route_state.health_timestamp == NOW.isoformat()


def test_probe_failure_keeps_litellm_withdrawn(tmp_path) -> None:
    def fail(_url, _key):
        raise OSError("refused")

    manager, live, _presence = _manager(tmp_path, probe=fail)
    manager.withdraw((NODE_ID,))

    with pytest.raises(RouteRuntimeError, match="probe"):
        manager.publish(
            commit="a" * 40,
            profile="agent-single",
            targets=(NODE_ID,),
            routes=ROUTES,
        )

    assert json.loads(live.read_bytes())["model_list"] == []


def test_stale_presence_never_reaches_probe_or_live_routes(tmp_path) -> None:
    manager, live, _presence = _manager(
        tmp_path,
        observed_at=NOW - timedelta(seconds=151),
        probe=lambda _url, _key: pytest.fail("must not probe stale address"),
    )
    manager.withdraw((NODE_ID,))

    with pytest.raises(RouteRuntimeError, match="stale"):
        manager.publish(
            commit="a" * 40,
            profile="agent-single",
            targets=(NODE_ID,),
            routes=ROUTES,
        )

    assert json.loads(live.read_bytes())["model_list"] == []


def test_route_port_must_come_from_repository_workload(tmp_path) -> None:
    manager, _live, _presence = _manager(tmp_path)
    forged = {"deepseek": dict(ROUTES["deepseek"], port=9999)}

    with pytest.raises(RouteRuntimeError, match="fields"):
        manager.publish(
            commit="a" * 40,
            profile="agent-single",
            targets=(NODE_ID,),
            routes=forged,
        )


def test_refresh_republishes_a_changed_dhcp_observation(tmp_path) -> None:
    current = [NOW]
    probes = []
    manager, live, presence = _manager(
        tmp_path,
        clock=lambda: current[0],
        probe=lambda url, _key: probes.append(url),
    )
    manager.withdraw((NODE_ID,))
    manager.publish(
        commit="a" * 40,
        profile="agent-single",
        targets=(NODE_ID,),
        routes=ROUTES,
    )
    current[0] = NOW + timedelta(seconds=60)
    presence.observe(NODE_ID, "10.0.0.43", current[0])

    assert manager.refresh_if_due(lambda: "a" * 40) is True
    config = json.loads(live.read_bytes())
    assert config["model_list"][0]["litellm_params"]["api_base"] == "http://10.0.0.43:8888/v1"
    assert probes[-1] == "http://10.0.0.43:8888/v1/models"


def test_refresh_withdraws_routes_when_presence_expires(tmp_path) -> None:
    current = [NOW]
    manager, live, _presence = _manager(tmp_path, clock=lambda: current[0])
    manager.withdraw((NODE_ID,))
    manager.publish(
        commit="a" * 40,
        profile="agent-single",
        targets=(NODE_ID,),
        routes=ROUTES,
    )
    current[0] = NOW + timedelta(seconds=151)

    assert manager.refresh_if_due(lambda: "a" * 40) is False
    assert json.loads(live.read_bytes())["model_list"] == []


def test_refresh_is_bounded_and_skips_before_interval(tmp_path) -> None:
    current = [NOW]
    manager, _live, _presence = _manager(tmp_path, clock=lambda: current[0])
    manager.withdraw((NODE_ID,))
    manager.publish(
        commit="a" * 40,
        profile="agent-single",
        targets=(NODE_ID,),
        routes=ROUTES,
    )

    assert manager.refresh_if_due(lambda: pytest.fail("must not resolve commit")) is False


def test_refresh_withdraws_when_commit_loses_eligibility(tmp_path) -> None:
    current = [NOW]
    manager, live, _presence = _manager(tmp_path, clock=lambda: current[0])
    manager.withdraw((NODE_ID,))
    manager.publish(
        commit="a" * 40,
        profile="agent-single",
        targets=(NODE_ID,),
        routes=ROUTES,
    )
    current[0] = NOW + timedelta(seconds=60)

    assert manager.refresh_if_due(lambda: "a" * 40, eligible=lambda _commit: False) is False
    assert json.loads(live.read_bytes())["model_list"] == []
