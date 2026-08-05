### Spec Compliance

- ❌ Issues found: the generated admin surface includes a certificate-bearing approval response (`control/src/dgx_control/agent_api.py:178`, `control/openapi.json:1078`), violating the certificate-material boundary.
- ⚠️ Cannot verify from this task diff: later CLI/web equivalence, absence of direct backend construction in routine commands, and preservation of separately named bootstrap/recovery interfaces.

### Strengths

- Canonical reconciliation, fleet, and job routes are retained; generic enqueue is excluded from OpenAPI (`control/src/dgx_control/api.py:478`, `control/src/dgx_control/api.py:587`).
- Missing observations correctly become `healthy=null`, `stale=true`, and `probe_age_seconds=null` (`control/src/dgx_control/dashboard.py:111`).
- Agent output is capped at 500 nodes and omits addresses and stored certificate bodies (`control/src/dgx_control/operation_api.py:336`).
- Generators are version-pinned, write to the required downstream paths, and have twice-idempotent drift checks (`scripts/generate-control-clients:24`, `tests/control/test_openapi_clients.py:41`).

### Issues

#### Critical

- `control/src/dgx_control/agent_api.py:178`, `control/src/dgx_control/agent_api.py:608`, `control/openapi.json:1078`, `control/web/src/api/generated.d.ts:793`: the admin schema includes `approveAgentEnrollment`, whose live response contains `certificate_pem` and `chain_pem`; declaring it as `[key: string]: unknown` merely hides those names from the schema test while generated browser clients still receive the material. Impact: the explicit browser certificate boundary is bypassed. Fix: make admin approval return a strict secret-free decision/one-time component-state response and deliver certificates only through the agent activation channel; never represent certificate-bearing responses as opaque objects.

#### Important

- `control/src/dgx_control/agent_api.py:566`, `control/src/dgx_control/agent_api.py:608`, `control/src/dgx_control/agent_api.py:617`, `control/src/dgx_control/agent_api.py:627`: grant creation, approval, rejection, and revocation perform no audit append, while only reconciliation apply and resume are audited in `control/src/dgx_control/api.py:523` and `control/src/dgx_control/api.py:662`. Impact: required enrollment lifecycle actions lack actor/request correlation. Fix: pass the audit sink and request into the human agent routes and append stable audit records after each successful mutation.
- `control/src/dgx_control/operation_api.py:273`, `control/src/dgx_control/operation_api.py:285`, `control/src/dgx_control/operation_api.py:328`: endpoint resolution checks only a subset of activation-marker invariants and directly indexes an unvalidated `expires_at`. It omits canonical marker shape, marker lease validation, the generation-to-manifest digest tie, and manifest/LiteLLM verification implemented by the authoritative reader at `control/src/dgx_control/route_runtime.py:949` and `control/src/dgx_control/route_runtime.py:993`. Impact: corrupt publication state can return a stale endpoint or raise an unbounded 500 instead of fail-closed 503. Fix: reuse a public authoritative route-bundle verifier, then cross-check its validated marker against the durable owner/publication records.
- `control/src/dgx_control/api.py:513`, `control/openapi.json:1792`, `src/spark_profiles/generated_control/api/default/apply_reconciliation.py:44`: apply returns required 409/503 failures, but OpenAPI documents only 202/422 and the generated client returns `None` for other statuses by default. Endpoint and resume have the same omission. Impact: generated consumers cannot distinguish stale digest, missing resource, or unavailable control plane through typed contracts. Fix: add a strict bounded error model and declare 401/403/404/409/503 responses on every applicable operation before regeneration.
- `control/src/dgx_control/operation_api.py:77`, `control/src/dgx_control/operation_api.py:123`, `control/src/dgx_control/operation_api.py:430`: plan DAG fields and job-operation progress remain `dict[str, Any]`, with persisted progress copied verbatim. The generated schema confirms unrestricted progress at `control/openapi.json:362`. Impact: the supposedly strict client contract cannot validate stable DAG/progress shapes or enforce the private-evidence boundary. Fix: introduce explicit bounded nested plan and progress models and project only whitelisted fields.
- `control/src/dgx_control/dashboard.py:111`, `control/src/dgx_control/api.py:815`: the required missing-observation value `probe_age_seconds=None` is passed to `float()` during metrics refresh. Impact: `/metrics` fails whenever a fleet node has no health observation. Fix: handle `None` explicitly by omitting/clearing that metric or using a defined unknown-state representation.

#### Minor

- None.

### Assessment

**Task quality:** Needs fixes

The implementation preserves the canonical resources and establishes reproducible generation, but the certificate-material exposure and missing audit/fail-closed/typed-error guarantees are blocking contract and security defects.
