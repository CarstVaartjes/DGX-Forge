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

# Fix Round 1

Reviewed head: `1ae2464667ba2ed9523ade47a77ba26f8c6bbcbb`

## Finding 1: redirects and credential forwarding

Added
`test_generated_mutation_rejects_redirect_without_forwarding_credentials`,
covering HTTPS-to-HTTP and cross-origin redirect locations. The test asserts a
single credentialed mutation request, a typed rejection of the 302, and no
replay or forwarded request. Generated operations now use a transport whose
urllib boundary installs a redirect handler that never creates a redirect
request; the httpx generated client also keeps `follow_redirects=False`.

RED:

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_generated_mutation_rejects_redirect_without_forwarding_credentials -v
2 failed in 0.12s
TypeError: ControlClient.__init__() got an unexpected keyword argument 'transport'
```

GREEN:

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_generated_mutation_rejects_redirect_without_forwarding_credentials -v
2 passed in 0.09s
```

## Finding 2: descriptor-bound token validation

Added `test_client_reads_token_from_single_validated_descriptor`, which
deterministically replaces the token path at the former `Path.read_text()`
boundary. The client now opens the token once with no-follow and close-on-exec
flags where supported, validates regular-file type, owner, and mode from
`fstat()`, reads a bounded value from that same descriptor, and closes it in a
`finally` block. `test_client_closes_token_descriptor_on_validation_error`
verifies closure on a decode failure.

RED:

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_client_reads_token_from_single_validated_descriptor -v
1 failed in 0.11s
Expected Bearer original-token; observed Bearer attacker-token
```

GREEN:

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_client_reads_token_from_single_validated_descriptor tests/spark_profiles/test_control_client.py::test_client_rejects_symlink_token tests/spark_profiles/test_control_client.py::test_client_rejects_group_or_world_readable_token -v
3 passed in 0.09s

$ uv run pytest tests/spark_profiles/test_control_client.py::test_client_closes_token_descriptor_on_validation_error -v
1 passed in 0.11s
```

## Finding 3: typed HTTP status before parsing

Added a 20-case matrix in
`test_http_status_typing_precedes_unusable_error_body_parsing`: empty, HTML,
malformed JSON, and schema-invalid bodies for 401, 403, 404, 409, and 503. The
raw response is recorded at the transport boundary so mandatory status mapping
occurs even when generated parsing fails. Retry-After remains bounded to 30
seconds, and unusable bodies receive a fixed safe detail.

RED:

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_http_status_typing_precedes_unusable_error_body_parsing -v
20 failed in 0.30s
Observed ControlMalformedResponse instead of the required typed status errors
```

GREEN:

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_http_status_typing_precedes_unusable_error_body_parsing -v
20 passed in 0.10s
```

## Finding 4: remote detail and terminal reason sanitization

Added bearer-token, PEM certificate/private-key, credential, and oversized-text
cases to `test_http_error_detail_is_bounded_and_redacted` and
`test_terminal_job_reason_is_bounded_and_redacted`. HTTP error details and
terminal job reasons are sanitized before they are stored on exceptions or
included in exception text, with a 256-character maximum.

RED:

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_http_error_detail_is_bounded_and_redacted tests/spark_profiles/test_control_client.py::test_terminal_job_reason_is_bounded_and_redacted -v
8 failed in 0.18s
Raw secrets/PEM content remained visible and oversized values remained length 5000
```

GREEN:

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_http_error_detail_is_bounded_and_redacted tests/spark_profiles/test_control_client.py::test_terminal_job_reason_is_bounded_and_redacted -v
8 passed in 0.12s
```

## Fix Round 1 fresh verification

```text
$ uv run pytest tests/spark_profiles/test_control_client.py -q
63 passed in 1.19s

$ uv run pytest tests/control/test_openapi_clients.py -q
5 passed in 3.63s

$ uv run --with ruff==0.16.1 ruff check src/spark_profiles/control_client.py tests/spark_profiles/test_control_client.py
All checks passed!

$ uv run --with ruff==0.16.1 ruff format --check src/spark_profiles/control_client.py tests/spark_profiles/test_control_client.py
2 files already formatted

$ git diff --check
[no output; exit 0]
```

Self-review confirmed no generated source edits, no mutation replay, no token
path reopen after validation, mandatory typed status mapping before body parsing,
and bounded remote text on the exception surfaces covered by this task. The
pre-existing unrelated `progress.md` modification remains unstaged. The prior
out-of-scope legacy CLI/jsonschema SIGSEGV concern above is unchanged; scoped
and adjacent verification are green.

