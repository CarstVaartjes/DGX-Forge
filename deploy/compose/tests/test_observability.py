import json
from pathlib import Path

from deploy.compose.tests.test_networking import _rendered


ROOT = Path(__file__).resolve().parents[3]


def test_every_alert_has_runbook_and_nonempty_summary() -> None:
    document = json.loads((ROOT / "deploy/compose/prometheus/alerts.yaml").read_text())
    alerts = [rule for group in document["groups"] for rule in group["rules"]]
    assert {alert["alert"] for alert in alerts} >= {
        "InferenceRoutesStuckInMaintenance", "SparkProbeStale", "RepeatedReconciliationFailure",
        "ControlBackupStale", "ControlDatabaseUnavailable", "WorkerLeaseStarvation",
    }
    for alert in alerts:
        assert alert["annotations"]["summary"]
        assert alert["annotations"]["runbook_url"].startswith("https://")


def test_grafana_is_only_reachable_via_caddy_and_has_no_anonymous_admin() -> None:
    services = _rendered()["services"]
    grafana = services["grafana"]
    assert "ports" not in grafana
    assert set(grafana["networks"]) == {"application", "ingress"}
    assert grafana["environment"]["GF_AUTH_ANONYMOUS_ENABLED"] == "false"
    assert grafana["environment"]["GF_SECURITY_ADMIN_PASSWORD__FILE"] == "/run/secrets/grafana-admin-password"
    caddy = (ROOT / "deploy/compose/Caddyfile").read_text()
    assert "handle /grafana/*" in caddy and "grafana:3000" in caddy


def test_dashboards_are_versioned_and_query_stable_metrics() -> None:
    for name in ("fleet", "jobs"):
        dashboard = json.loads((ROOT / f"deploy/compose/grafana/dashboards/{name}.json").read_text())
        assert dashboard["uid"] == f"dgx-{name}"
        assert dashboard["title"] and dashboard["panels"]
        assert all(panel.get("targets") for panel in dashboard["panels"])


def test_every_service_has_bounded_logging() -> None:
    for service in _rendered()["services"].values():
        assert service["logging"]["driver"] == "local"
        assert service["logging"]["options"]["max-size"]
        assert service["logging"]["options"]["max-file"]
