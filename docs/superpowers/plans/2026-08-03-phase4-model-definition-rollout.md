# Phase 4 model-definition rollout

The phase-4 catalog contains one immutable, single-Spark candidate for every
required remaining model family. Source and checkpoint revisions are pinned,
placement and resource contracts are explicit, and all lifecycle commands route
through a fail-closed adapter boundary. TripoSG has advanced to `verified` on
Spark 2; the other candidates remain `planned` and cannot be activated or
advertised as serving endpoints.

Each candidate advances independently through `prepared -> verified ->
accepted` only after its Spark-native loader, image digest, artifact manifest,
quality fixtures, resource envelope, lifecycle recovery, and exact profile
co-residency gates have been recorded. Unqualified candidates remain behind
the fail-closed adapter and are not advertised as endpoints.

The source repositories and checkpoint revisions were resolved on 2026-08-03
from their public upstreams. The approved design remains the authority for
loader selection and acceptance thresholds. TripoSG qualification evidence is
in `docs/audits/2026-08-03-triposg-runtime-qualification.json`; its runtime
uses its own source, weights, cache/venv, inputs, outputs, logs, PID, and
endpoint namespace. TokenRig/SkinTokens now has the same isolated adapter
boundary, but remains `planned` because Spark2 lacks the official Blender
>=4.2 prerequisite; the gate is recorded in
`docs/audits/2026-08-03-tokenrig-prerequisite-gate.json`.
