# Task 4 report — outbound enrollment and long-poll client

## Outcome

Implemented the outbound Spark agent transport and crash-recovering lifecycle,
including the resolved Task 3/4 integration requirements for enrollment replay,
bounded long polling, result reconciliation, shared HTTPS/ORAS credentials, and
two-phase certificate rotation. The agent opens no listener and uses fixed
direct HTTPS POST routes with no ambient proxy, redirect, netrc, or caller
header surface.

## RED/GREEN evidence

The initial required RED command was:

```text
uv run --project agent pytest agent/tests/test_client.py agent/tests/test_lifecycle.py -v
```

Collection failed because `dgx_agent.client` and `dgx_agent.main` did not
exist. Subsequent focused RED slices demonstrated missing client methods,
credential storage, lifecycle rotation, enrollment replay, two-phase control
rotation, long-poll behavior, ORAS provider refresh, and the console entry
point before each implementation slice.

Additional review-driven RED cases reproduced:

- ambiguous numeric origins (`https://127.1` and `https://2130706433`);
- SQLite simultaneous renewal issuing colliding staged generations;
- partial dynamic ORAS credential snapshots leaking a descriptor;
- restart leaving an incomplete credential-generation directory;
- non-canonical FastAPI validation responses on the agent API;
- historical multi-certificate migration failure at the new unique generation
  constraint; and
- non-JSON intermediary error bodies hiding explicit 401/422/503 semantics.

Each case passed after the corresponding bounded fix. The final required
focused command collected 39 tests and passed all 39 in 11.57 seconds.

## Implementation

### Outbound client and lifecycle

- `AgentClient` uses one `http.client.HTTPSConnection` per request and closes
  the response, socket, TLS credential snapshots, and all FDs deterministically.
- Origins are canonical HTTPS origins; ambiguous numeric aliases, userinfo,
  paths, queries, fragments, non-canonical DNS/IPv6, and port zero are denied.
- Runtime requests use the configured mTLS origin. Enrollment requires a
  separate explicit server-authenticated origin and deliberately omits the
  client identity.
- Fixed `/agent/v1/...` POST paths use canonical request JSON, bounded bodies,
  independent connect/read/long-poll timeouts, hostname verification, the
  private CA, and the active client identity.
- Successful JSON responses require the exact JSON content type, UTF-8,
  duplicate-free canonical bytes, valid protocol contracts, and no trailing
  data. Request and response bodies are capped at 64 KiB.
- 408, 429, and 5xx are retryable transport outcomes; 401/403 are typed
  authentication failures; other 4xx are typed permanent failures. A result
  204 or stale-fence 409 acknowledges only the submitted durable result.
- `Agent.run_once()` submits a pending terminal result first, then resumes the
  exact active claim through `OperationRegistry`, performs credential recovery,
  and only then asks for new work. `run_forever()` uses interruptible bounded
  jittered exponential backoff and resets it after success.
- `dgx-forge-agent` is installed as a console entry point with SIGINT/SIGTERM
  shutdown through an interruptible event.

### Credentials and rotation

- `CredentialStore` keeps the configured certificate/key as the seed identity,
  generates Ed25519 keys and CSRs locally, and persists the same CSR across a
  lost renewal response.
- Service-owned generations use descriptor-relative, no-follow, bounded writes,
  exact `0700`/`0600` modes, file and parent fsync, atomic generation/pointer
  publication, and restart cleanup of incomplete staging directories.
- Renewal begins at one-third remaining lifetime. A new generation stays
  staged locally until activation succeeds. Restart resumes renewal or staged
  activation, including a lost activation response.
- HTTPS and ORAS consume sealed memfd snapshots through one stable credential
  provider. ORAS resolves that provider for every pull, so rotation cannot
  leave release transport on an obsolete client identity.

### Control-plane integration

- Exact enrollment replay returns pending 202, approved certificate 200, or a
  terminal rejection. CSR/evidence mismatch never exposes certificate material;
  malformed first use retains one-time-token consumption hardening.
- Certificate rows now carry state, deterministic per-node generation, staged
  public material, and the CSR fingerprint. The reversible migration assigns
  unique historical generations before adding the unique constraint, including
  databases with multiple old certificate rows.
