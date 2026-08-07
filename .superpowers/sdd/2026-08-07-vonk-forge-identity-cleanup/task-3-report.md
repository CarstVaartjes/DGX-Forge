# Task 3 report — native agent identity migration

Status: COMPLETE

## Delivered

- Renamed the tracked Python supervisor, eight systemd units, node-local
  installer, installer test, Debian systemd units, and native installation
  guide to their Vonk Forge identities.
- Standardized native service names on `vonk-forge-agent*` and
  `vonk-forge-package-helper*`, including Rust restart dispatch, Debian
  lifecycle scripts, release workflow checks, package build/verification, and
  installed-root systemd verification.
- Migrated owned native environment variables to `VONK_*`, the agent account
  to `vonk-agent`, the workload account to `vonk-workload`, and runtime paths
  to the canonical `/etc/vonk-forge-agent`, `/var/lib/vonk-forge-agent`,
  `/var/lib/vonk-forge`, `/opt/vonk-forge`, and `/run/vonk-forge-agent`
  families. Supervisor and package-helper private state retain dedicated Vonk
  sub-identities.
- Updated the Python slot builder, runtime policy, package-helper boundaries,
  Rust config/process constants, package metadata, release payload names,
  CI paths, tests, and native installation documentation.
- Preserved NVIDIA-owned OS/tool evidence and deferred schema, SPIFFE,
  media-type, deployment, and broad documentation namespaces to Task 4.

## Verification

```text
$ cargo fmt --all -- --check
exit 0

$ cargo test --workspace
all workspace unit, integration, and doc-test targets passed

$ uv run --isolated scripts/verify-agent-systemd
installed-root verification passed; all five service security checks passed

$ uv run --isolated --project agent --frozen --with-editable . pytest -q \
    agent/tests/test_supervisor.py agent/tests/test_update.py \
    agent/tests/test_lifecycle.py tests/nodes/test_install_vonk_agent.py \
    tests/scripts/test_agent_deb.py
148 passed in 30.09s

$ uv run --isolated --project agent --frozen --with-editable . pytest -q \
    agent/tests/test_runtime_policy.py agent/tests/test_package_helper.py \
    agent/tests/test_slot_artifact.py
48 passed in 32.04s

$ python3 -m compileall -q agent/src agent/supervisor nodes/bin agent/tools scripts
$ sh -n packaging/debian/postinst packaging/debian/preinst packaging/debian/prerm
$ git diff --check
all exited 0
```

## Review round 1 remediation

- Removed `RuntimeDirectory=vonk-forge-agent` from the root package-helper
  service. The unprivileged agent remains the sole owner of that runtime
  directory; the existing socket unit continues to own the package-helper
  socket lifecycle and the helper retains its separate root-owned state
  directory.
- Renamed the package-helper CLI usage identity to
  `vonk-forge-package-helper`.
- Added focused regressions for exclusive runtime-directory ownership and the
  observable `--help` program name.

### Follow-up verification

```text
$ uv run --isolated --project agent --frozen --with-editable . pytest -q \
    agent/tests/test_package_helper.py
21 passed in 0.15s

$ uv run --isolated --project agent --frozen --with-editable . pytest -q \
    agent/tests/test_supervisor.py -k \
    'systemd_units_verify_and_enforce_split_privilege_hardening or installed_systemd_harness_verifies_units_by_installed_name'
2 passed, 66 deselected in 0.30s

$ uv run --isolated scripts/verify-agent-systemd
installed-root verification passed; all five service security checks passed

$ python3 -m compileall -q agent/src
$ git diff --check
all exited 0
```

## Review round 2 remediation

- Moved the privileged package-helper endpoint to
  `/run/vonk-forge-package-helper/package-helper.sock` in both native socket
  units and the agent client. Agent activation, rollback, container, and
  readiness files remain under `/run/vonk-forge-agent`.
- Made the helper runtime explicitly root-managed with
  `RuntimeDirectory=vonk-forge-package-helper`. The parent is preserved while
  its socket unit remains active and uses mode `0711`: unprivileged processes
  can traverse to the `0660 root:vonk-agent` socket but cannot list or mutate
  the root-owned directory.
- Updated the node installer to establish the same root-owned runtime boundary
  before enabling the socket. Extended source-unit, client, installer, Debian
  artifact, and installed-root verifier regressions to enforce it.
- Preserved the round-1 `vonk-forge-package-helper` CLI usage-name fix.

### Round 2 verification

```text
$ uv run --isolated --project agent --frozen --with-editable . pytest -q \
    agent/tests/test_package_helper.py agent/tests/test_package_helper_client.py
39 passed in 0.23s

$ uv run --isolated --project agent --frozen --with-editable . pytest -q \
    agent/tests/test_supervisor.py -k \
    'systemd_units_verify_and_enforce_split_privilege_hardening or installed_systemd_harness_verifies_units_by_installed_name'
2 passed, 66 deselected in 0.42s

$ uv run --isolated --project agent --frozen --with-editable . pytest -q \
    tests/nodes/test_install_vonk_agent.py
32 passed in 10.53s

$ uv run --isolated --frozen pytest -q tests/scripts/test_agent_deb.py
3 passed in 0.66s

$ uv run --isolated scripts/verify-agent-systemd
installed-root verification passed; all five service security checks passed

$ python3 -m compileall -q agent/src agent/supervisor nodes/bin agent/tools scripts
$ sh -n packaging/debian/postinst packaging/debian/preinst packaging/debian/prerm
$ git diff --check
all exited 0
```
