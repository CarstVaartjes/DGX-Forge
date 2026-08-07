# Task 2 report — Python namespace and command migration

Status: COMPLETE

## Delivered

- Renamed the root, control, agent, and protocol import packages to
  `cluster_profiles`, `vonk_control`, `vonk_agent`, and
  `vonk_agent_protocol`.
- Renamed the root test package directory and repository-local CLI launchers
  to match `cluster_profiles` and `vonkctl`.
- Updated Python imports, import-resource lookups, monkeypatch targets,
  package source paths, generated-client paths, build inputs, scripts, tests,
  and CI/package-integration references.
- Renamed the distributions to `vonk-cluster-profiles`,
  `vonk-forge-agent`, and `vonk-agent-protocol`, while intentionally leaving
  the control distribution and native/runtime entry-point identities for
  their later plan tasks.
- Added explicit Hatch wheel package declarations where the new distribution
  name no longer normalizes to the import-package name.
- Rebuilt the tracked protocol wheel as
  `inventory/wheels/vonk_agent_protocol-2.1.0-py3-none-any.whl` and
  regenerated all four uv lockfiles.
- Preserved Compose, native agent, systemd, installer, transport-contract,
  runtime filesystem, and broad documentation identities for Tasks 3–5.

## Verification

```text
$ uv lock
$ uv lock --project control
$ uv lock --project agent
$ uv lock --project agent_protocol
all exited 0

$ uv run --isolated --frozen pytest -q tests/cluster_profiles
691 passed in 58.40s

$ uv run --isolated --project control --frozen --with-editable . pytest -q control/tests/test_settings.py control/tests/test_api.py
40 passed in 2.19s

$ uv run --isolated --project agent --frozen --with-editable . pytest -q agent/tests/test_config.py agent/tests/test_client.py
115 passed in 14.25s

$ uv run --isolated --project agent_protocol --frozen --with-editable . pytest -q agent_protocol/tests
427 passed in 0.51s

$ uv build --wheel --out-dir <temporary-root>
$ uv build --project control --wheel --out-dir <temporary-control>
$ uv build --project agent --wheel --out-dir <temporary-agent>
$ uv build --project agent_protocol --wheel --out-dir <temporary-protocol>
all exited 0; wheel metadata and embedded import-package paths were checked

$ PYTHONPATH=src:control/src:agent/src:agent_protocol/src python3 -c <four-package import check>
cluster_profiles vonk_control vonk_agent vonk_agent_protocol

$ python3 -m compileall -q src control/src agent/src agent_protocol/src scripts bin
exit 0

$ git diff --check
exit 0

$ scripts/verify-vonk-identity --json .
exit 1, status=failed, owned_matches=7257, external_matches=10
```

The identity-guard failure is expected at this staged point. Remaining owned
matches are concentrated in documentation, Compose/deployment contracts,
native and installer surfaces, transport/runtime identifiers, release
metadata, and historical task records assigned to later plan tasks.

## Broad-suite diagnostic

The brief's literal `uv run pytest tests -q` could not spawn `pytest` because
the checkout's pre-existing `.venv` launcher points at another workspace.
Running the suite with `uv run --isolated pytest tests -q` completed with
`1203 passed, 1 skipped, 43 failed`. The failures are staged-plan or existing
environment boundaries outside Task 2: unchanged docs now lag renamed command
assertions, unchanged native/workflow assertions await later tasks,
supply-chain generated metadata awaits its designated regeneration task, and
the OpenAPI generator cannot find its subprocess tool in the pre-existing
control environment. The package-specific suites above pass in isolated,
locked environments.
