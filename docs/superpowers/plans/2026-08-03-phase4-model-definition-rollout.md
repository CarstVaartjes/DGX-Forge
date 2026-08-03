# Phase 4 model-definition rollout

The phase-4 catalog now contains one immutable, single-Spark candidate for
every required remaining model family. Candidates are deliberately recorded
as `planned`: source and checkpoint revisions are pinned, placement and
resource contracts are explicit, and all lifecycle commands route through a
fail-closed adapter boundary. A planned definition cannot be activated or
advertised as a serving endpoint.

Each candidate advances independently through `prepared -> verified ->
accepted` only after its Spark-native loader, image digest, artifact manifest,
quality fixtures, resource envelope, lifecycle recovery, and exact profile
co-residency gates have been recorded. The placeholder image digest for a
planned candidate is intentionally all-zero and must be replaced by the
immutable digest of the qualified runtime before preparation.

The source repositories and checkpoint revisions were resolved on 2026-08-03
from their public upstreams. The approved design remains the authority for
loader selection and acceptance thresholds.
