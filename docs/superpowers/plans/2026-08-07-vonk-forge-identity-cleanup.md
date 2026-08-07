# Vonk Forge identity cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox ( - [ ] ) syntax for tracking.

**Goal:** Remove Spark/DGX terminology from Vonk-owned identifiers and migrate the unreleased repository to one consistent Vonk Forge identity.

**Architecture:** Rename package/module, command, service, filesystem, Compose, API-contract, and release namespaces in one pre-release change. Preserve only explicitly external upstream/vendor evidence at integration boundaries. No compatibility aliases or migrations are added.

**Tech Stack:** Python 3.12/uv, Rust/Cargo, TypeScript/npm, Docker Compose, JSON Schema, OpenAPI, systemd, GitHub Actions.

## Global Constraints

- Vonk-owned names use Vonk Forge, vonk-forge, VONK_*, node, GPU host, cluster, and workload.
- Vonk-owned code, configuration, paths, commands, API schemas, fixtures, and docs contain no spark or dgx tokens.
- Opaque upstream URLs, model names, vendor API values, and raw hardware evidence may retain those tokens only in explicitly external evidence roots.
- No compatibility aliases, dual settings, legacy services, or migration readers are introduced.
- svc:vonk-forge is the canonical Tailscale service identifier.
- Hermes is outside this cleanup plan; the later NAS plan will make Hermes an opt-in Compose profile disabled by default.

---

### Task 1: Add the identity regression guard

**Files:**
- Create: scripts/vonk_identity.py
- Create: scripts/verify-vonk-identity
- Create: tests/scripts/test_verify_vonk_identity.py

**Interfaces:**
- scripts/vonk_identity.py exports verify(root: Path) -> dict[str, object] with status, owned_matches, and external_matches.
- scripts/verify-vonk-identity is the executable CLI wrapper around verify.
- The verifier skips .git, caches, binaries, and explicit external evidence roots manifests/, inventory/raw/, and tests/fixtures/external/.

- [ ] Step 1: Write a failing test.

    def test_identity_verifier_rejects_owned_spark_token(tmp_path):
        (tmp_path / "README.md").write_text("vonk sparkctl\n", encoding="utf-8")
        result = verify(tmp_path)
        assert result["status"] == "failed"
        assert "sparkctl" in result["owned_matches"][0]["text"]

- [ ] Step 2: Run the focused test.

    Run: uv run pytest tests/scripts/test_verify_vonk_identity.py -q
    Expected: FAIL because the verifier does not exist.

- [ ] Step 3: Implement the verifier with stable sorted JSON and exit status 1 when owned_matches is non-empty.

- [ ] Step 4: Run the test and scripts/verify-vonk-identity --json. Record the initial legacy inventory.

- [ ] Step 5: Commit.

    git add scripts/vonk_identity.py scripts/verify-vonk-identity tests/scripts/test_verify_vonk_identity.py
    git commit -m "test: add Vonk identity regression guard"

### Task 2: Rename Python namespaces and commands

**Files:**
- Rename: src/spark_profiles/ to src/cluster_profiles/
- Rename: control/src/dgx_control/ to control/src/vonk_control/
- Rename: agent/src/dgx_agent/ to agent/src/vonk_agent/
- Rename: agent_protocol/src/dgx_agent_protocol/ to agent_protocol/src/vonk_agent_protocol/
- Modify: pyproject.toml, control/pyproject.toml, agent/pyproject.toml, agent_protocol/pyproject.toml, all Python imports, scripts, tests, and lockfiles.

**Interfaces:**
- The root project is vonk-cluster-profiles and exposes vonkctl = cluster_profiles.cli:main.
- The control project imports vonk_control.
- The agent project is vonk-forge-agent and imports vonk_agent.
- The protocol project is vonk-agent-protocol and imports vonk_agent_protocol.

- [ ] Step 1: Rename package trees.

    git mv src/spark_profiles src/cluster_profiles
    git mv control/src/dgx_control control/src/vonk_control
    git mv agent/src/dgx_agent agent/src/vonk_agent
    git mv agent_protocol/src/dgx_agent_protocol agent_protocol/src/vonk_agent_protocol

- [ ] Step 2: Change spark-profiles to vonk-cluster-profiles, sparkctl to vonkctl, dgx-agent to vonk-forge-agent, and dgx-agent-protocol to vonk-agent-protocol in project metadata and entry points.

- [ ] Step 3: Replace imports, force-include paths, uv.sources, generated output paths, and test references.

- [ ] Step 4: Regenerate locks and run focused tests.

    uv lock
    uv lock --project control
    uv lock --project agent
    uv lock --project agent_protocol
    uv run pytest tests -q
    uv run --project control --frozen --with-editable . pytest -q control/tests/test_settings.py control/tests/test_api.py
    uv run --project agent --frozen --with-editable . pytest -q agent/tests/test_config.py agent/tests/test_client.py
    uv run --project agent_protocol --frozen --with-editable . pytest -q agent_protocol/tests

- [ ] Step 5: Commit.

    git add pyproject.toml control agent agent_protocol src scripts
    git commit -m "refactor: rename Python namespaces to Vonk Forge"

