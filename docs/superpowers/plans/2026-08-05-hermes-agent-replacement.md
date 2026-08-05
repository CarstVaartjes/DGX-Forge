# Hermes Agent Replacement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the SSH AI devbox with a hardened, persistent Hermes Agent that is reachable only through two exact Tailscale Services and uses only the best eligible, already-running local model through LiteLLM.

**Architecture:** A digest-pinned official Hermes image is wrapped only to read its API key from a Compose secret before preserving the upstream s6 entrypoint. A strict repository policy derives an ordered `hermes-agent` LiteLLM group from accepted workloads already present in the active reconciliation, with endpoints constructed only from authenticated Spark presence. Hermes has separate edge, inference, and controlled-egress networks and no host or control-plane privileges.

**Tech Stack:** Docker Compose, Nous Research Hermes Agent v2026.7.20, Tailscale Services, LiteLLM v1.82.3, Python 3.12, FastAPI control worker, TOML/JSON policy, pytest, POSIX shell.

## Global Constraints

- Hermes is the only user-facing development agent; no standing SSH service remains.
- Hermes uses `http://litellm:4000/v1` and model name `hermes-agent`; it never receives a Spark address.
- `local_only = true` is mandatory and no cloud model or arbitrary URL is accepted.
- Candidate workloads must be present in the pinned repository, have current maturity `accepted`, be part of the active reconciliation, have fresh authenticated presence, pass a bounded probe, and have a repository-declared port.
- Candidate priority is unique and ascending; the dual-Spark DeepSeek workload precedes the single-Spark DeepSeek workload.
- Tailscale exposes exactly `svc:dgx-forge`, `svc:hermes-dashboard`, and `svc:hermes-api`; Docker publishes no Hermes port.
- Hermes receives no Docker socket, devices, privileged mode, host networking, control-plane administrator token, Spark agent PKI, registry credential, or Tailscale OAuth credential.
- The Hermes root filesystem is read-only and writable storage is limited to `/opt/data`, `/workspace`, `/opt/data/home/.cache`, and bounded temporary filesystems.
- The official image is `nousresearch/hermes-agent:v2026.7.20@sha256:f7b35053268f532f98955195c909f15a230470fbcbdacaa9fdecb95707dad04a`.
- Runtime fallback is limited to connection failures and HTTP 429, 502, 503, or 504 before completion content begins; a partial stream is never replayed.

---

### Task 1: Strict local-only Hermes policy

**Files:**
- Create: `config/hermes-agent-policy.toml`
- Create: `control/src/dgx_control/hermes_policy.py`
- Create: `control/tests/test_hermes_policy.py`

**Interfaces:**
- Consumes: commit-pinned bytes from `read_commit_file()`, active route workload IDs, and `inventory/reports/model-definitions.json` maturity records.
- Produces: `HermesAgentPolicy.parse(content: bytes, *, known_workloads: AbstractSet[str]) -> HermesAgentPolicy` and `HermesAgentPolicy.eligible(active_workloads: AbstractSet[str], maturity: Mapping[str, str]) -> tuple[HermesCandidate, ...]`.

- [ ] **Step 1: Write failing parser and selection tests**

```python
def test_policy_selects_only_active_accepted_candidates_in_priority_order() -> None:
    policy = HermesAgentPolicy.parse(POLICY, known_workloads={"deepseek-agent-dual", "deepseek-agent-single"})
    assert [item.workload for item in policy.eligible(
        {"deepseek-agent-dual", "deepseek-agent-single"},
        {"deepseek-agent-dual": "accepted", "deepseek-agent-single": "accepted"},
    )] == ["deepseek-agent-dual", "deepseek-agent-single"]

@pytest.mark.parametrize("mutation", ("unknown-field", "remote", "duplicate-priority", "unknown-workload", "below-accepted"))
def test_policy_rejects_nonlocal_or_ambiguous_configuration(mutation: str) -> None:
    bad_policies = {
        "unknown-field": POLICY + b'cloud_fallback = "openai"\n',
        "remote": POLICY.replace(b"local_only = true", b"local_only = false"),
        "duplicate-priority": POLICY.replace(b"priority = 2", b"priority = 1"),
        "unknown-workload": POLICY.replace(b"deepseek-agent-single", b"remote-agent"),
        "below-accepted": POLICY.replace(b'minimum_maturity = "accepted"', b'minimum_maturity = "verified"', 1),
    }
    with pytest.raises(HermesPolicyError):
        HermesAgentPolicy.parse(
            bad_policies[mutation],
            known_workloads={"deepseek-agent-dual", "deepseek-agent-single"},
        )
```

