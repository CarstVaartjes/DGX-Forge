# External Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and deploy the external one-shot profile controller and NAS-hosted Caddy gateway while keeping both Sparks free of non-AI containers.

**Architecture:** A dependency-light Python CLI runs as an ephemeral container on the DS218+ or another external host. It persists state and a kernel `flock` on a bind mount, loads Caddy configuration over a private Compose network, and invokes a forced `spark-nodectl` command over dedicated SSH keys. Caddy is the sole client endpoint and fails closed to HTTP 503.

**Tech Stack:** Python 3.12 standard library, pytest, TOML, OpenSSH, Bash, Docker Compose, Caddy 2 pinned by digest, private-CA TLS.

## Global Constraints

- No Caddy, controller, UI, LiteLLM, Tailscale, or monitoring container may run on either Spark.
- Controller runtime dependencies are Python standard library plus the OpenSSH client; test-only dependency is pytest.
- Controller commands are one-shot and use `restart: "no"`.
- Persistent controller state path inside the container is `/var/lib/dgx-spark-platform`.
- Caddy admin port 2019 is reachable only on the private Compose network.
- vLLM port 8888 is accepted only from the external gateway IP and local host.
- A switch drains for 300 seconds by default, allows 30–1,800 seconds, stops with 120-second grace, and waits at most 900 seconds for health.
- Every failure restores Caddy maintenance mode and leaves heavyweight workloads stopped.
- Do not execute this plan until the new NAS or another approved non-Spark container host is installed, inventoried, and reachable.
- Consume the validated node-local DeepSeek and TRELLIS.2 profile scripts from the preceding local-runtime plans.

---

### Task 1: Scaffold controller configuration and immutable domain types

**Files:**
- Create: `control/controller/pyproject.toml`
- Create: `control/controller/src/spark_controller/__init__.py`
- Create: `control/controller/src/spark_controller/config.py`
- Create: `control/controller/tests/test_config.py`
- Create: `config/hosts.example.toml`
- Create: `config/profiles/maintenance.toml`

**Interfaces:**
- Produces: `HostConfig`, `ProfileConfig`, `ControllerConfig`, and `load_config(root: Path) -> ControllerConfig`.
- `HostConfig` fields: `name`, `role`, `ssh_alias`, `lan_ip`, `fabric_ip`.
- `ProfileConfig` fields: `name`, `nodes`, `upstream`, `start_order`, `stop_order`, `health_timeout_seconds`, `drain_timeout_seconds`.

- [ ] **Step 1: Write failing TOML parsing tests**

```python
def test_loads_maintenance_profile(config_root):
    cfg = load_config(config_root)
    assert cfg.hosts["spark1"].role == "head"
    assert cfg.profiles["maintenance"].nodes == ()
    assert cfg.profiles["maintenance"].upstream is None

def test_rejects_unknown_profile_node(config_root):
    write_profile(config_root, nodes=["spark3"])
    with pytest.raises(ConfigError, match="unknown node spark3"):
        load_config(config_root)
```

- [ ] **Step 2: Verify tests fail before implementation**

Run: `pytest control/controller/tests/test_config.py -v`

Expected: import failure for `spark_controller.config`.

- [ ] **Step 3: Implement frozen dataclasses and TOML validation**

```python
@dataclass(frozen=True)
class HostConfig:
    name: str
    role: Literal["head", "worker"]
    ssh_alias: str
    lan_ip: str
    fabric_ip: str

@dataclass(frozen=True)
class ProfileConfig:
    name: str
    nodes: tuple[str, ...]
    upstream: str | None
    start_order: tuple[str, ...]
    stop_order: tuple[str, ...]
    health_timeout_seconds: int = 900
    drain_timeout_seconds: int = 300
```

Use `tomllib`; reject duplicate names, missing hosts, timeout values outside the approved bounds, and start/stop orders that do not contain each declared node exactly once.

- [ ] **Step 4: Run tests**

