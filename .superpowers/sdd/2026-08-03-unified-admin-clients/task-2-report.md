# Task 2 report: control client polling and typed failures

## Status and scope

Implemented Task 2 on `feature/unified-admin-clients` from base
`0d91b4c6005990ba09b559a055de900cea88a2c1`.

Changed only:

- `src/spark_profiles/control_client.py`
- `tests/spark_profiles/test_control_client.py`
- this report

The tracked generated package under `src/spark_profiles/generated_control/` was
wrapped and verified but not manually edited. Existing `create_proposal()`,
`get()`, and `submit_change()` APIs remain present for the existing proposal CLI.

## Implementation

- Added `nodes()`, `plan_profile()`, `apply_plan()`, `job()`, `wait_job()`,
  `endpoint()`, and `agents()` over the generated `AuthenticatedClient`, generated
  endpoint functions, generated request models, and generated response models.
- Kept the existing injectable urllib HTTP boundary while adapting it to the
  generated httpx client. The boundary preserves HTTPS verification, bearer
  authentication, the 1,048,576-byte response limit, JSON content-type checks,
  and normalized failures that never include token values.
- Added typed HTTP failures for 401, 403, 404, 409, and 503, plus typed malformed,
  oversized, transport, and timeout failures.
- Required a caller-supplied canonical UUID request ID for `apply_plan()` and
  sent it as `X-Request-ID`. An ambiguous mutation transport failure is returned
  after exactly one attempt; it is never transparently replayed.
- Implemented GET-only job polling. `succeeded` returns the generated structured
  `JobDetailResponse`; `failed`/`expired` raise `JobFailed`; operator wait raises
  `JobWaitingForOperator`; deadline expiry raises `ControlTimeout` with the last
  structured observation.
- Safe GET polling retries transient transport failures and 503 responses.
  Numeric `Retry-After` values are clamped to 1 through 30 seconds; malformed or
  negative values fall back to the caller interval.
- Tightened the existing boundary so the base URL is an HTTPS origin (no path)
  and token files cannot be symlinks or group/world accessible.

## Strict TDD evidence

Each behavior below named the production break before its test was written. All
expectations were hand-derived. The only fakes were HTTP opener responses (plus
real wall-clock observation for timeout/backoff tests).

### 1. Generated operational wrappers and explicit request ID

Break caught: missing/misrouted wrappers, wrong generated model type, wrong apply
body, or omitted caller request ID.

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_operational_methods_use_generated_models_and_exact_routes -v
FAILED ... AttributeError: 'ControlClient' object has no attribute 'nodes'
============================== 1 failed in 0.11s ===============================

$ uv run pytest tests/spark_profiles/test_control_client.py::test_operational_methods_use_generated_models_and_exact_routes -v
PASSED
============================== 1 passed in 0.09s ===============================
```

### 2. Typed 401/403/409/503 failures

Break caught: collapsing authorization, conflict, and availability outcomes into
one generic error.

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_control_statuses_raise_typed_failures -v
FAILED ... module 'spark_profiles.control_client' has no attribute 'ControlUnauthorized'
FAILED ... module 'spark_profiles.control_client' has no attribute 'ControlForbidden'
FAILED ... module 'spark_profiles.control_client' has no attribute 'ControlConflict'
FAILED ... module 'spark_profiles.control_client' has no attribute 'ControlUnavailable'
============================== 4 failed in 0.11s ===============================

$ uv run pytest tests/spark_profiles/test_control_client.py::test_control_statuses_raise_typed_failures -v
============================== 4 passed in 0.09s ===============================
```

### 3. Typed 404

Break caught: returning a missing job/endpoint as an undifferentiated failure.

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_missing_resource_raises_typed_not_found -v
FAILED ... module 'spark_profiles.control_client' has no attribute 'ControlNotFound'
============================== 1 failed in 0.10s ===============================