- [ ] **Step 2: Run the policy tests and confirm the module is missing**

Run: `uv run --project control pytest -q control/tests/test_hermes_policy.py`

Expected: collection fails because `dgx_control.hermes_policy` does not exist.

- [ ] **Step 3: Implement the immutable policy types and strict parser**

```python
@dataclass(frozen=True)
class HermesCandidate:
    workload: str
    priority: int
    minimum_maturity: str

@dataclass(frozen=True)
class HermesAgentPolicy:
    schema_version: int
    alias: str
    local_only: bool
    candidates: tuple[HermesCandidate, ...]

    @classmethod
    def parse(cls, content: bytes, *, known_workloads: AbstractSet[str]) -> "HermesAgentPolicy":
        try:
            document = tomllib.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
            raise HermesPolicyError("Hermes policy is invalid TOML") from error
        if set(document) != {"schema_version", "alias", "local_only", "candidates"}:
            raise HermesPolicyError("Hermes policy fields are invalid")
        if document["schema_version"] != 1 or document["alias"] != "hermes-agent" or document["local_only"] is not True:
            raise HermesPolicyError("Hermes policy must be version one and local-only")
        rows = document["candidates"]
        if not isinstance(rows, list) or not rows:
            raise HermesPolicyError("Hermes candidates are required")
        candidates = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"workload", "priority", "minimum_maturity"}:
                raise HermesPolicyError("Hermes candidate fields are invalid")
            candidate = HermesCandidate(row["workload"], row["priority"], row["minimum_maturity"])
            if candidate.workload not in known_workloads or candidate.minimum_maturity != "accepted" or isinstance(candidate.priority, bool) or not isinstance(candidate.priority, int) or candidate.priority < 1:
                raise HermesPolicyError("Hermes candidate is not an accepted local workload")
            candidates.append(candidate)
        if len({item.workload for item in candidates}) != len(candidates) or len({item.priority for item in candidates}) != len(candidates):
            raise HermesPolicyError("Hermes candidates must have unique workloads and priorities")
        return cls(1, "hermes-agent", True, tuple(sorted(candidates, key=lambda item: item.priority)))

    def eligible(self, active_workloads: AbstractSet[str], maturity: Mapping[str, str]) -> tuple[HermesCandidate, ...]:
        return tuple(
            candidate
            for candidate in self.candidates
            if candidate.workload in active_workloads
            and maturity.get(candidate.workload) == candidate.minimum_maturity
        )
```

The parser accepts exactly `schema_version`, `alias`, `local_only`, and `candidates`; each candidate accepts exactly `workload`, `priority`, and `minimum_maturity`. It requires version `1`, alias `hermes-agent`, boolean `local_only = true`, positive unique priorities, unique known workloads, and `minimum_maturity = "accepted"`.

- [ ] **Step 4: Add the initial repository policy**

```toml
schema_version = 1
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
```

- [ ] **Step 5: Run the policy tests and commit**

Run: `uv run --project control pytest -q control/tests/test_hermes_policy.py`

```bash
git add config/hermes-agent-policy.toml control/src/dgx_control/hermes_policy.py control/tests/test_hermes_policy.py
git commit -m "feat: define local-only Hermes model policy"
```

---

### Task 2: Ordered Hermes deployments in LiteLLM