# Fix Round 2

Reviewed head: `6a06f090755e1902b3636e78fd2acfbffdbe59bc`

## Finding 1: every production mutation rejects redirects

Replaced the mock-transport redirect regression with
`test_production_boundary_rejects_mutation_redirect_without_forward_or_replay`.
It runs urllib's real redirect/error processing with a controlled protocol
handler for generated `apply_plan()` and preserved `create_proposal()` and
`submit_change()`, against both HTTPS-to-HTTP and cross-origin HTTPS locations.
Each case proves exactly one credentialed request and no redirected request or
mutation replay. When `opener=` is omitted, the client now builds a
redirect-denying urllib opener and uses it for preserved requests too.

RED (after removing an over-specific error-message assertion so the test
measured rejection/no forwarding):

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_production_boundary_rejects_mutation_redirect_without_forward_or_replay -v
2 passed, 4 failed in 0.22s
The generated apply cases rejected; proposal/change followed the redirects and did not raise
```

GREEN:

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_production_boundary_rejects_mutation_redirect_without_forward_or_replay -v
6 passed in 0.24s
```

## Finding 2: missing no-follow support fails closed

Added
`test_client_fails_closed_before_open_when_no_follow_flag_is_unavailable`.
It removes `os.O_NOFOLLOW`, installs an `os.open` tripwire, and proves the
client rejects the platform before any path open. Supported platforms retain
the single `open`/`fstat`/bounded-read/`close` sequence.

RED:

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_client_fails_closed_before_open_when_no_follow_flag_is_unavailable -v
1 failed in 0.13s
Expected safe-open rejection; observed the fallback path call os.open
```

GREEN with adjacent descriptor checks:

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_client_fails_closed_before_open_when_no_follow_flag_is_unavailable tests/spark_profiles/test_control_client.py::test_client_reads_token_from_single_validated_descriptor tests/spark_profiles/test_control_client.py::test_client_closes_token_descriptor_on_validation_error tests/spark_profiles/test_control_client.py::test_client_rejects_symlink_token -v
4 passed in 0.09s
```

## Finding 3: recursive JSON preserves mandatory status typing

Added a five-status matrix in
`test_http_status_typing_precedes_recursive_json_failure` and the successful
response control
`test_successful_recursive_json_is_reported_as_malformed_response`. The final
fixture is valid JSON with depth 10,000 and size 20,001 bytes; it deterministically
raises `RecursionError` in this runtime. Error statuses are mapped with safe
detail and bounded Retry-After from the recorded raw response, while a 200
response becomes a visible `ControlMalformedResponse` nesting error.

Fixture calibration at depth 2,000 produced five passes and one schema-error
failure because the decoder accepted that depth. The corrected fixture then
provided the required RED:

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_http_status_typing_precedes_recursive_json_failure tests/spark_profiles/test_control_client.py::test_successful_recursive_json_is_reported_as_malformed_response -v
6 failed in 0.32s
All six exposed uncaught RecursionError from json.decoder
```

GREEN:

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_http_status_typing_precedes_recursive_json_failure tests/spark_profiles/test_control_client.py::test_successful_recursive_json_is_reported_as_malformed_response -v
6 passed in 0.16s
```

## Finding 4: token-aware and certificate-aware remote-text redaction

Extended both HTTP-detail and terminal-job-reason matrices with bare literal
client tokens, delimiter-free `client_certificate`/`x509` values, and oversized
certificate-labeled values. The client's actual token is now passed as a
sensitive value into both exception sanitization paths. Certificate-like
assignments are redacted before bounding, and attributes plus exception strings
are checked for every forbidden value.

RED:

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_http_error_detail_is_bounded_and_redacted tests/spark_profiles/test_control_client.py::test_terminal_job_reason_is_bounded_and_redacted -v
8 passed, 6 failed in 0.32s
Bare token, certificate-labeled, and oversized certificate content leaked on both surfaces
```

GREEN:

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_http_error_detail_is_bounded_and_redacted tests/spark_profiles/test_control_client.py::test_terminal_job_reason_is_bounded_and_redacted -v
14 passed in 0.24s
```

## Finding 5: one injectable request boundary

Added `test_supplied_opener_is_used_by_generated_and_preserved_methods`. A
caller-supplied opener returns one generated response and one preserved
proposal response, while a fallback `build_opener` tripwire proves no alternate
network boundary is reached. `_OpenerTransport` now adapts the same opener used
by `request()`; the separate public `transport=` path and its test shim were
removed. The omitted/default opener remains the redirect-denying production
boundary from Finding 1.