- Renewal authenticates only an active identity, creates one idempotent staged
  generation, and keeps the old certificate active. Activation authenticates
  the staged certificate, atomically activates it, and revokes older local
  identities. Retrying with the newly active certificate is idempotent.
- Trusted-proxy validation admits staged identities only on
  `/agent/v1/renew/activate`; staged claim, heartbeat, result, renewal, and
  artifact requests are rejected.
- `/claim` performs a bounded condition-based long poll, wakes after committed
  enqueue, and returns 204 on timeout without per-client durable state.
- Failed terminal results map the stable bounded `error_code` to the stored
  failure reason; exception text and secret-bearing context are not serialized.
- Agent success and validation/error JSON is canonical. Caddy evidence proves
  activation is routed only on the agent SNI protected by verified client TLS,
  not the browser/control or enrollment SNI.

## Final verification

Executed on the final candidate:

```text
uv run --project agent pytest agent/tests/test_client.py agent/tests/test_lifecycle.py -v
39 passed in 11.57s

uv run --project agent pytest agent/tests -q
452 passed in 25.53s

uv run --project agent_protocol pytest agent_protocol/tests -q
321 passed in 0.30s

uv run --project control pytest control/tests -q
275 passed in 21.44s

uv run --project control pytest control/tests/test_agent_migrations.py -q
6 passed in 1.03s

uv run pytest deploy/compose/tests -q
22 passed in 7.85s

docker compose --env-file deploy/compose/tests/test.env \
  -f deploy/compose/compose.yaml \
  -f deploy/compose/compose.step-ca.yaml config --quiet
exit 0

docker compose --env-file deploy/compose/tests/test.env \
  -f deploy/compose/compose.yaml \
  -f deploy/compose/compose.builtin-ca.yaml config --quiet
exit 0

uvx --from ruff==0.16.1 ruff check \
  agent/src/dgx_agent/client.py agent/src/dgx_agent/main.py \
  agent/src/dgx_agent/oci.py agent/tests/test_client.py \
  agent/tests/test_lifecycle.py agent/tests/test_releases.py \
  control/src/dgx_control/agent_api.py \
  control/src/dgx_control/agent_jobs.py control/src/dgx_control/api.py \
  control/src/dgx_control/auth.py control/src/dgx_control/enrollment.py \
  control/src/dgx_control/models.py control/src/dgx_control/pki.py \
  control/tests/test_agent_api.py control/tests/test_agent_jobs.py \
  control/tests/test_agent_migrations.py control/tests/test_enrollment.py \
  control/migrations/versions/0005_certificate_rotation.py \
  deploy/compose/tests/test_agent_ingress.py
All checks passed!

uv run --project agent python -m compileall -q agent/src
exit 0

uv build --project agent
Successfully built agent/dist/dgx_agent-0.1.0.tar.gz
Successfully built agent/dist/dgx_agent-0.1.0-py3-none-any.whl

fresh Python 3.12 environment; install built protocol and agent wheels;
import AgentClient/CredentialStore/Agent/ORASClient/AgentClaim; run entry --help
fresh-wheel-imports-ok; exit 0

scripts/verify-supply-chain --json
{"errors":[],"images":6,"manifest_sha256":"6d3f8a95cc355d2156dcf7429bda4453637676538cf56847657e1c3eb3f8edea","ok":true,"sboms":["inventory/sbom/agent-protocol.spdx.json","inventory/sbom/agent-python.spdx.json","inventory/sbom/control-python.spdx.json","inventory/sbom/control-web.spdx.json"]}

git diff --check
exit 0
```

The first supply-chain check correctly reported the agent Python SBOM and
manifest stale after `cryptography==50.0.0` became a direct runtime dependency.
`scripts/verify-supply-chain --generate --json` regenerated only the derived
agent SBOM and aggregate manifest; the final verifier output above is clean.

## Boundary for Task 5

`Agent` accepts the complete Task 1–3 `OperationContext`. The current console
builder wires the node-probe boundary available from the Task 1 configuration.
Task 5 remains responsible for installing the fixed ORAS/TUF/release/workload
paths and policy material before those production handlers can be constructed
by the service entry point. No network claim can supply those paths or policy.