**Files:**
- Modify: `control/src/dgx_control/litellm.py`
- Modify: `control/src/dgx_control/route_runtime.py`
- Modify: `control/tests/test_litellm.py`
- Modify: `control/tests/test_route_runtime.py`

**Interfaces:**
- Consumes: `HermesAgentPolicy`, normalized active reconciliation routes, repository maturity index, fresh `AgentPresenceService` observations, and existing route lease/probe machinery.
- Produces: `LiteLlmDeployment(model_name: str, workload: str, api_base: str, priority: int, requests_per_minute: int, tokens_per_minute: int)` and a generated LiteLLM `hermes-agent` group ordered by policy priority.

- [ ] **Step 1: Write failing LiteLLM rendering tests**

```python
def test_hermes_group_is_local_ordered_and_retry_bounded(tmp_path) -> None:
    content = publisher.render(routes, LiteLlmPolicy(models={}, deployments=(
        LiteLlmDeployment("hermes-agent", "deepseek-agent-dual", "http://10.0.0.42:8888/v1", 1, 30, 10000),
        LiteLlmDeployment("hermes-agent", "deepseek-agent-single", "http://10.0.0.43:8888/v1", 2, 30, 10000),
    )))
    config = json.loads(content)
    assert [item["litellm_params"]["model"] for item in config["model_list"]] == [
        "openai/deepseek-agent-dual", "openai/deepseek-agent-single"
    ]
    assert [item["order"] for item in config["model_list"]] == [1, 2]
    assert config["router_settings"]["routing_strategy"] == "priority-based-routing"
```

Also assert every `api_base` is an already-rendered management IP URL, every API key is `os.environ/LITELLM_UPSTREAM_KEY`, and forbidden cloud/provider strings never appear.

- [ ] **Step 2: Run the rendering tests and confirm the deployment type is missing**

Run: `uv run --project control pytest -q control/tests/test_litellm.py`

Expected: import or constructor failure for `LiteLlmDeployment`.

- [ ] **Step 3: Add deployment rendering without changing ordinary model routes**

```python
@dataclass(frozen=True)
class LiteLlmDeployment:
    model_name: str
    workload: str
    api_base: str
    priority: int
    requests_per_minute: int
    tokens_per_minute: int

@dataclass(frozen=True)
class LiteLlmPolicy:
    models: Mapping[str, Mapping[str, int]]
    deployments: tuple[LiteLlmDeployment, ...] = ()
```

Render duplicate `model_name = "hermes-agent"` entries with distinct OpenAI-compatible workload model names and ascending `order`. Configure priority routing and explicit retry policy entries only for connection, rate-limit, and service-unavailable classes. Keep zero retries for authentication, validation, and content errors.

- [ ] **Step 4: Write failing route-manager selection tests**

Cover these behaviors with literal repository fixtures:

- `test_dual_candidate_outranks_simultaneously_running_single_candidate` asserts the two generated `hermes-agent` entries have workloads `deepseek-agent-dual`, then `deepseek-agent-single`, with orders `1`, then `2`.
- `test_verified_single_candidate_is_not_added_to_hermes_group` marks the single workload `verified` and asserts no generated deployment names it.
- `test_mixed_profile_can_publish_an_accepted_single_candidate` marks the single workload accepted and active without the dual workload, then asserts it is the sole `hermes-agent` entry.
- `test_failed_primary_probe_leaves_only_an_eligible_secondary` makes only the dual probe fail and asserts publication is maintenance until a subsequent fresh reconciliation publishes the single candidate alone.
- `test_no_eligible_candidate_omits_hermes_group_but_keeps_other_routes` asserts the ordinary profile alias remains and `hermes-agent` is absent.
- `test_v2_reconciliation_route_shape_is_normalized_without_trusting_an_address` supplies `workload_id`, `nodes`, `entrypoint_node_id`, `scheme`, `port`, `path`, and nested `quota`, then asserts the output address still comes from `AgentPresenceService` and the port from the workload file.

