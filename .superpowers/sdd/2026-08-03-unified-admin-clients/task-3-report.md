# Task 3 report: routine `sparkctl` control-API cutover

## Status and scope

Implemented global Task 23 / plan Task 3 on `feature/unified-admin-clients` from
base `8d81174a43d01376b82c0cf9760d83e654ba423f`.

Changed:

- `src/spark_profiles/cli.py`
- `src/spark_profiles/legacy_cli.py`
- `bin/sparkctl-legacy`
- `tests/spark_profiles/test_agent_cli.py`
- `tests/spark_profiles/test_cli.py`
- `README.md`
- `docs/runbooks/sparkctl.md`
- this report

No generated client file, local controller behavior, SSH transport, API schema,
inventory identity, hostname, or address was changed.

## Implementation

- The ordinary `sparkctl` module imports the control client boundary and no
  backend, health collector, state store, catalog, switcher, or SSH component.
- `nodes status`, `validate`, `prepare`, `switch`, `restore-default`, and
  `endpoint` call typed `ControlClient` operations and emit generated model
  data. Successful structured resources bypass the inherited sanitizer and
  preserve every returned collection element and string exactly; bounded
  redaction/truncation remains only on admin and error-safety paths.
- `validate` is always plan-only. `prepare`, `switch`, and `restore-default`
  return the exact server plan unless `--apply` is present. Apply submits only
  the digest from that freshly returned plan with a canonical UUID request ID.
- Applied mutations wait for the accepted job by default and when `--wait` is
  explicit. `--no-wait` returns the typed accepted-job response without a job
  GET. Waiting polls only the accepted job ID through `ControlClient.wait_job`.
- API transport/503 failures emit the stable
  `{"error":"control API unavailable","error_type":"control_api"}` shape.
  Other control failures remain bounded and redacted. No error selects local,
  SSH, or legacy execution, and an apply is never replayed by the CLI.
- Existing API-backed `admin` proposal/deploy/read dispatch was retained.
- The prior `cli.py` was moved intact to `legacy_cli.py`; comparison against the
  base file, after substituting only the module docstring, returns byte equality.
  It is reachable only through executable `bin/sparkctl-legacy`. The standard
  parser rejects `sparkctl legacy ...` and has no implicit compatibility branch.
- Production README/runbook examples now require control API configuration,
  explain plan/apply/wait behavior, and mark `sparkctl-legacy` as explicitly
  non-production migration/recovery compatibility that is never selected after
  an API error. Archived local-controller behavior is visibly scoped as legacy.

## Strict TDD evidence

All primary expectations were hand-derived. The external double is a real
`ThreadingHTTPServer`; its small HTTPS-to-loopback opener adapter is the actual
ControlClient network boundary. Local dependency construction, `SshBackend`,
`NodeHealthService`, `ProfileSwitcher`, and subprocess execution raise in every
routine test.

### Initial routine dispatch, digest/apply/wait, and no-fallback RED

Break caught: any of the six routine commands retained local dispatch; mutation
flags/digest/job polling were absent; or control unavailability could reach a
fallback.

```text
$ uv run pytest tests/spark_profiles/test_agent_cli.py -v
collected 19 items
18 failed, 1 passed in 9.28s
```

All six routine request/output cases, all three mutation digest/no-wait cases,
all three explicit-wait cases, and all six unavailable-control cases failed on
the missing new `request_id_factory`/API dispatch interface. The one passing
case proved the old parser already rejected an ordinary `legacy` subcommand; a
separate missing-launcher RED below proved explicit compatibility availability.

### Explicit legacy launcher RED/GREEN

Break caught: migration/recovery behavior had no separately named executable.

```text
$ uv run pytest \
  tests/spark_profiles/test_cli.py::test_bin_script_finds_the_repository_when_run_elsewhere -v
FAILED ... can't open file '.../bin/sparkctl-legacy': [Errno 2]
============================== 1 failed in 0.86s ===============================
```

After adding the launcher, the same test passed. The required adjacent run also
exercised the launcher successfully.

### Exact large-plan RED/GREEN after independent review

Break caught: the inherited sanitizer silently sliced generated-model lists to
64 elements, so an operator could see an incomplete plan under its full digest.

```text
$ uv run pytest \
  tests/spark_profiles/test_agent_cli.py::test_plan_output_preserves_every_server_target_past_sixty_four -v
FAILED ... output targets and operation_graph.targets differed after element 64
============================== 1 failed in 0.65s ===============================

$ uv run pytest \
  tests/spark_profiles/test_agent_cli.py::test_plan_output_preserves_every_server_target_past_sixty_four \
  tests/spark_profiles/test_agent_cli.py::test_apply_and_poll_failures_never_replay_or_select_legacy -v
============================== 3 passed in 1.65s ===============================
```