### Task 3: Rename native agent, systemd, installer, and filesystem identities

**Files:**
- Rename: agent/systemd/dgx-forge-* to agent/systemd/vonk-forge-*.
- Rename: agent/supervisor/dgx-agent-supervisor to agent/supervisor/vonk-agent-supervisor.
- Rename: nodes/bin/install-dgx-agent to nodes/bin/install-vonk-agent.
- Modify: Rust crates, agent/tools/build-slot-artifact, packaging scripts, supervisor tests, and release metadata.

**Interfaces:**
- Units are vonk-forge-agent.service, vonk-forge-agent-supervisor.service, vonk-forge-agent-activation.service, vonk-forge-agent-rollback.service, and vonk-forge-package-helper.service/socket.
- Runtime roots are /etc/vonk-forge-agent, /var/lib/vonk-forge-agent, /var/lib/vonk-forge, /opt/vonk-forge, and /run/vonk-forge-agent.
- The installer command is install-vonk-agent.

- [ ] Step 1: Rename the tracked unit, supervisor, and installer files with git mv.

- [ ] Step 2: Replace unit names, paths, descriptions, service dependencies, Rust constants, installer destinations, and package metadata.

- [ ] Step 3: Run native and installer verification.

    cargo fmt --all -- --check
    cargo test --workspace
    uv run scripts/verify-agent-systemd
    uv run pytest -q agent/tests/test_supervisor.py agent/tests/test_update.py agent/tests/test_lifecycle.py

- [ ] Step 4: Commit.

    git add agent nodes rust scripts
    git commit -m "refactor: rename agent runtime identities"

### Task 4: Rename contracts, Compose, release, and documentation namespaces

**Files:**
- Modify: schemas/, control/openapi*.json, control/openapi-python-client.yaml, control/src/vonk_control/, agent_protocol/src/vonk_agent_protocol/, manifests/, fixtures, deploy/compose/, .github/, README.md, docs/, and release scripts.

**Interfaces:**
- Evidence keys use vonk_forge.
- Media types use application/vnd.vonk-forge.*.
- Schema and SPIFFE namespaces use vonk-forge.
- Compose uses VONK_*, vonk-forge-control, svc:vonk-forge, /srv/vonk-forge, and ghcr.io/carstvaartjes/vonk-forge-*.
- User-facing SparkRun/spark profiles become WorkloadRun/workload profiles.

- [ ] Step 1: Replace owned schema IDs, OpenAPI titles, evidence keys, media types, SPIFFE URIs, Compose variables, image references, Tailscale names, Caddy hostnames, registry paths, and docs. Preserve only external evidence strings in the allowlisted roots.

- [ ] Step 2: Regenerate clients and deterministic artifacts.

    scripts/generate-control-clients
    npm ci --prefix control/web
    npm test --prefix control/web -- --run

- [ ] Step 3: Render Compose with the checked-in test environment.

    docker compose --env-file deploy/compose/tests/test.env -f deploy/compose/compose.yaml config --quiet

    Expected: no DGX_ or legacy path/image/hostname requirement.

- [ ] Step 4: Commit.

    git add schemas control agent_protocol manifests deploy .github README.md docs scripts
    git commit -m "refactor: standardize Vonk Forge contracts and deployment names"

### Task 5: Update tests, package verification, and release metadata

**Files:**
- Modify: tests/, control/tests/, agent/tests/, agent_protocol/tests/, rust/**/tests/, deploy/compose/tests/, scripts/build-agent-deb, scripts/verify-agent-deb, scripts/verify-supply-chain, and workflow metadata.

- [ ] Step 1: Update expected commands, paths, package names, units, media types, URI namespaces, Compose names, and evidence keys. Keep raw vendor evidence assertions unchanged and labeled external.

- [ ] Step 2: Run the full local verification tiers.

    uv run --frozen pytest -q
    uv run --project control --frozen --with-editable . pytest -q control/tests
    npm ci --prefix control/web
    npm test --prefix control/web -- --run
    uv run --frozen pytest -q deploy/compose/tests
    scripts/verify-agent-systemd
    scripts/verify-supply-chain --json

- [ ] Step 3: Run scripts/verify-vonk-identity --json. Expected: status passed and any external matches confined to documented external evidence roots.

- [ ] Step 4: Build and verify the renamed agent package using scripts/build-agent-deb and scripts/verify-agent-deb --json.

- [ ] Step 5: Commit.

    git add tests control/tests agent/tests agent_protocol/tests rust scripts .github
    git commit -m "test: complete Vonk Forge identity migration"

### Task 6: Final handoff to NAS design

**Files:**
- Modify: none unless verification identifies a remaining Vonk-owned legacy token.

- [ ] Step 1: Run git status --short and git diff --check. Expected: no output.

- [ ] Step 2: Run rg -n -i 'spark|dgx' README.md docs deploy scripts src control agent agent_protocol rust schemas .github. Expected: no Vonk-owned matches; only explicitly allowed external evidence.

- [ ] Step 3: Record the verification commands and allowed external matches, then begin the separate source-first NAS design. Do not publish an agent package or start NAS implementation until the identity cleanup commit is the repository base.