- [ ] **Step 5: Run the route-manager tests and confirm selection is absent**

Run: `uv run --project control pytest -q control/tests/test_route_runtime.py`

Expected: assertions fail because current output contains only reconciliation aliases.

- [ ] **Step 6: Load policy and maturity at the pinned commit and derive deployments**

Add private helpers that:

1. parse old and V2 route payloads into `workload`, entrypoint node, and quota without accepting a URL;
2. read `config/hermes-agent-policy.toml` and `inventory/reports/model-definitions.json` through `repository_reader(commit, path)`;
3. select only active accepted candidates;
4. construct endpoints from `AgentPresenceService` plus `_workload_port()`;
5. probe every selected endpoint before publication; and
6. pass ordered `LiteLlmDeployment` values to `LiteLlmPublisher`.

No-candidate output omits `hermes-agent`. A changed endpoint still publishes maintenance before probing. The lease expiry remains the minimum of all selected source-presence expirations.

- [ ] **Step 7: Run routing and LiteLLM tests and commit**

Run: `uv run --project control pytest -q control/tests/test_hermes_policy.py control/tests/test_litellm.py control/tests/test_route_runtime.py control/tests/test_runtime_handlers.py`

```bash
git add control/src/dgx_control/litellm.py control/src/dgx_control/route_runtime.py control/tests/test_litellm.py control/tests/test_route_runtime.py
git commit -m "feat: route Hermes through ordered local models"
```

---

### Task 3: Hardened Hermes Compose service

**Files:**
- Delete: `deploy/compose/ai-devbox/`
- Delete: `deploy/compose/tests/ai-devbox-runtime.sh`
- Delete: `deploy/compose/tests/test_ai_devbox.py`
- Create: `deploy/compose/hermes-agent/Dockerfile`
- Create: `deploy/compose/hermes-agent/entrypoint.sh`
- Create: `deploy/compose/hermes-agent/compose.yaml`
- Create: `deploy/compose/tests/hermes-agent-runtime.sh`
- Create: `deploy/compose/tests/test_hermes_agent.py`
- Modify: `deploy/compose/compose.yaml`
- Modify: `deploy/compose/tests/test.env`
- Modify: `deploy/compose/tests/test_networking.py`
- Modify: `deploy/compose/.env.example`

**Interfaces:**
- Consumes: `/run/secrets/hermes-api-key`, persistent `${HERMES_DATA_ROOT}/{data,workspaces,cache}`, and LiteLLM at `http://litellm:4000/v1`.
- Produces: healthy internal targets `hermes-agent:8642` and `hermes-agent:9119`, plus a one-time `hermes-setup` Compose profile sharing only `/opt/data`, `/workspace`, cache, inference, and egress.

- [ ] **Step 1: Replace devbox structural tests with failing Hermes contracts**

The rendered Compose assertions require:

```python
assert set(service["networks"]) == {"tailnet-hermes-edge", "hermes-inference", "hermes-egress"}
assert service["read_only"] is True
assert service["security_opt"] == ["no-new-privileges:true"]
assert not service.get("ports")
assert not service.get("devices")
assert not service.get("privileged")
assert "docker.sock" not in json.dumps(service)
assert targets == {"/opt/data", "/workspace", "/opt/data/home/.cache"}
```

Also require bounded CPU/memory/shm/logging, bounded `/run` and `/tmp`, a healthcheck for both local ports, a 32-byte minimum API key validation in the wrapper, and no SSH package, port, key, grant, or volume anywhere in the rendered project.

- [ ] **Step 2: Run the Compose tests and confirm the old devbox violates them**

Run: `uv run --project control pytest -q deploy/compose/tests/test_hermes_agent.py deploy/compose/tests/test_networking.py`

Expected: failure because `hermes-agent` and its networks do not exist.

- [ ] **Step 3: Add a pinned wrapper image and secret-loading entrypoint**