$ uv run pytest tests/spark_profiles/test_control_client.py::test_missing_resource_raises_typed_not_found -v
============================== 1 passed in 0.09s ===============================
```

### 4. Malformed JSON

Break caught: leaking the generated parser's JSON exception.

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_malformed_json_raises_typed_response_failure -v
FAILED ... module 'spark_profiles.control_client' has no attribute 'ControlMalformedResponse'
============================== 1 failed in 0.10s ===============================

$ uv run pytest tests/spark_profiles/test_control_client.py::test_malformed_json_raises_typed_response_failure -v
============================== 1 passed in 0.09s ===============================
```

### 5. Oversized response

Break caught: reading/parsing more than 1,048,576 response bytes.

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_oversized_generated_response_is_rejected_before_parsing -v
FAILED ... module 'spark_profiles.control_client' has no attribute 'ControlResponseTooLarge'
============================== 1 failed in 0.10s ===============================

$ uv run pytest tests/spark_profiles/test_control_client.py::test_oversized_generated_response_is_rejected_before_parsing -v
============================== 1 passed in 0.09s ===============================
```

### 6. JSON content type

Break caught: accepting JSON-looking HTML/text at the authenticated API boundary.

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_generated_response_requires_json_content_type -v
FAILED ... DID NOT RAISE ControlMalformedResponse
============================== 1 failed in 0.10s ===============================

$ uv run pytest tests/spark_profiles/test_control_client.py::test_generated_response_requires_json_content_type -v
============================== 1 passed in 0.09s ===============================
```

### 7. No replay after ambiguous mutation failure

Break caught: replaying apply or leaking a token-bearing transport detail.

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_ambiguous_apply_transport_failure_is_not_replayed -v
FAILED ... module 'spark_profiles.control_client' has no attribute 'ControlTransportError'
============================== 1 failed in 0.11s ===============================

$ uv run pytest tests/spark_profiles/test_control_client.py::test_ambiguous_apply_transport_failure_is_not_replayed -v
============================== 1 passed in 0.09s ===============================
```

### 8. Real urllib HTTP error behavior

Break caught: classifying live `urlopen` 4xx/5xx `HTTPError` responses as
transport failures rather than parsing their bounded bodies/statuses.

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_urlopen_http_error_body_keeps_typed_status_mapping -v
FAILED ... ControlTransportError: control API request failed: HTTPError
============================== 1 failed in 0.14s ===============================

$ uv run pytest tests/spark_profiles/test_control_client.py::test_urlopen_http_error_body_keeps_typed_status_mapping -v
============================== 1 passed in 0.09s ===============================
```

### 9. Bounded Retry-After

Break caught: trusting zero, excessive, or malformed server delays.

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_retry_after_is_bounded_to_safe_seconds -v
FAILED [0-1] ... no attribute 'retry_after_seconds'
FAILED [1-1] ... no attribute 'retry_after_seconds'
FAILED [17-17] ... no attribute 'retry_after_seconds'
FAILED [31-30] ... no attribute 'retry_after_seconds'
FAILED [invalid-None] ... no attribute 'retry_after_seconds'
============================== 5 failed in 0.14s ===============================

$ uv run pytest tests/spark_profiles/test_control_client.py::test_retry_after_is_bounded_to_safe_seconds -v
============================== 5 passed in 0.09s ===============================
```

### 10. Terminal success/failure/operator wait

Break caught: missing `wait_job()`, returning failed/operator-blocked jobs as
success, or returning unstructured dictionaries.

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_wait_job_returns_structured_terminal_success tests/spark_profiles/test_control_client.py::test_wait_job_raises_typed_terminal_failure -v
FAILED ... 'ControlClient' object has no attribute 'wait_job'
FAILED ... module 'spark_profiles.control_client' has no attribute 'JobFailed'
FAILED ... no attribute 'JobWaitingForOperator'
============================== 3 failed in 0.12s ===============================

$ uv run pytest tests/spark_profiles/test_control_client.py::test_wait_job_returns_structured_terminal_success tests/spark_profiles/test_control_client.py::test_wait_job_raises_typed_terminal_failure -v
============================== 3 passed in 0.09s ===============================
```

