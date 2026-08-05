# Task 2 fix round 1 re-review

## Finding verdicts

1. **NOT ADDRESSED — Critical redirect/token forwarding:** generated calls reject redirects, but preserved proposal/change mutations still use the redirect-following opener; the new test used a mock transport rather than the production urllib handler.
2. **NOT ADDRESSED — Important descriptor-bound token read:** Linux uses one descriptor, but the missing-`O_NOFOLLOW` fallback recreates a check/open race and must fail closed.
3. **NOT ADDRESSED — Important status-first typing:** common unusable bodies are typed, but deeply nested JSON can raise uncaught `RecursionError` before status mapping.
4. **NOT ADDRESSED — Important redaction:** labeled forms are redacted, but the client's literal token and certificate-labeled material without PEM delimiters can still escape in HTTP/job exception text.

## New breakage

- **Important:** the constructor's existing `opener=` injection is no longer used for generated operations, which can bypass caller TLS/proxy behavior and unexpectedly perform network I/O.

## Verdict

Fix round 1 failed; all four original findings plus the new Important compatibility regression enter round 2.