```dockerfile
ARG HERMES_IMAGE=nousresearch/hermes-agent:v2026.7.20@sha256:f7b35053268f532f98955195c909f15a230470fbcbdacaa9fdecb95707dad04a
FROM ${HERMES_IMAGE}
COPY --chmod=0755 entrypoint.sh /usr/local/bin/dgx-hermes-entrypoint
ENTRYPOINT ["/usr/local/bin/dgx-hermes-entrypoint"]
CMD ["gateway", "run"]
```

The wrapper rejects symlink, non-regular, oversized, short, whitespace-containing, or multi-line secret files, exports `API_SERVER_KEY`, and execs the upstream `/init /opt/hermes/docker/main-wrapper.sh "$@"` chain without logging the key.

- [ ] **Step 4: Add the normal and setup services**

The normal service sets `API_SERVER_ENABLED=true`, `API_SERVER_HOST=0.0.0.0`, `API_SERVER_MODEL_NAME=hermes-agent`, `HERMES_DASHBOARD=1`, `MESSAGING_CWD=/workspace`, and explicit dashboard origin. It mounts no credentials except the API-key file. The setup profile uses the same data directories, does not publish ports, and runs the official interactive setup command.

Start with `cap_drop: [ALL]`. If the pinned image's s6 initialization fails against pre-owned UID/GID directories, use the runtime harness to identify and restore only the exact capabilities required for ownership verification and final `s6-setuidgid` transition, recording each capability in `docs/security/threat-model.md`.

- [ ] **Step 5: Run structural tests and the opt-in runtime harness**

Run: `uv run --project control pytest -q deploy/compose/tests/test_hermes_agent.py deploy/compose/tests/test_networking.py`

Runtime command on a Docker host: `bash deploy/compose/tests/hermes-agent-runtime.sh`

The harness proves non-root tool execution, read-only root, API authentication, both health targets, persistence through recreation, absence of the Docker socket, and inability to attach private stack networks.

- [ ] **Step 6: Commit the container replacement**

```bash
git add -A deploy/compose
git commit -m "feat: replace SSH devbox with Hermes Agent"
```

---

### Task 4: Exact Hermes Tailscale Services

**Files:**
- Modify: `deploy/compose/tailscale/compose.yaml`
- Modify: `deploy/compose/tailscale/configure.sh`
- Modify: `deploy/compose/tailscale/grants.example.hujson`
- Modify: `deploy/compose/tests/test_tailscale.py`

**Interfaces:**
- Consumes: healthy `caddy:8080`, `hermes-agent:9119`, and `hermes-agent:8642` on exact shared edge networks.
- Produces: HTTPS Services `svc:dgx-forge`, `svc:hermes-dashboard`, and `svc:hermes-api`, with deterministic reset/recreation and `group:hermes-users` grants.

- [ ] **Step 1: Write failing exact-map and least-privilege tests**

Expected Service map:

```json
{"version":"0.0.1","services":{"svc:dgx-forge":{"endpoints":{"tcp:443":"http://caddy:8080"}},"svc:hermes-api":{"endpoints":{"tcp:443":"http://hermes-agent:8642"}},"svc:hermes-dashboard":{"endpoints":{"tcp:443":"http://hermes-agent:9119"}}}}
```

Tests reject any `svc:ai-devbox`, TCP 22, extra Service, HTTP listener on 443, retargeted upstream, wildcard service grant, or missing dependency health gate.

- [ ] **Step 2: Run the Tailscale tests and confirm they fail against SSH Services**

Run: `uv run --project control pytest -q deploy/compose/tests/test_tailscale.py`

- [ ] **Step 3: Replace the service map and grants**

Use:

```sh
ts serve --service=svc:hermes-dashboard --https=443 http://hermes-agent:9119
ts serve --service=svc:hermes-api --https=443 http://hermes-agent:8642
```

The grants file gives `group:hermes-users` access to only the two Hermes HTTPS Services. The API still requires `API_SERVER_KEY`. Auto-approval permits only `tag:dgx-gateway` to advertise the three named Services.