Run: `pytest control/controller/tests/test_config.py -v`

Expected: PASS.

- [ ] **Step 5: Commit the configuration contract**

```bash
git add control/controller config/hosts.example.toml config/profiles/maintenance.toml
git commit -m "feat: define controller configuration"
```

### Task 2: Implement persistent state and cross-container locking

**Files:**
- Create: `control/controller/src/spark_controller/state.py`
- Create: `control/controller/tests/test_state.py`

**Interfaces:**
- Produces: `ControllerState`, `StateStore.load()`, `StateStore.save_atomic(state)`, and `SwitchLock.acquire()`.
- State values: `stopped`, `transitioning`, `active`, `recovery-required`, and `stopped-after-reboot`.

- [ ] **Step 1: Write failing atomic-state and lock tests**

```python
def test_atomic_state_round_trip(tmp_path):
    store = StateStore(tmp_path)
    state = ControllerState.stopped(boot_ids={"spark1": "a", "spark2": "b"})
    store.save_atomic(state)
    assert store.load() == state
    assert not (tmp_path / "state.json.tmp").exists()

def test_second_lock_holder_is_rejected(tmp_path):
    with SwitchLock(tmp_path / "switch.lock"):
        with pytest.raises(LockBusy):
            with SwitchLock(tmp_path / "switch.lock", nonblocking=True):
                pass
```

- [ ] **Step 2: Run tests and verify failure**

Run: `pytest control/controller/tests/test_state.py -v`

Expected: import failure for `spark_controller.state`.

- [ ] **Step 3: Implement JSON state and `fcntl.flock`**

Write state to a same-directory temporary file, `fsync`, then `os.replace`. Store schema version, prior/target/active profile, phase, PID, UTC timestamp, last error, and both boot IDs. Use `LOCK_EX | LOCK_NB`; lock-file contents are diagnostic only.

- [ ] **Step 4: Run state tests**

Run: `pytest control/controller/tests/test_state.py -v`

Expected: PASS, including subprocess contention tests.

- [ ] **Step 5: Commit state management**

```bash
git add control/controller/src/spark_controller/state.py control/controller/tests/test_state.py
git commit -m "feat: persist controller state and lock"
```

### Task 3: Define the restricted node command protocol

**Files:**
- Create: `nodes/bin/spark-nodectl`
- Create: `nodes/etc/sudoers.d/spark-nodectl`
- Create: `tests/nodes/test_spark_nodectl.py`
- Create: `docs/runbooks/controller-ssh.md`

**Interfaces:**
- Consumes: `SSH_ORIGINAL_COMMAND` with one of `status`, `doctor`, `profile-render NAME`, `profile-start NAME`, `profile-stop NAME`, or `profile-logs NAME LINES`.
- Produces: exactly one JSON object on stdout with `ok`, `operation`, `node`, `data`, and `error`.

- [ ] **Step 1: Write failing allowlist tests**

```python
@pytest.mark.parametrize("command", ["bash", "rm -rf x", "profile-start bad/name", "profile-logs deepseek 100000"])
def test_rejects_unapproved_commands(run_nodectl, command):
    result = run_nodectl(command)
    assert result.returncode == 64
    assert json.loads(result.stdout)["ok"] is False

def test_status_returns_json(run_nodectl):
    result = run_nodectl("status")
    assert json.loads(result.stdout)["operation"] == "status"
```

- [ ] **Step 2: Run tests and verify failure**

Run: `pytest tests/nodes/test_spark_nodectl.py -v`

Expected: FAIL because the command does not exist.

- [ ] **Step 3: Implement strict command parsing**

Use a Bash `case` over exact token counts; accept profile names matching `^[a-z0-9][a-z0-9-]{0,31}$` and log lines from 1 through 1000. Set a fixed `PATH`, clear inherited environment variables, and call only repository-owned profile scripts through narrowly scoped `sudo` rules.

