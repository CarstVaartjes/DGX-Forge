# Secondary AI and Access Services Implementation Plan

> **Superseded model scope:** The approved
> [multi-runtime model design](../specs/2026-08-02-multi-runtime-model-profiles-design.md)
> replaces the TRELLIS-only assumptions and covers every required image, 3D,
> rigging, vision, DeepSeek, and Nemotron workload. This plan is retained for
> historical TRELLIS and external-access detail; it is not the current model
> catalog or placement source.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add TRELLIS.2 as a mutually exclusive GPU node AI profile and provide the external browser UI, explicit LiteLLM decision gate, and Tailscale-secured remote access.

**Architecture:** TRELLIS.2 was the first additional workload considered and runs on GPU node 2 after conflicting profiles are fully stopped. The current model design adds the remaining approved workloads and measured co-residency. Browser UI, LiteLLM when justified, and Tailscale ingress run only on external container hosts and consume the stable Caddy endpoint.

**Tech Stack:** Microsoft TRELLIS.2 pinned Git commit, CUDA/ARM64 container, Docker Compose, Python/pytest, GLB validation, Open WebUI pinned image, optional LiteLLM 1.83.7 or later, Tailscale Services and grants.

## Global Constraints

- DeepSeek and TRELLIS.2 never run concurrently.
- TRELLIS.2 begins with 512-cubed acceptance and writes outputs to GPU node 2 local storage.
- No UI, LiteLLM, Tailscale, proxy, controller, or monitoring container runs on a GPU node.
- Every image is pinned by immutable digest; every source checkout is pinned by commit.
- UI and remote users reach AI services only through Caddy.
- LiteLLM is deployed only if the documented gate passes; otherwise its state is explicitly recorded as disabled.
- Tailscale uses a named service and restrictive grants/ACLs; no public port forwarding is allowed.
- Tasks 1–2 are in the immediate scope and use a GPU node 2 loopback endpoint through an SSH tunnel.
- Tasks 3–6 require the new external container host and are deferred until it is installed.

---

### Task 1: Audit and pin TRELLIS.2 for Vonk Forge GPU node

**Files:**
- Create: `docs/audits/trellis2.md`
- Modify: `locks/sources.toml`
- Modify: `locks/images.toml`
- Create: `profiles/trellis2/Dockerfile`
- Create: `profiles/trellis2/compose.yaml`
- Create: `profiles/trellis2/env.example`
- Create: `config/profiles/trellis2.toml`
- Create: `tests/profiles/test_trellis2_profile.py`

**Interfaces:**
- Produces: node-local Compose project `vonk-trellis2` on GPU node 2 and loopback port 7860.
- Consumes: local checkpoint path and local output path; no NAS mount.

- [ ] **Step 1: Write failing placement and isolation tests**

```python
def test_trellis_runs_only_on_node2(profile):
    assert profile.nodes == ("node2",)
    assert profile.stop_conflicts == ("deepseek-baseline", "deepseek-draft", "deepseek-nvfp4", "deepseek-agent", "deepseek-long")

def test_trellis_has_no_nas_mount(compose):
    mounts = compose["services"]["trellis2"].get("volumes", [])
    assert all("nfs" not in str(m).lower() and "volume1" not in str(m).lower() for m in mounts)
```

- [ ] **Step 2: Verify tests fail**

Run: `pytest tests/profiles/test_trellis2_profile.py -v`

Expected: FAIL because the profile is absent.

- [ ] **Step 3: Select and audit an immutable upstream commit**

Resolve Microsoft TRELLIS.2 `HEAD`, inspect its license, dependency pins, checkpoint sources, CUDA kernels, web binding, file writes, and install commands, then record the reviewed commit and checksums. Do not execute its setup scripts until the audit identifies every package and write path.

- [ ] **Step 4: Build a pinned ARM64/CUDA image**

Use the installed GPU node CUDA/driver compatibility recorded in Plan 1. Run as non-root where GPU access permits, bind only local checkpoint/output directories, bind 7860 to `127.0.0.1`, set `restart: "no"`, and configure 50 MiB × 5 logs.

- [ ] **Step 5: Run profile tests and image smoke build**

Run: `pytest tests/profiles/test_trellis2_profile.py -v && docker compose -f profiles/trellis2/compose.yaml config --quiet`

Expected: PASS; the built image reports ARM64 and sees the GPU on GPU node 2.

- [ ] **Step 6: Commit TRELLIS artifacts**

```bash
git add docs/audits/trellis2.md locks profiles/trellis2 config/profiles/trellis2.toml tests/profiles/test_trellis2_profile.py
git commit -m "feat: add pinned TRELLIS2 profile"
```

### Task 2: Validate TRELLIS.2 output and switching

**Files:**
- Create: `validation/glb.py`
- Create: `tests/validation/test_glb.py`
- Create: `validation/fixtures/trellis-input.png`
- Create: `inventory/reports/trellis2.json`
- Create: `docs/runbooks/trellis2.md`