- [ ] **Step 4: Run the tests and commit**

Run: `uv run --project control pytest -q deploy/compose/tests/test_tailscale.py`

```bash
git add deploy/compose/tailscale deploy/compose/tests/test_tailscale.py
git commit -m "feat: expose Hermes through exact Tailscale Services"
```

---

### Task 5: Host egress boundary and encrypted recovery

**Files:**
- Create: `deploy/compose/bin/harden-hermes-egress`
- Create: `deploy/compose/tests/test_hermes_egress.py`
- Modify: `deploy/compose/bin/backup-control-plane`
- Modify: `deploy/compose/bin/restore-control-plane`
- Modify: `deploy/compose/tests/test_backup_restore.py`

**Interfaces:**
- Consumes: the resolved Docker bridge subnet for `${COMPOSE_PROJECT_NAME}_hermes-egress`, `10.0.0.0/24`, configured direct-fabric CIDRs, link-local metadata CIDRs, and `${HERMES_DATA_ROOT}`.
- Produces: an idempotent, explicit `--check`, `--apply`, and `--verify` host rule workflow plus encrypted backups containing Hermes `data` and `workspaces` but not `cache`.

- [ ] **Step 1: Write failing command-render and backup-content tests**

The hardening test supplies a fake Docker network inspection result and fake firewall binary, then asserts rules deny the Hermes bridge source to management, direct-fabric, `169.254.0.0/16`, and Docker private-control destinations while leaving DNS and ordinary Internet output available. It also proves the default invocation is non-mutating.

The backup test creates `data/.env`, `data/sessions/session.json`, and `workspaces/repository/README.md`, then asserts all are inside the encrypted manifest while `cache/sentinel` is absent.

- [ ] **Step 2: Run the host and recovery tests and confirm the workflows are absent**

Run: `uv run --project control pytest -q deploy/compose/tests/test_hermes_egress.py deploy/compose/tests/test_backup_restore.py`

- [ ] **Step 3: Implement fail-closed host hardening**

The script resolves exactly one named bridge through `docker network inspect`, validates its CIDR, renders an owned firewall chain, and changes host state only with `--apply`. It refuses missing/ambiguous networks, broad targets, unresolved environment values, or an unavailable firewall backend. `--verify` checks the effective rules and performs no broad flush.

- [ ] **Step 4: Include Hermes state in backup and restore**

Pass `${HERMES_DATA_ROOT}/data` and `${HERMES_DATA_ROOT}/workspaces` as explicit backup sources. During restore, keep Hermes stopped, extract to the verified staging directory, install files under the configured data root with owner-only modes, restore the configured UID/GID, and never restore cache or start Hermes before the normal route freshness checks run.

- [ ] **Step 5: Run tests and commit**

Run: `uv run --project control pytest -q deploy/compose/tests/test_hermes_egress.py deploy/compose/tests/test_backup_restore.py`

```bash
git add deploy/compose/bin deploy/compose/tests/test_hermes_egress.py deploy/compose/tests/test_backup_restore.py
git commit -m "feat: harden and recover Hermes state"
```

---

### Task 6: Supply chain and operator documentation

**Files:**
- Modify: `deploy/compose/images.lock.json`
- Modify: `scripts/verify-supply-chain`
- Modify: `tests/scripts/test_verify_supply_chain.py`
- Delete: `docs/runbooks/ai-devbox.md`
- Create: `docs/runbooks/hermes-agent.md`
- Modify: `README.md`
- Modify: `docs/runbooks/control-plane-bootstrap.md`
- Modify: `docs/runbooks/control-plane-recovery.md`
- Modify: `docs/runbooks/platform-operations.md`
- Modify: `docs/runbooks/tailscale.md`
- Modify: `docs/security/threat-model.md`
- Modify: `inventory/sbom/manifest.json`