RED:

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_supplied_opener_is_used_by_generated_and_preserved_methods -v
1 failed in 0.16s
AssertionError: caller-supplied opener was bypassed
```

GREEN:

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_supplied_opener_is_used_by_generated_and_preserved_methods -v
1 passed in 0.12s
```

## Fix Round 2 fresh verification

```text
$ uv run pytest tests/spark_profiles/test_control_client.py -q
81 passed in 1.36s

$ uv run pytest tests/control/test_openapi_clients.py -q
5 passed in 3.65s

$ uv run --with ruff==0.16.1 ruff check src/spark_profiles/control_client.py tests/spark_profiles/test_control_client.py
All checks passed!

$ uv run --with ruff==0.16.1 ruff format --check src/spark_profiles/control_client.py tests/spark_profiles/test_control_client.py
2 files already formatted

$ git diff --check
[no output; exit 0]
```

Self-review confirmed one opener boundary for generated and preserved methods,
one safe default production opener, no mutation replay, fail-closed missing
no-follow support, typed status mapping across recursive decoder failures, and
token/certificate redaction before exception storage or display. No generated
source was edited. The pre-existing unrelated `progress.md` modification remains
unstaged, and the previously reported out-of-scope legacy CLI/jsonschema SIGSEGV
concern is unchanged.

# Fix Round 3

Reviewed head: `c3b00d5fe4cb285d4f76d8cb2e5b0a36bba4b0c7`

## Residual finding: safe copied timeout observations

Added three timeout-path regressions:

- `test_wait_job_timeout_stores_safe_bounded_observation` covers ordinary
  nonterminal timeout with a bare client token, PEM certificate/private key,
  credential assignment, oversized text, `certificate_pem`, `cert_pem`,
  `chain_pem`, and mixed-case/spacing assignment syntax.
- `test_wait_job_transient_timeout_stores_safe_bounded_observation` covers both
  a transient transport failure and a typed 503 after a prior queued
  observation.
- `test_wait_job_timeout_copies_observation_before_sanitizing` proves the
  generated/caller observation is not mutated and the exception stores a
  separate generated `JobDetailResponse` copy.

All timeout tests assert that non-sensitive identifiers, state, attempt, commit,
and progress fields remain intact; stored `status_reason` values stay within 256
characters; and neither the observation nor exception string exposes the
original secret/certificate content.

RED:

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_wait_job_timeout_stores_safe_bounded_observation tests/spark_profiles/test_control_client.py::test_wait_job_transient_timeout_stores_safe_bounded_observation tests/spark_profiles/test_control_client.py::test_wait_job_timeout_copies_observation_before_sanitizing -v
11 failed in 0.27s
ControlTimeout retained the raw observation by identity; secrets and oversized reasons remained stored
```

GREEN:

```text
$ uv run pytest tests/spark_profiles/test_control_client.py::test_wait_job_timeout_stores_safe_bounded_observation tests/spark_profiles/test_control_client.py::test_wait_job_transient_timeout_stores_safe_bounded_observation tests/spark_profiles/test_control_client.py::test_wait_job_timeout_copies_observation_before_sanitizing -v
11 passed in 0.12s
```

`ControlTimeout` now creates its stored observation with the generated model's
`to_dict()`/`from_dict()` round trip, sanitizes only the copied
`status_reason`, and receives the client's literal token as a sensitive value at
all ordinary and transient timeout raises. The assignment sanitizer now covers
`certificate_pem`, `cert_pem`, `chain_pem`, and `client_certificate_pem`
case-insensitively with existing flexible separator spacing. Generated response
typing is unchanged, and no polling-parameter behavior was modified.

## Fix Round 3 fresh verification

```text
$ uv run pytest tests/spark_profiles/test_control_client.py -q
92 passed in 1.38s

$ uv run pytest tests/control/test_openapi_clients.py -q
5 passed in 3.89s

$ uv run --with ruff==0.16.1 ruff check src/spark_profiles/control_client.py tests/spark_profiles/test_control_client.py
All checks passed!

$ uv run --with ruff==0.16.1 ruff format --check src/spark_profiles/control_client.py tests/spark_profiles/test_control_client.py
2 files already formatted

$ git diff --check
[no output; exit 0]
```

Self-review confirmed timeout observations are copied before sanitization,
generated/caller objects remain unchanged, every timeout construction path
passes token context, certificate assignment labels are covered exactly, and no
generated source or deferred polling parameter was changed. The pre-existing
unrelated `progress.md` modification remains unstaged; the previously reported
legacy CLI/jsonschema SIGSEGV concern is unchanged.