**Interfaces:**
- Produces: `validate_glb(path: Path) -> GlbReport` with `valid_header`, `version`, `declared_length`, `json_chunk`, `binary_chunk`, and `nonempty_scene`.

- [ ] **Step 1: Write failing GLB validator tests**

```python
def test_rejects_truncated_glb(tmp_path):
    path = tmp_path / "bad.glb"
    path.write_bytes(b"glTF\x02\x00\x00\x00\xff\xff\xff\xff")
    assert validate_glb(path).ok is False

def test_accepts_minimal_scene(valid_glb):
    report = validate_glb(valid_glb)
    assert report.ok is True
    assert report.nonempty_scene is True
```

- [ ] **Step 2: Verify tests fail**

Run: `pytest tests/validation/test_glb.py -v`

Expected: import failure for `validation.glb`.

- [ ] **Step 3: Implement binary GLB validation**

Parse the 12-byte header and aligned chunks with `struct`; require magic `glTF`, version 2, exact declared length, a JSON chunk, a binary chunk, and at least one scene/node/mesh reference.

- [ ] **Step 4: Run unit tests**

Run: `pytest tests/validation/test_glb.py -v`

Expected: PASS.

- [ ] **Step 5: Run live 512-cubed acceptance**

Stop DeepSeek, start TRELLIS.2, open `ssh -L 7860:127.0.0.1:7860 vonk-node-2`, submit the committed input image at 512-cubed resolution through the tunnel, validate the returned GLB, stop TRELLIS.2, and require GPU node 2 memory to recover within 5 GiB of baseline in 120 seconds.

- [ ] **Step 6: Commit TRELLIS evidence**

```bash
git add validation/glb.py validation/fixtures/trellis-input.png tests/validation/test_glb.py inventory/reports/trellis2.json docs/runbooks/trellis2.md
git commit -m "test: accept TRELLIS2 generation"
```

### Task 3: Deploy the external browser UI

**Files:**
- Create: `control/ui/compose.override.yaml`
- Create: `control/ui/config.example.env`
- Create: `tests/control/test_ui_compose.py`
- Create: `docs/runbooks/browser-ui.md`

**Interfaces:**
- Produces: external UI on port 3000 using only `https://node-gateway.home.arpa:8443/v1`.
- Resource limit: 1 CPU and 2 GiB; host gate: 4 GiB installed, 2 GiB available, 20 GiB free disk.

- [ ] **Step 1: Write failing placement and endpoint tests**

```python
def test_ui_uses_only_gateway(compose):
    env = compose["services"]["ui"]["environment"]
    assert env["OPENAI_API_BASE_URL"] == "https://node-gateway.home.arpa:8443/v1"
    assert "192.168.1.211" not in json.dumps(env)

def test_ui_resource_limits(compose):
    assert compose["services"]["ui"]["cpus"] == 1.0
    assert compose["services"]["ui"]["mem_limit"] == "2g"
```

- [ ] **Step 2: Verify tests fail**

Run: `pytest tests/control/test_ui_compose.py -v`

Expected: FAIL because the UI Compose override is absent.

- [ ] **Step 3: Pin and configure the UI image**

Resolve the selected stable Open WebUI image to an immutable digest. Disable unauthenticated signup after the first admin is created, mount persistent UI data only on the external host, inject the Caddy API key through a secret, and configure bounded logs.

- [ ] **Step 4: Run configuration tests**

Run: `docker compose -f control/compose.yaml -f control/ui/compose.override.yaml config --quiet && pytest tests/control/test_ui_compose.py -v`

Expected: PASS.

- [ ] **Step 5: Deploy and validate model visibility**

With maintenance active, require the UI to show no healthy model. Advertise `deepseek-agent`, require only that model to appear, switch to TRELLIS.2, and require the UI to remove DeepSeek and show the TRELLIS route appropriate to the selected UI integration.

- [ ] **Step 6: Commit UI deployment**

```bash
git add control/ui tests/control/test_ui_compose.py docs/runbooks/browser-ui.md
git commit -m "feat: add external GPU node browser UI"
```

### Task 4: Apply the LiteLLM deployment gate

**Files:**
- Create: `decisions/litellm.toml`
- Create: `control/litellm/compose.override.yaml`
- Create: `control/litellm/config.example.yaml`
- Create: `tests/control/test_litellm_gate.py`
- Create: `docs/runbooks/litellm.md`

**Interfaces:**
- Produces: decision `enabled = true|false`, reason, date, evaluated endpoints, and resource evidence.
- Deployment requires LiteLLM 1.83.7 or later, immutable signed image, 1 CPU/1 GiB, and external host with 6 GiB installed/3 GiB available for UI plus LiteLLM.

- [ ] **Step 1: Write failing decision-gate tests**

```python
def test_single_endpoint_defaults_to_disabled(decision):
    result = evaluate_litellm(endpoints=["caddy"], needs_quotas=False, needs_usage_db=False)
    assert result.enabled is False

def test_enabled_requires_security_floor(decision):
    decision.enabled = True
    decision.version = "1.83.6"
    with pytest.raises(GateError, match="1.83.7"):
        validate_litellm_decision(decision)
```