The regression returns 70 exact server targets at both nesting levels. The
paired apply/poll tests prove a 503 apply and missing-job poll produce bounded
control errors, issue exactly one reconciliation POST, and never select legacy.

### Exact successful-string RED/GREEN after follow-up review

Break caught: even after collection preservation, successful typed model
strings still passed through the error sanitizer and could be truncated or
rewritten. A contract-valid node display label containing `token=compute-a`
was returned as `<redacted>`.

```text
$ uv run pytest \
  'tests/spark_profiles/test_agent_cli.py::test_routine_commands_emit_exact_server_models_without_local_dependencies[argv0-response0-GET-/api/v1/nodes/status-None]' -v
FAILED ... emitted node display_name differed from the exact server model
============================== 1 failed in 0.64s ===============================

$ uv run pytest \
  'tests/spark_profiles/test_agent_cli.py::test_routine_commands_emit_exact_server_models_without_local_dependencies[argv0-response0-GET-/api/v1/nodes/status-None]' -v
============================== 1 passed in 0.61s ===============================
```

Successful routine typed models now bypass sanitization entirely. Errors and
the preserved generic admin path retain bounded redaction.

### Routine GREEN

The first complete implementation run passed the original API test set:

```text
$ uv run pytest tests/spark_profiles/test_agent_cli.py -q
19 passed in 8.72s
```

The final expanded test file additionally covers a contract-valid `published`
endpoint state, more-than-64 exact plan arrays, omitted flags defaulting to
wait, explicit `--wait`, apply-stage failure, and polling-stage failure.

## Required and adjacent verification

Required CLI/client/legacy run before review:

```text
$ uv run pytest tests/spark_profiles/test_agent_cli.py \
  tests/spark_profiles/test_control_client.py tests/spark_profiles/test_cli.py -v
============================= 171 passed in 27.30s =============================
```

Final expanded task and established runbook verification after review fixes:

```text
$ uv run pytest tests/spark_profiles/test_agent_cli.py \
  tests/spark_profiles/test_control_client.py tests/spark_profiles/test_cli.py \
  tests/runbooks -q
186 passed in 30.28s
```

Static verification:

```text
$ uvx --from ruff==0.16.1 ruff check .
All checks passed!

$ uvx --from ruff==0.16.1 ruff format --check \
  src/spark_profiles/cli.py tests/spark_profiles/test_agent_cli.py
2 files already formatted

$ git diff --check
(no output)

$ cmp -s <(git show 8d81174:src/spark_profiles/cli.py | \
  sed '1s/.*/"""Explicit legacy developer-machine controller for Spark profiles."""/') \
  src/spark_profiles/legacy_cli.py
LEGACY_EXACT_EXIT=0
```

The copied legacy module and the small existing legacy test file were not
bulk-reformatted, to preserve the old implementation and keep the migration
diff reviewable. Ruff checks all task and repository files.

## Broad-suite isolation

The optional repository-wide run has two named host-interpreter failures outside
this CLI change:

```text
$ uv run pytest -x --tb=no -vv
collected 824 items
FAILED tests/control/test_openapi_clients.py::test_generator_is_idempotent_and_admin_schema_is_secret_free
... openapi-python-client ... died with <Signals.SIGSEGV: 11>
======================== 1 failed, 137 passed in 15.12s ========================
```

The crashing generator had removed tracked generated model files before exit.
Only those test-created deletions were restored from unchanged `HEAD`, and
`git status` confirmed no generated-client diff remained.

A follow-up excluding that named generator test reached 43 percent and exited
139 inside `jsonschema.validators` while setting up
`tests/spark_profiles/test_admission.py`. A preceding ordinary broad run reached
61 percent and pytest itself exited 139 while formatting the generator failure.
No product assertion from Task 3 failed, and no further unstable broad reruns
were made after the coordinator requested focused completion.

The legacy launcher crash warning from the brief was also isolated. One early
combined run observed child return code `-11`; `-X faulthandler`, five direct
launcher runs, five focused pytest launcher runs, three combined API/launcher
runs, and both required adjacent suites then completed successfully. There was
no reproducible product failure or legacy-file difference.

## Independent review and self-review

The first independent review correctly found silent >64-element truncation as
Critical and stale production documentation as Important. It also requested a
contract-valid endpoint fixture, omitted/default wait coverage, and post-plan
apply/poll no-fallback coverage. Follow-up review then found that exact routine
strings still used the error sanitizer and that two archived blocks retained
copyable standard-launcher commands. All findings were addressed before final
verification. Final read-only review reported no residual Critical or Important
issues and verdict `Ready to merge — Yes`.

