# Phase 4 model-definition rollout

The active qualification pass is intentionally LLM-only. It covers the
accepted Mia service, the DS4 single-Spark definition (including a possible
`bleysg` DSpark merge into the DS4 branch), Nemotron, Qwen3-VL, and Laguna S
2.1. The image/3D definitions remain cataloged for the broader approved
design, but are deferred from this pass and cannot be activated or advertised
as serving endpoints.

Each LLM candidate has one immutable, single-Spark or dual-Spark definition.
Source and checkpoint revisions are pinned, placement and resource contracts
are explicit, and all lifecycle commands route through a fail-closed adapter
boundary. Laguna S 2.1 is cataloged as `laguna-s21-single` and remains
`planned` pending a Spark-native runtime qualification.

Each candidate advances independently through `prepared -> verified ->
accepted` only after its Spark-native loader, image digest, artifact manifest,
quality fixtures, resource envelope, lifecycle recovery, and exact profile
co-residency gates have been recorded. Unqualified candidates remain behind
the fail-closed adapter and are not advertised as endpoints.

The source repositories and checkpoint revisions were resolved on 2026-08-03
from their public upstreams. The approved design remains the authority for
loader selection and acceptance thresholds. Every definition owns a distinct
adapter directory and command path; its `paths.scratch` is reserved for that
model's venv and runtime cache, with no shared generic adapter. The creative
qualification records remain historical evidence: TripoSG is `verified` and
TokenRig/SkinTokens is `planned` behind its Blender >=4.2 prerequisite, but
neither is part of the active LLM-only pass.