- [ ] **Step 4: Run unit and ShellCheck tests**

Run: `pytest tests/nodes/test_spark_nodectl.py -v && shellcheck nodes/bin/spark-nodectl`

Expected: PASS.

- [ ] **Step 5: Document the authorized-key restriction**

Use this form with the generated controller public key and the observed external-host LAN IP:

```text
from="CONTROL_HOST_IP",restrict,command="/usr/local/sbin/spark-nodectl" ssh-ed25519 CONTROLLER_PUBLIC_KEY DGX-Spark-Controller
```

The implementation substitutes the two measured values before installation and never commits the private key.

- [ ] **Step 6: Commit the node protocol**

```bash
git add nodes/bin/spark-nodectl nodes/etc/sudoers.d/spark-nodectl tests/nodes/test_spark_nodectl.py docs/runbooks/controller-ssh.md
git commit -m "security: add restricted Spark node control"
```

### Task 4: Implement the SSH remote runner

**Files:**
- Create: `control/controller/src/spark_controller/remote.py`
- Create: `control/controller/tests/test_remote.py`

**Interfaces:**
- Produces: `RemoteRunner.run(host: HostConfig, operation: str, *args: str, timeout: int = 60) -> NodeResult`.
- `NodeResult` fields: `ok`, `operation`, `node`, `data`, `error`.

- [ ] **Step 1: Write failing subprocess and JSON validation tests**

```python
def test_remote_uses_batch_mode(fake_ssh, host):
    RemoteRunner(binary=fake_ssh).run(host, "status")
    assert "BatchMode=yes" in fake_ssh.argv
    assert fake_ssh.argv[-1] == "status"

def test_remote_rejects_non_json(fake_ssh, host):
    fake_ssh.stdout = "motd noise"
    with pytest.raises(RemoteProtocolError):
        RemoteRunner(binary=fake_ssh).run(host, "status")
```

- [ ] **Step 2: Verify tests fail**

Run: `pytest control/controller/tests/test_remote.py -v`

Expected: import failure for `spark_controller.remote`.

- [ ] **Step 3: Implement SSH execution**

Build an argv list, never a shell string. Set `BatchMode=yes`, `IdentitiesOnly=yes`, `ConnectTimeout=8`, the mounted key path, and a pinned known-hosts file. Reject extra stdout, schema mismatches, timeouts, and nonzero exits.

- [ ] **Step 4: Run tests**

Run: `pytest control/controller/tests/test_remote.py -v`

Expected: PASS.

- [ ] **Step 5: Commit remote execution**

```bash
git add control/controller/src/spark_controller/remote.py control/controller/tests/test_remote.py
git commit -m "feat: add restricted remote runner"
```

### Task 5: Generate and load fail-closed Caddy configurations

**Files:**
- Create: `control/controller/src/spark_controller/gateway.py`
- Create: `control/controller/tests/test_gateway.py`
- Create: `control/caddy/maintenance.json`

**Interfaces:**
- Produces: `Gateway.set_maintenance(reason: str)`, `Gateway.set_draining()`, and `Gateway.set_active(profile: str, upstream: str)`.
- Caddy API base URL is `http://caddy:2019`; externally served address is `:8443`.

- [ ] **Step 1: Write failing configuration tests**

```python
def test_maintenance_returns_503(gateway):
    config = gateway.render_maintenance("stopped-after-reboot")
    assert find_status(config) == 503
    assert find_header(config, "Retry-After") == "30"

def test_active_route_requires_allowlisted_upstream(gateway):
    with pytest.raises(GatewayConfigError):
        gateway.render_active("deepseek", "http://example.com:8888")
```

- [ ] **Step 2: Verify tests fail**

Run: `pytest control/controller/tests/test_gateway.py -v`

Expected: import failure for `spark_controller.gateway`.

- [ ] **Step 3: Implement JSON rendering and atomic load**