Self-review confirmed:

- standard routine imports and execution have no local/SSH dependency path;
- every mutation is plan-first, apply-explicit, digest-bound, and single-submit;
- successful structured output preserves complete server collections and
  strings;
- control errors are bounded, redacted, and never trigger compatibility mode;
- legacy behavior is byte-preserved and separately named/executable;
- the standard parser cannot select legacy explicitly or implicitly;
- admin proposal/deploy/read behavior remains behind the same API client;
- production docs do not recommend legacy and clearly scope archived behavior;
- no hard-coded Spark names, hostnames, or addresses were added to production
  routine code.

## Concerns

- Repository-wide pytest remains blocked by reproducible host interpreter
  SIGSEGVs in the OpenAPI generator and `jsonschema`, as detailed above. The
  complete required and adjacent Task 3 suites pass.
- The broader documentation migration plan still owns comprehensive historical
  runbook labeling across documents other than the README and `sparkctl`
  runbook. This task made only the production boundary updates required for the
  compatibility launcher.

---

# Fix round 1/5: installed production console journey

## Review finding and implementation

Review found that current production documentation invoked `bin/sparkctl`
directly after `uv sync`. That repository script uses ambient `python3`, while
the project did not install a console command, so the documented routine path
could fail before bounded CLI handling when the ambient interpreter lacked
`httpx`.

The fix adds the standard project entry point
`sparkctl = "spark_profiles.cli:main"` and documents the environment-aware form
`uv run --project /path/to/DGX-Forge sparkctl ...` consistently in README and
the current runbook. The existing injectable `main(...)` remains the entry
target; no parallel argument parser or wrapper behavior was added. Explicit
`bin/sparkctl-legacy` compatibility remains separate.

The outside-environment test also exposed and fixed one real transport defect:
`urllib` production responses expose headers as `HTTPMessage`, whose iteration
yields names rather than `(name, value)` pairs. `_OpenerTransport` now adapts
`response.headers.items()` into `httpx.Headers`, which works for production
responses and existing injected response doubles.

## Strict TDD evidence

The regression runs the exact documented command from a temporary directory,
removes `VIRTUAL_ENV` and `PYTHONPATH`, configures the token entirely through
environment variables, and serves the generated node model from a real local
HTTPS server with a test CA trusted through `SSL_CERT_FILE`. A sentinel SSH
binary records any forbidden local fallback.

Initial packaging RED:

```text
$ uv run pytest \
  tests/spark_profiles/test_agent_cli.py::test_documented_console_command_runs_from_outside_project_environment -v
FAILED ... error: Failed to spawn: `sparkctl`
  Caused by: No such file or directory (os error 2)
============================== 1 failed in 0.72s ===============================
```

After adding the entry point, the same boundary progressed to HTTPS and exposed
the production header adapter rather than passing prematurely:

```text
FAILED ... returncode 2
stdout={"error":"control API response does not match the generated schema",...}
============================== 1 failed in 0.84s ===============================
```

Root-cause isolation showed `httpx.Headers(response.headers)` raising
`ValueError` for the real `HTTPMessage`. After the one-line `.items()` boundary
fix, exact GREEN:

```text
$ uv run pytest \
  tests/spark_profiles/test_agent_cli.py::test_documented_console_command_runs_from_outside_project_environment -v
============================== 1 passed in 0.76s ===============================
```

The GREEN asserts exit `0`, empty stderr, the exact typed node JSON, one GET to
`/api/v1/nodes/status`, and no SSH sentinel creation.

## Fresh verification

Focused changed-boundary suites:

```text
$ uv run pytest tests/spark_profiles/test_agent_cli.py \
  tests/spark_profiles/test_control_client.py -q
118 passed in 14.19s
```

Complete required/adjacent Task 3 suites:

```text
$ uv run pytest tests/spark_profiles/test_agent_cli.py \
  tests/spark_profiles/test_control_client.py tests/spark_profiles/test_cli.py \
  tests/runbooks -q
187 passed in 29.69s
```

Static checks:

```text
$ uvx --from ruff==0.16.1 ruff check .
All checks passed!

$ uvx --from ruff==0.16.1 ruff format --check \
  src/spark_profiles/control_client.py tests/spark_profiles/test_agent_cli.py
2 files already formatted

$ git diff --check
(no output)
```

Independent read-only fix review reported no residual Critical or Important
issues and verdict `Ready to merge — Yes`.

## Fix-round concerns

None new. The deferred Minor launcher docstring remains intentionally unchanged
for the final review, per fix-round scope. The repository-wide interpreter
SIGSEGV concern recorded above is unchanged; no broad suite was rerun.
