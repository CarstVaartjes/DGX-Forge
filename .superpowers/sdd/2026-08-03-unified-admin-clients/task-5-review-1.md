# Task 5 independent review

## Spec compliance

Not compliant. The main UI exists, but the required real CLI/web equivalence integration does not cross the web-client boundary, and bounded-total, acceptance-evidence, and resume-safety requirements remain incomplete.

## Critical

- The mandatory equivalence test does not exercise the real web client. The CLI side calls `ControlClient`, but the supposed web side, stale apply, and accepted apply are direct `TestClient.post` calls. The unavailable-node assertion checks raw JSON rather than the web's fail-closed display.

## Important

- Acceptance evidence is not tied to the exact plan or refreshed before apply, so a typed confirmation can remain enabled while live fleet evidence changes.
- The client gate misses empty-target commit mismatch, accepts zero/fractional/reversed protocol ranges, and does not verify operation-graph commit/targets against top-level plan authority.
- Job and operation APIs silently cap results while the UI presents truncated lengths as totals; job detail target/status fields are unbounded at the schema boundary.
- Resume is an unlocked read-then-write; concurrent operators can both succeed and audit, and client 409 leaves stale resume authority visible.

## Minor

- Route display fabricates a URL with human `display_name` instead of presenting the exact server route fields and entrypoint node ID.

## Assessment

Task quality: needs fixes.
