# Task 5 scoped re-review — fix round 1

## Finding verdicts

1. **ADDRESSED** — One disposable HTTP API is exercised by generated Python `ControlClient`, actual browser `ApiClient`, and rendered `ProfilesPage`, including exact apply body, real 409 race, and visible unavailable-node fail-closed behavior.

2. **NOT ADDRESSED** — The UI refreshes fleet evidence, but the server attaches `fleet_evidence_digest` after canonical plan digesting. Apply separately compares current evidence and enqueues by plan digest, permitting a fresh evidence digest to be paired with an old plan and leaving a check/enqueue race. The job does not persist the asserted evidence digest for later revalidation.

3. **ADDRESSED** — Global gates validate commit/evidence, positive integral ordered protocol bounds, graph commit and exact target authority, duplicates, and operation membership, including empty-target plans.

4. **NOT ADDRESSED** — Fixed numeric ceilings are removed and normal traversal works, but unsigned cursors are not bound to filter/job context. Replaying a cursor with different job filters or another job can skip records while totals remain authoritative; forged syntactically valid boundaries are accepted.

5. **ADDRESSED** — Durable resume uses a conditional one-winner transition, only the winner audits, and the browser invalidates/refetches stale authority after error.

## New breakage

None beyond the two incomplete findings.

## Verdict

Needs fixes. Findings 1, 3, and 5 are addressed; findings 2 and 4 remain incomplete.