### 11. GET-only queued/running polling

Break caught: stopping at an ordinary nonterminal state or polling by mutation.

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_wait_job_polls_only_get_until_terminal_state -v
FAILED ... ControlClientError: control job ... is not terminal
============================== 1 failed in 0.12s ===============================

$ uv run pytest tests/spark_profiles/test_control_client.py::test_wait_job_polls_only_get_until_terminal_state -v
============================== 1 passed in 0.09s ===============================
```

### 12. Poll deadline

Break caught: polling an endlessly queued job forever or discarding its last
observation.

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_wait_job_times_out_with_last_observation -v
FAILED ... module 'spark_profiles.control_client' has no attribute 'ControlTimeout'
============================== 1 failed in 0.11s ===============================

$ uv run pytest tests/spark_profiles/test_control_client.py::test_wait_job_times_out_with_last_observation -v
============================== 1 passed in 0.09s ===============================
```

### 13. Honor Retry-After during GET polling

Break caught: immediately surfacing transient 503 from a safe job GET.

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_wait_job_honors_bounded_retry_after_on_get -v
FAILED ... ControlUnavailable: control API returned HTTP 503
============================== 1 failed in 0.12s ===============================

$ uv run pytest tests/spark_profiles/test_control_client.py::test_wait_job_honors_bounded_retry_after_on_get -v
============================== 1 passed in 1.09s ===============================
```

### 14. Retry ambiguous GET only

Break caught: failing safe GET convergence after one transport reset (paired
with the mutation one-attempt test above).

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_wait_job_retries_ambiguous_get_transport_failure -v
FAILED ... ControlTransportError: control API request failed: URLError
============================== 1 failed in 0.14s ===============================

$ uv run pytest tests/spark_profiles/test_control_client.py::test_wait_job_retries_ambiguous_get_transport_failure -v
============================== 1 passed in 0.09s ===============================
```

### 15. Expired terminal job

Break caught: polling an already terminal expired job until timeout.

```text
$ uv run pytest 'tests/spark_profiles/test_control_client.py::test_wait_job_raises_typed_terminal_failure[expired-JobFailed]' -v
FAILED ... ControlTimeout: timed out waiting for control job ...
============================== 1 failed in 1.12s ===============================

$ uv run pytest 'tests/spark_profiles/test_control_client.py::test_wait_job_raises_typed_terminal_failure[expired-JobFailed]' -v
============================== 1 passed in 0.09s ===============================
```

### 16. Token-file permissions

Break caught: accepting a group-readable bearer-token file.

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_client_rejects_group_or_world_readable_token -v
FAILED ... DID NOT RAISE ControlClientError
============================== 1 failed in 0.12s ===============================

$ uv run pytest tests/spark_profiles/test_control_client.py::test_client_rejects_group_or_world_readable_token -v
============================== 1 passed in 0.09s ===============================
```

### 17. Canonical caller request ID

Break caught: letting the server silently replace an invalid mutation request ID.

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_apply_rejects_noncanonical_request_id_before_network -v
FAILED ... AssertionError: invalid request ID reached the network
============================== 1 failed in 0.14s ===============================

$ uv run pytest tests/spark_profiles/test_control_client.py::test_apply_rejects_noncanonical_request_id_before_network -v
============================== 1 passed in 0.09s ===============================
```

### 18. Generated model shape normalization

Break caught: leaking `KeyError` for syntactically valid JSON that does not match
the generated response schema.

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_malformed_generated_model_raises_typed_response_failure -v
FAILED ... KeyError: 'nodes'
============================== 1 failed in 0.13s ===============================

