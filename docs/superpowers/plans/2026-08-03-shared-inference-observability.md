# Shared Inference and Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish only healthy accepted model endpoints through Caddy and LiteLLM and provide actionable metrics, dashboards, alerts, and bounded logs.

**Architecture:** The reconciler renders desired route fragments from an eligible commit and healthy concrete placement. Caddy owns public TLS/auth/maintenance behavior; LiteLLM owns OpenAI-compatible aliases, quotas, and usage only. Prometheus and Grafana remain separate standard containers with provisioned, versioned configuration.

**Tech Stack:** Caddy 2, LiteLLM, Prometheus, Grafana, optional Alertmanager, OpenMetrics, pytest, Docker Compose integration.

## Global Constraints

- Routing is derived output, never model/profile authority.
- Caddy is the only public HTTP service.
- LiteLLM receives only endpoints published healthy by the control plane.
- Route updates are atomic and fail to maintenance/503.
- Metrics and logs contain no prompts, responses, tokens, credentials, raw serials, or private addresses unless explicitly classified and protected.
- Prometheus data is disposable initially; accepted evidence remains in Git-backed records.
- Do not enable routes for unfinished runtime-roadmap models.

---

### Task 1: Implement atomic route publication

**Files:**
- Create: `control/src/dgx_control/routes.py`
- Create: `control/tests/test_routes.py`
- Modify: `deploy/compose/Caddyfile`

**Interfaces:**
- `RoutePublisher.maintenance(targets, reason)`, `publish(snapshot)`, and `snapshot()`.
- Route snapshot pins commit, profile, workload, node IDs, upstreams, and health timestamp.

- [ ] **Step 1: Write failing atomicity and invalid-upstream tests**

```python
def test_invalid_candidate_keeps_maintenance_routes(publisher):
    publisher.maintenance(TARGETS, "switch")
    with pytest.raises(RouteValidationError):
        publisher.publish(snapshot(upstream="http://unconfigured:8888"))
    assert publisher.snapshot().state == "maintenance"


def test_publish_is_atomic_for_all_profile_aliases(publisher):
    publisher.publish(snapshot(aliases=["deepseek", "reasoning"]))
    assert publisher.snapshot().generation == 1
    assert publisher.visible_aliases() == {"deepseek", "reasoning"}
```

- [ ] **Step 2: Run and observe missing publisher**

Run: `uv run --project control pytest control/tests/test_routes.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement canonical fragments, allowlisted upstreams, validation, and transactional generation swap**

Render into a temporary generation directory, validate through Caddy's config adapter/API on the private network, swap the active generation only after all routes validate, and retain one known-good maintenance generation.

- [ ] **Step 4: Run route tests**

Run: `uv run --project control pytest control/tests/test_routes.py -v`
Expected: PASS.

- [ ] **Step 5: Commit route publication**

```bash
git add control/src/dgx_control/routes.py control/tests/test_routes.py deploy/compose/Caddyfile
git commit -m "feat: publish fail-closed inference routes"
```

### Task 2: Add LiteLLM as policy-limited inference router

**Files:**
- Modify: `deploy/compose/compose.yaml`
- Create: `deploy/compose/litellm/config.yaml`
- Create: `control/src/dgx_control/litellm.py`
- Create: `control/tests/test_litellm.py`

**Interfaces:**
- `LiteLlmPublisher.render(route_snapshot, policy) -> bytes` and `apply(generation)`.
- LiteLLM aliases map only to control-plane internal Caddy upstream routes or validated Spark endpoints on the private path.

- [ ] **Step 1: Write failing authority and secret tests**

```python
def test_litellm_cannot_add_unknown_repository_model(publisher, route_snapshot):
    with pytest.raises(LiteLlmPolicyError):
        publisher.render(route_snapshot, policy_with_extra_model("shadow-model"))


def test_rendered_config_contains_secret_reference_not_value(publisher, route_snapshot):
    rendered = publisher.render(route_snapshot, POLICY).decode()
    assert "os.environ/" in rendered
    assert "sk-live" not in rendered
```

- [ ] **Step 2: Run and observe missing service**

Run: `uv run --project control pytest control/tests/test_litellm.py -v`
Expected: FAIL.

- [ ] **Step 3: Pin LiteLLM and implement generated alias/quota configuration**

Disable dynamic model administration, persist only required usage records, bind privately, require Caddy-origin authentication, validate config before reload, and keep the prior generation on failure.

- [ ] **Step 4: Run tests and Compose validation**

Run: `uv run --project control pytest control/tests/test_litellm.py -v && docker compose -f deploy/compose/compose.yaml config --quiet`
Expected: PASS.

- [ ] **Step 5: Commit LiteLLM**

```bash
git add deploy/compose control/src/dgx_control/litellm.py control/tests/test_litellm.py
git commit -m "feat: route accepted models through LiteLLM"
```

### Task 3: Export sanitized control and fleet metrics

**Files:**
- Create: `control/src/dgx_control/metrics.py`
- Modify: `control/src/dgx_control/api.py`
- Create: `control/tests/test_metrics.py`
- Create: `deploy/compose/prometheus/prometheus.yml`
- Modify: `deploy/compose/compose.yaml`

**Interfaces:**
- Private `/metrics` exports job counts/durations, reconciliation status, route health, node readiness/capacity, probe age, and API outcomes.

- [ ] **Step 1: Write failing cardinality and secret-leak tests**

```python
def test_metrics_use_node_id_not_hostname_or_address(metrics_text):
    assert 'node_id="spk_' in metrics_text
    assert "192.168." not in metrics_text and "spark.local" not in metrics_text