Render Caddy JSON with private-CA TLS, bearer-key matchers read from `/run/secrets/gateway_api_keys`, access logs, active health checks, and an error route that returns 503. POST the full JSON to `/load`; then GET `/config/` and verify the expected profile marker.

- [ ] **Step 4: Run tests**

Run: `pytest control/controller/tests/test_gateway.py -v`

Expected: PASS with bearer secrets absent from assertion output.

- [ ] **Step 5: Commit gateway configuration**

```bash
git add control/controller/src/spark_controller/gateway.py control/controller/tests/test_gateway.py control/caddy/maintenance.json
git commit -m "feat: add fail-closed Caddy control"
```

### Task 6: Implement the switch state machine and CLI

**Files:**
- Create: `control/controller/src/spark_controller/switch.py`
- Create: `control/controller/src/spark_controller/cli.py`
- Create: `control/controller/tests/test_switch.py`
- Create: `control/controller/tests/test_cli.py`

**Interfaces:**
- Produces: `SwitchService.switch(profile_name: str) -> ControllerState` and CLI commands `switch`, `start`, `stop`, `status`, `logs`, `doctor`, `recover --force`.
- Consumes: `StateStore`, `SwitchLock`, `RemoteRunner`, `Gateway`, and loaded profiles.

- [ ] **Step 1: Write failing ordering and failure tests**

```python
def test_switch_orders_drain_stop_start_health_advertise(service, events):
    service.switch("deepseek-baseline")
    assert events == [
        "gateway:draining", "remote:spark1:profile-stop", "remote:spark2:profile-stop",
        "validate:target", "remote:spark2:profile-start", "remote:spark1:profile-start",
        "health:deepseek-baseline", "quality:deepseek-baseline", "gateway:active",
    ]

def test_failed_health_stops_partial_target(service, gateway):
    service.health.fail = True
    with pytest.raises(SwitchFailed):
        service.switch("deepseek-baseline")
    assert gateway.mode == "maintenance"
    assert service.state.load().status == "stopped"
```

- [ ] **Step 2: Verify tests fail**

Run: `pytest control/controller/tests/test_switch.py control/controller/tests/test_cli.py -v`

Expected: import failure for switch and CLI modules.

- [ ] **Step 3: Implement the exact twelve-phase transition**

Persist state before and after each phase. Poll active requests until zero or the configured drain deadline; stop head before worker, start worker before head, validate all remote JSON responses, and always call `set_maintenance` in the failure handler before stopping partial services.

- [ ] **Step 4: Implement `recover --force` safeguards**

Require no live controller lock, matching current boot IDs, and `status` from both nodes showing no active profile containers. Otherwise exit 75 and preserve `recovery-required`.

- [ ] **Step 5: Run controller tests**

Run: `pytest control/controller/tests -v`

Expected: PASS.

- [ ] **Step 6: Commit controller behavior**

```bash
git add control/controller/src/spark_controller control/controller/tests
git commit -m "feat: implement fail-to-stopped switching"
```

### Task 7: Package the external control plane

**Files:**
- Create: `control/controller/Dockerfile`
- Create: `control/compose.yaml`
- Create: `control/.env.example`
- Create: `bin/sparkctl`
- Create: `tests/control/test_compose.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `bin/sparkctl COMMAND [ARGS]` as the only operator entry point.
- Compose services: long-running `caddy` and profile-only one-shot `controller`.

- [ ] **Step 1: Write failing Compose policy tests**

```python
def test_only_caddy_can_restart(compose):
    assert compose["services"]["caddy"]["restart"] == "unless-stopped"
    assert compose["services"]["controller"]["restart"] == "no"

def test_caddy_admin_is_not_published(compose):
    assert all("2019" not in str(port) for port in compose["services"]["caddy"].get("ports", []))
