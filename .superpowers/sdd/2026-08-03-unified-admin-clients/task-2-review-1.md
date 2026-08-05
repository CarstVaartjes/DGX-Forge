# Task 2 review round 1

## Spec compliance

- **Critical:** `_OpenerTransport` delegates to the default urllib opener, which follows redirects before httpx can enforce `follow_redirects=False`; Python can forward the bearer token from the configured HTTPS origin to another or HTTP origin.
- **Important:** token validation uses separate path operations (`is_symlink`, `is_file`, `stat`, `read_text`) and is vulnerable to replacement between validation and read. It must open once with no-follow semantics, validate that descriptor, and read the same descriptor.
- **Important:** content-type/JSON/schema validation occurs before status mapping, so malformed or non-JSON 401/403/409/503 responses lose their required typed outcomes.
- **Important:** raw server-controlled HTTP detail and job terminal reason text is interpolated into exception strings that existing CLI JSON can expose. Output must be bounded and sanitized/redacted.

## Code quality

- **Minor (deferred to final review):** `wait_job` does not validate negative, zero, non-finite, or otherwise unsafe timeout/interval values and may leak `ValueError` or busy-spin.

## Verdict

Spec compliance and task quality both require fixes. Fix round 1 covers the Critical and Important findings above.
