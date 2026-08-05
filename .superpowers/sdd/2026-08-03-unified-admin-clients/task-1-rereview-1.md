### Finding Verdicts

- Certificate-bearing approval response — ADDRESSED. The strict decision model exposes only `id`, `node_id`, and `state`, and approval returns exactly those fields; certificate serialization remains on the agent enrollment channel (`control/src/dgx_control/agent_api.py:152`, `control/src/dgx_control/agent_api.py:686`, `control/src/dgx_control/agent_api.py:713`, `control/src/dgx_control/agent_api.py:825`).
- Missing enrollment mutation audits — ADDRESSED. Grant, approve, reject, and revoke append request-correlated audit records only after successful mutation (`control/src/dgx_control/agent_api.py:625`, `control/src/dgx_control/agent_api.py:629`, `control/src/dgx_control/agent_api.py:698`, `control/src/dgx_control/agent_api.py:704`, `control/src/dgx_control/agent_api.py:732`, `control/src/dgx_control/agent_api.py:736`, `control/src/dgx_control/agent_api.py:763`, `control/src/dgx_control/agent_api.py:771`). Failure audit silence is explicitly exercised at `control/tests/test_agent_api.py:953`.
- Incomplete endpoint validation — ADDRESSED. The authoritative verifier validates canonical marker bytes, all generation files and digests, route/LiteLLM documents, and lease bounds (`control/src/dgx_control/route_runtime.py:989`, `control/src/dgx_control/route_runtime.py:1045`, `control/src/dgx_control/route_runtime.py:1077`, `control/src/dgx_control/route_runtime.py:1123`). Endpoint projection then cross-checks the verified marker against durable publication and owner receipts (`control/src/dgx_control/operation_api.py:417`, `control/src/dgx_control/operation_api.py:422`).
- Untyped bounded errors — NOT ADDRESSED. The principal apply/endpoint/resume cases are fixed, but coverage is not complete for every applicable operation. Planning raises bounded 409 (`control/src/dgx_control/api.py:490`) while both plan decorators omit 409 (`control/src/dgx_control/api.py:508`, `control/src/dgx_control/api.py:518`). Job-log content returns 403/404/503 (`control/src/dgx_control/api.py:727`) while OpenAPI declares only 200/422 (`control/openapi.json:2619`) and its generated parser returns `None` for those statuses (`src/spark_profiles/generated_control/api/default/get_job_log.py:36`). The GREEN schema test omits these operations from its expectation table (`control/tests/test_operation_api.py:664`).
- Unrestricted plan DAG and progress — ADDRESSED. Plan shapes and DAG nodes are bounded typed models (`control/src/dgx_control/operation_api.py:88`, `control/src/dgx_control/operation_api.py:171`, `control/src/dgx_control/operation_api.py:181`); job progress is restricted to bounded `phase` (`control/src/dgx_control/operation_api.py:234`, `control/src/dgx_control/operation_api.py:347`); plan projection explicitly selects public fields (`control/src/dgx_control/operation_api.py:717`).
- `probe_age_seconds=None` crash — ADDRESSED. Fleet refresh preserves `None` (`control/src/dgx_control/api.py:173`), the registry accepts it (`control/src/dgx_control/metrics.py:85`), and rendering omits the age series (`control/src/dgx_control/metrics.py:199`).

### New Breakage in the Fix Diff

- None.

### Out-of-Scope Observations

- None.

### Verdict

**Fix round:** Findings remain open — finding 4, typed bounded errors for every applicable operation.