- [ ] **Step 2: Verify tests fail**

Run: `pytest tests/control/test_litellm_gate.py -v`

Expected: FAIL because the gate is absent.

- [ ] **Step 3: Implement and evaluate the gate**

Enable only if at least two simultaneously useful upstream endpoints require aliases/routing, or quotas/central usage accounting are explicitly required. With the initial mutually exclusive single healthy profile, record `enabled = false` and keep Caddy as the only gateway. If the gate is true, require all security and resource constraints before rendering Compose.

- [ ] **Step 4: Run gate tests**

Run: `pytest tests/control/test_litellm_gate.py -v`

Expected: PASS; initial decision is disabled unless measured requirements changed.

- [ ] **Step 5: Commit the explicit decision**

```bash
git add decisions/litellm.toml control/litellm tests/control/test_litellm_gate.py docs/runbooks/litellm.md
git commit -m "docs: record LiteLLM deployment decision"
```

### Task 5: Add external Tailscale ingress

**Files:**
- Create: `control/tailscale/compose.override.yaml`
- Create: `control/tailscale/serve.json`
- Create: `control/tailscale/grants.example.json`
- Create: `tests/control/test_tailscale_policy.py`
- Create: `docs/runbooks/tailscale.md`

**Interfaces:**
- Produces: named Tailscale Service for Caddy and a restricted subnet route only if remote GPU node SSH is required.
- No Tailscale process runs on either GPU node.

- [ ] **Step 1: Write failing policy tests**

```python
def test_service_targets_only_caddy(serve_config):
    assert destinations(serve_config) == {"https://caddy:8443"}

def test_no_public_or_wildcard_grant(grants):
    assert "*" not in grant_sources(grants)
    assert all("0.0.0.0/0" not in value for value in json.dumps(grants).split())
```

- [ ] **Step 2: Verify tests fail**

Run: `pytest tests/control/test_tailscale_policy.py -v`

Expected: FAIL because policy files are absent.

- [ ] **Step 3: Pin and configure the external Tailscale deployment**

Resolve the official image to an immutable digest, store the auth key in 1Password, inject it as a runtime secret, persist only Tailscale state on the external host, and advertise the named Caddy service. Do not advertise a subnet route until a remote SSH requirement is recorded.

- [ ] **Step 4: Write least-privilege grants**

Allow only named user/group identities to the Caddy service. If subnet SSH is enabled, grant only TCP 22 to `192.168.1.211/32` and `192.168.1.212/32`; do not grant general LAN access.

- [ ] **Step 5: Run policy/configuration tests**

Run: `pytest tests/control/test_tailscale_policy.py -v && docker compose -f control/compose.yaml -f control/tailscale/compose.override.yaml config --quiet`

Expected: PASS.

- [ ] **Step 6: Validate remote behavior**

From an approved tailnet client, require authenticated access to the named service, rejection from an unapproved identity, no public reachability, and no direct GPU node API reachability.

- [ ] **Step 7: Commit remote-access configuration**

```bash
git add control/tailscale tests/control/test_tailscale_policy.py docs/runbooks/tailscale.md
git commit -m "feat: add restricted Tailscale ingress"
```

### Task 6: Run full platform switching acceptance

**Files:**
- Create: `validation/platform_acceptance.py`
- Create: `tests/validation/test_platform_acceptance.py`
- Create: `inventory/reports/platform-acceptance.json`
- Create: `docs/runbooks/operations.md`

**Interfaces:**
- Produces: one final report covering DeepSeek, TRELLIS.2, UI, Caddy, controller, reboot, failure, and Tailscale behavior.

- [ ] **Step 1: Write failing sequence tests with fake backends**

```python
def test_full_sequence(acceptance, events):
    acceptance.run()
    assert events == [
        "maintenance", "deepseek-agent", "maintenance", "trellis2",
        "maintenance", "reboot-check", "remote-access-check",
    ]
```

- [ ] **Step 2: Verify tests fail**

Run: `pytest tests/validation/test_platform_acceptance.py -v`

Expected: import failure for `validation.platform_acceptance`.

- [ ] **Step 3: Implement the acceptance orchestrator**

Call existing controller and validators; do not duplicate their logic. Record pins, timestamps, boot IDs, state transitions, endpoint status, output-quality summaries, GLB report, UI visibility, and Tailscale authorization results.

- [ ] **Step 4: Run unit tests**

Run: `pytest tests/validation/test_platform_acceptance.py -v`

Expected: PASS.

- [ ] **Step 5: Run live acceptance**

Run: `python -m validation.platform_acceptance --config inventory/cluster.toml --output inventory/reports/platform-acceptance.json`

Expected: every required phase passes; any failure leaves Caddy in maintenance and both heavyweight profiles stopped.

- [ ] **Step 6: Commit final operations baseline**

```bash
git add validation/platform_acceptance.py tests/validation/test_platform_acceptance.py inventory/reports/platform-acceptance.json docs/runbooks/operations.md
git commit -m "test: accept complete dual-GPU node platform"
```