$ uv run pytest tests/spark_profiles/test_control_client.py::test_malformed_generated_model_raises_typed_response_failure -v
============================== 1 passed in 0.10s ===============================
```

### 19. HTTPS origin path validation

Break caught: accepting a base URL containing a path instead of an origin.

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_client_rejects_control_url_with_path -v
FAILED ... DID NOT RAISE ControlClientError
============================== 1 failed in 0.12s ===============================

$ uv run pytest tests/spark_profiles/test_control_client.py::test_client_rejects_control_url_with_path -v
============================== 1 passed in 0.09s ===============================
```

## Fresh verification

Focused Task 2 suite:

```text
$ uv run pytest tests/spark_profiles/test_control_client.py -v
collected 31 items
============================== 31 passed in 1.16s ===============================
```

Adjacent generated-client/security/drift regression suite:

```text
$ uv run pytest tests/control/test_openapi_clients.py -v
collected 5 items
============================== 5 passed in 3.83s ===============================
```

Pinned lint, formatting, and diff checks:

```text
$ uv run --with ruff==0.16.1 ruff format src/spark_profiles/control_client.py tests/spark_profiles/test_control_client.py
2 files left unchanged

$ uv run --with ruff==0.16.1 ruff check src/spark_profiles/control_client.py tests/spark_profiles/test_control_client.py
All checks passed!

$ uv run --with ruff==0.16.1 ruff format --check src/spark_profiles/control_client.py tests/spark_profiles/test_control_client.py
2 files already formatted

$ git diff --check
[no output; exit 0]
```

The generated-client verification left no generated or OpenAPI drift; before
the report, `git status --short` contained only the intended client and test
files.

## Adjacent verification concern

An attempted combined adjacent run collected 96 tests and passed all 31 client
tests plus the first legacy CLI tests, then the Python interpreter segfaulted in
unchanged `jsonschema` validation code:

```text
$ uv run pytest tests/spark_profiles/test_control_client.py tests/spark_profiles/test_cli.py tests/control/test_openapi_clients.py -v
... test_control_client.py: 31 passed ...
... test_cli.py::test_prepare_failed_and_blocked_have_distinct_exit_codes
Fatal Python error: Segmentation fault
... jsonschema/validators.py ...
```

Systematic isolation showed the named test passes alone:

```text
$ uv run pytest tests/spark_profiles/test_cli.py::test_prepare_failed_and_blocked_have_distinct_exit_codes -v
============================== 1 passed in 1.23s ===============================
```

The full legacy CLI file then reproduced a SIGSEGV at varying locations,
including collection and different catalog validations. A fresh isolated uv
environment also reproduced it at a different legacy CLI test:

```text
$ uv run --isolated pytest tests/spark_profiles/test_cli.py -q
Installed 18 packages in 13ms
......Fatal Python error: Segmentation fault
... jsonschema/validators.py ...
... test_endpoint_refuses_workload_when_controller_is_stopped ...
```

This task does not touch `contracts.py`, `catalog.py`, schemas, jsonschema, or
the affected legacy CLI paths. No speculative out-of-scope change was made.
The proposal CLI compatibility behavior is directly covered in the 31-test
focused suite, and the separately run generated-client suite is green.

## Self-review

- Generated request/response definitions are reused; no duplicate DTOs were
  introduced and no generated source was edited.
- Every operation uses the generated route function and returns its generated
  response type.
- Only `wait_job()` replays, and it calls only `job()`/GET. `apply_plan()` makes
  one attempt and requires a reusable caller ID.
- Status, malformed response, response-size, content-type, terminal-state,
  timeout, and retry branches each have behavioral mutation coverage.
- Error messages contain bounded status/reason data or exception type names,
  never bearer token text or certificate material.
- Existing proposal APIs and their CLI regression remain intact.
- No SSH, local controller, bootstrap/recovery fallback, generated edit, merge,
  or push was introduced.

## Concerns

No blocking Task 2 concern. The reproducible out-of-scope Python/jsonschema
SIGSEGV prevents claiming that the entire unrelated legacy CLI file is green in
this environment; the exact isolated legacy test and all scoped/adjacent client
verification described above pass.