**Interfaces:**
- Consumes: final Compose, image, policy, Tailscale, hardening, and recovery artifacts.
- Produces: an offline-verifiable image lock/manifest and complete setup, access, recovery, and identity guidance.

- [ ] **Step 1: Write failing supply-chain assertions**

Require the Hermes upstream digest in `images.lock.json`, require every Hermes Dockerfile/Compose/policy/entrypoint/hardening file in the generated manifest, and reject all remaining AI-devbox build-base entries and paths.

- [ ] **Step 2: Run the supply-chain tests and confirm stale devbox inputs fail**

Run: `uv run --python 3.12 --frozen --with pytest==9.1.1 pytest -q tests/scripts/test_verify_supply_chain.py`

- [ ] **Step 3: Update the lock and verifier**

Add:

```json
"hermes-agent": "nousresearch/hermes-agent:v2026.7.20@sha256:f7b35053268f532f98955195c909f15a230470fbcbdacaa9fdecb95707dad04a"
```

Remove the AI-devbox build bases. Verify the Hermes wrapper `ARG HERMES_IMAGE` is an exact locked digest and every production Compose image remains pinned.

- [ ] **Step 4: Replace operator documentation**

The Hermes runbook documents directory creation and ownership, 32-byte API-key generation, interactive setup profile, custom local endpoint/model, Tailscale origins, normal startup, health checks, backups, restore checks, and unavailable behavior. It explicitly separates GitHub-backed Tailscale identity, the Hermes API key, and an optional repository credential.

The threat model records the exact runtime capabilities proven necessary, the three Hermes networks, host egress rules, no Docker socket, local-only inference invariant, and the residual risk of terminal tools inside the Hermes container.

- [ ] **Step 5: Regenerate evidence, verify, and commit**

Run: `scripts/verify-supply-chain --generate --json`

Run: `scripts/verify-supply-chain --json`

```bash
git add -A README.md docs deploy/compose/images.lock.json scripts/verify-supply-chain tests/scripts/test_verify_supply_chain.py inventory/sbom/manifest.json
git commit -m "docs: document secure Hermes operations"
```

---

### Task 7: Integrated verification and acceptance handoff

**Files:**
- Modify only files required by failures that reproduce a violated requirement from Tasks 1-6.

**Interfaces:**
- Consumes: the complete feature branch.
- Produces: evidence that the branch satisfies static, unit, integration, supply-chain, and opt-in runtime contracts without claiming physical tailnet/NAS acceptance.

- [ ] **Step 1: Run focused control and Compose suites**

Run: `uv run --project control --python 3.12 --frozen --with pytest==9.1.1 pytest -q control/tests deploy/compose/tests`

- [ ] **Step 2: Run repository CI checks**

Run: `uvx --from ruff==0.16.1 ruff check .`

Run: `uv run --python 3.12 --frozen --with pytest==9.1.1 pytest -q`

- [ ] **Step 3: Verify rendered Compose and supply-chain evidence**

Run: `docker compose -f deploy/compose/compose.yaml -f deploy/compose/compose.step-ca.yaml --env-file deploy/compose/tests/test.env config --format json >/dev/null`

Run: `scripts/verify-supply-chain --json`

Run: `rg -n 'ai-devbox|authorized_keys|tcp:22|svc:ai-devbox' README.md deploy/compose docs/runbooks docs/security scripts inventory/sbom/manifest.json`

Expected: the search returns no production or operator reference.

- [ ] **Step 4: Run opt-in image/runtime acceptance when the Docker host can pull the pinned image**

Run: `bash deploy/compose/tests/hermes-agent-runtime.sh`

Record separately that physical tailnet identity authorization, NAS firewall enforcement, and live Spark inference remain deployment acceptance checks.

- [ ] **Step 5: Review branch diff and commit any evidence-only corrections**

Run: `git diff --check`

Run: `git status --short`

If verification required a correction, commit only that correction with a message naming the violated contract. Leave the branch clean and ready to merge.