def test_metrics_do_not_contain_request_content(metrics_text):
    assert "prompt" not in metrics_text.lower()
    assert "bearer" not in metrics_text.lower()
```

- [ ] **Step 2: Run and observe missing metrics**

Run: `uv run --project control pytest control/tests/test_metrics.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement bounded-label metrics and private Prometheus scrape**

Use stable enum labels and node IDs, no job/request IDs as labels, histograms with explicit operational buckets, scrape auth on private network, retention/resource bounds, and no public Prometheus port.

- [ ] **Step 4: Run tests and configuration validation**

Run: `uv run --project control pytest control/tests/test_metrics.py -v && docker compose -f deploy/compose/compose.yaml config --quiet`
Expected: PASS.

- [ ] **Step 5: Commit metrics**

```bash
git add control/src/dgx_control/metrics.py control/src/dgx_control/api.py control/tests/test_metrics.py deploy/compose
git commit -m "feat: expose sanitized platform metrics"
```

### Task 4: Provision Grafana dashboards and actionable alerts

**Files:**
- Create: `deploy/compose/grafana/provisioning/datasources/prometheus.yaml`
- Create: `deploy/compose/grafana/provisioning/dashboards/default.yaml`
- Create: `deploy/compose/grafana/dashboards/fleet.json`
- Create: `deploy/compose/grafana/dashboards/jobs.json`
- Create: `deploy/compose/prometheus/alerts.yaml`
- Create: `deploy/compose/tests/test_observability.py`
- Modify: `deploy/compose/compose.yaml`

**Interfaces:**
- Dashboards: fleet/node capacity, active profile/routes, reconciliation/jobs, API/inference health.
- Alerts: route stuck in maintenance, stale node probes, repeated reconciliation failure, backup age, database unavailable, worker lease starvation.

- [ ] **Step 1: Write failing provisioning and runbook-link tests**

```python
def test_every_alert_has_runbook_and_nonempty_summary(alert_rules):
    for alert in alert_rules:
        assert alert["annotations"]["summary"]
        assert alert["annotations"]["runbook_url"].startswith("https://")


def test_grafana_is_only_reachable_via_caddy(rendered_compose):
    assert "ports" not in rendered_compose["services"]["grafana"]
```

- [ ] **Step 2: Run and observe missing provisioning**

Run: `uv run pytest deploy/compose/tests/test_observability.py -v`
Expected: FAIL.

- [ ] **Step 3: Pin Grafana/Prometheus, provision read-only dashboards, and add alert rules**

Use datasource UIDs, version-controlled dashboard JSON, no anonymous admin, secret-file credentials, Caddy subpath routing, actionable thresholds, and leave Alertmanager disabled unless a receiver secret/config is supplied.

- [ ] **Step 4: Run provisioning tests and Compose validation**

Run: `uv run pytest deploy/compose/tests/test_observability.py -v && docker compose -f deploy/compose/compose.yaml config --quiet`
Expected: PASS.

- [ ] **Step 5: Commit observability**

```bash
git add deploy/compose
git commit -m "feat: provision fleet observability"
```

### Task 5: Bound and expose correlated operational logs

**Files:**
- Create: `control/src/dgx_control/logging.py`
- Create: `control/tests/test_logging.py`
- Modify: `deploy/compose/compose.yaml`
- Create: `docs/runbooks/observability.md`

**Interfaces:**
- JSON logs contain timestamp, level, service, event, request ID or job ID, actor ID where appropriate, and redacted structured fields.
- API provides authorized bounded job-log retrieval from persisted evidence references.

- [ ] **Step 1: Write failing redaction and rotation tests**

```python
def test_structured_logger_redacts_secrets(caplog):
    log_event("job.failed", token="secret", stderr="Authorization: Bearer abc")
    assert "secret" not in caplog.text and "abc" not in caplog.text


def test_every_service_has_bounded_logging(rendered_compose):
    for service in rendered_compose["services"].values():
        assert service["logging"]["options"]["max-size"]
        assert service["logging"]["options"]["max-file"]
```

- [ ] **Step 2: Run and observe missing logging policy**

Run: `uv run --project control pytest control/tests/test_logging.py -v && uv run pytest deploy/compose/tests/test_observability.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement centralized redaction and bounded Docker logging**

Redact sensitive key names and bearer/basic authorization patterns, truncate remote output, store full sanitized job logs under content-addressed evidence paths with authorization checks, and configure `local` or `json-file` rotation for every service.

- [ ] **Step 4: Run Phase 5 integration**

Run: `uv run --project control pytest -v && uv run pytest deploy/compose/tests -v && docker compose -f deploy/compose/compose.yaml config --quiet && git diff --check`
Expected: PASS.

- [ ] **Step 5: Commit logging and runbook**

```bash
git add control/src/dgx_control/logging.py control/tests/test_logging.py deploy/compose/compose.yaml docs/runbooks/observability.md
git commit -m "feat: add bounded operational logging"
```
