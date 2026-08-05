# Task 5 scoped re-review — fix round 2

## Finding verdicts

1. **ADDRESSED** — Fleet evidence is obtained before planning, included in canonical plan serialization/digest, persisted in resolved plan and job payload, exact-pair checked with guarded enqueue rollback, and continuously revalidated through signed worker authority before normal plan effects.

2. **ADDRESSED** — Cursor keys use domain-separated derivation from the configured durable token secret. Versioned canonical tokens authenticate resource, order, normalized filters/job identity, and boundary; decoding rejects tamper, alternate encoding, oversize, wrong key, and cross-context replay while stable traversal remains covered.

## Preserved findings

The real client equivalence boundary, fail-closed plan gates, and atomic resume remain addressed.

## New breakage

None.

## Verdict

Approved.