```

- [ ] **Step 2: Verify tests fail**

Run: `pytest tests/control/test_compose.py -v`

Expected: FAIL because Compose is absent.

- [ ] **Step 3: Create pinned images and resource limits**

Use an immutable Caddy digest. Build the controller from a pinned `python:3.12-slim` digest, install only `openssh-client` and CA certificates, run as an unprivileged UID, mount state read-write, and mount keys/config/secrets read-only. Limit Caddy to 0.5 CPU/256 MiB and configure `50m` × 5 JSON logs.

- [ ] **Step 4: Implement the wrapper**

```bash
#!/usr/bin/env bash
set -euo pipefail
exec docker compose --project-directory "$(cd "$(dirname "$0")/../control" && pwd)" run --rm controller "$@"
```

- [ ] **Step 5: Run packaging checks**

Run: `docker compose -f control/compose.yaml config --quiet && pytest tests/control/test_compose.py -v && shellcheck bin/sparkctl`

Expected: all checks PASS.

- [ ] **Step 6: Commit packaging**

```bash
git add .gitignore bin/sparkctl control tests/control/test_compose.py
git commit -m "build: package external Spark control plane"
```

### Task 8: Deploy and validate the control plane end to end

**Files:**
- Create: `docs/runbooks/control-plane-deployment.md`
- Create: `inventory/reports/control-plane.json`

**Interfaces:**
- Consumes: external-host IP/resources, Spark SSH aliases from Plan 1, and validated node-local profiles from the DeepSeek/TRELLIS plans.
- Produces: a running Caddy maintenance endpoint and functional restricted controller access.

- [ ] **Step 1: Verify external-host gates**

Record DSM/OS, CPU architecture, Container Manager/Docker, installed/available memory, and free disk. Require at least 1 GiB available memory and 5 GiB free disk for Caddy/controller alone.

- [ ] **Step 2: Generate the controller key on the external host**

Create a dedicated Ed25519 key under the control-plane secret directory, mode 0600. Install only its public key on each Spark using the forced-command and source-address restriction from Task 3.

- [ ] **Step 3: Install node-control artifacts**

Install `spark-nodectl` root-owned mode 0755 and its sudoers file mode 0440 on Spark 2, validate with `visudo -cf`, test the restricted key, then repeat on Spark 1.

- [ ] **Step 4: Start Caddy in maintenance mode**

Run: `docker compose -f control/compose.yaml up -d caddy`

Expected: authenticated and unauthenticated HTTPS probes return the documented 503/401 behavior; port 2019 is unreachable from the LAN.

Install the private CA root only on approved clients. Configure the Spark host firewalls so future upstream ports accept the measured external-gateway source IP and local host only; keep them loopback-only until this check passes.

- [ ] **Step 5: Test controller status and locking**

Run two concurrent `bin/sparkctl status`/switch-lock probes. Expected: valid JSON status from both nodes and exactly one lock holder; no profile starts.

- [ ] **Step 6: Integrate the validated DeepSeek baseline**

Run `bin/sparkctl switch deepseek-baseline`. Expected: Caddy drains, the controller invokes the node-local scripts validated in the DeepSeek plan, the worker starts before the head, the direct quality gate passes again through Caddy, and Caddy exposes only the authenticated model endpoint.

- [ ] **Step 7: Test fail-closed and reboot behavior**

Load a deliberately unreachable allowlisted upstream in a test profile. Expected: Caddy returns 503 and controller state remains stopped. Reboot both Sparks while the external host stays up; require Caddy 503, controller state `stopped-after-reboot`, and no AI auto-start. Remove the test profile after recording the result.

Rotate the Caddy bearer key by accepting old and new keys together, moving a test client to the new key, proving the old key still works during the overlap, removing the old key, and proving it then receives 401. Store only key IDs and rotation dates in the report; obtain key material from 1Password runtime secrets.

- [ ] **Step 8: Commit deployment evidence**

```bash
git add docs/runbooks/control-plane-deployment.md inventory/reports/control-plane.json
git commit -m "ops: validate external Spark control plane"
```
