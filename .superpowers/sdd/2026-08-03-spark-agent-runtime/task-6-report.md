# Task 6 report — deterministic Spark agent lifecycle acceptance

Date: 2026-08-05
Overall program task: 15
Plan task: 6
Branch: `main` (direct-main pre-release policy)
Base: `ea61813837987fd3e34dd6ec1a9af7ffed7a4df0`
Source commit: `b5c00e8255dd6037374182c46ce4b6896392ef59`
Brief: `.superpowers/sdd/2026-08-03-spark-agent-runtime/task-6-brief.md`

## Outcome and interfaces

Task 6 adds a deterministic, seeded failure-injection simulator and a canonical
acceptance CLI for the outbound Spark agent lifecycle:

- `agent/src/dgx_agent/simulator.py` exposes
  `simulate_agent_lifecycle`, `lifecycle_evidence_passes`, canonical report
  encoding, and the shared acceptance argument/scale policy;
- `tests/agent/test_failure_matrix.py` exercises the matrix at one and sixteen
  agents, deterministic replay, canonical evidence, security-fault gating, and CLI
  scale controls;
- `scripts/accept-agent-lifecycle` re-executes in the locked agent project, accepts
  node count/seed/output options, and emits only canonical simulated JSON.

The simulator injects disconnect, process crash, stale fence, bad artifact, bad
certificate, and failed activation without SSH, shelling into a Spark, or opening a
worker-side listener. Across both covered fleet sizes it requires zero duplicate
mutations, zero cross-node claims accepted, zero stale results accepted, reconnect and
crash recovery for every node, and rollback for both bad-update paths.

## Deterministic fault matrix

For `N = 1` and `N = 16`, the accepted report requires:

| Fault | Required evidence |
|---|---|
| disconnect | `N` injections and `N` reconnect recoveries, with one durable mutation per claim |
| crash | `N` injections and `N` replay recoveries, without repeating the already durable mutation |
| stale fence | `N` claim rejections, `N` result rejections, and zero durable mutations |
| bad artifact | `N` validation failures and `N` rollbacks to the prior active digest |
| bad certificate | `N` authentication rejections and zero state or release mutations |
| failed activation | `N` activation failures and `N` rollbacks before active-pointer commit |

The aggregate invariants additionally require `2N` bad-update rollbacks, `N` crash
recoveries, `N` reconnect recoveries, and zero duplicate mutations, accepted
cross-node claims, or accepted stale results. Node IDs, claims, fences, digests, and
timestamps are derived deterministically from the supplied seed.

## Real and simulated boundary

This is protocol and recovery acceptance evidence, not physical Spark evidence. Every
scenario uses the production `AgentStateStore` against a temporary durable state root
and the production `OperationRegistry` for typed claim validation, dispatch, replay,
and result handling. The transport, certificate authority decision, inventory probe,
and A/B release-host effects are deliberately represented in memory so all faults are
repeatable and require no remote machine. The report therefore always states:

- `evidence_kind: simulated`;
- `environment: deterministic-in-memory-transport`;
- `physical_sparks_exercised: false`.

The simulator does not prove real network loss, certificate issuance/revocation,
ORAS/TUF transfer, systemd restart, GPU behavior, or physical A/B activation. Those
remain later installed-host and physical release gates.

## RED/GREEN and implementation

The task-start RED was observed exactly as planned: collecting
`tests/agent/test_failure_matrix.py` failed while importing
`dgx_agent.simulator`, because that module did not exist. Implementation then added
the seeded clock, deterministic identities, in-memory effect boundaries, real state
and operation integration, complete evidence evaluator, canonical encoder, CLI, and
the one- and sixteen-node behavioral matrix.

The CLI output is UTF-8, key-sorted, compact JSON with no non-finite values and one
trailing newline. Re-encoding parsed output is byte-identical. `--output` writes the
same canonical bytes, while `--json` or the absence of an output path writes them to
standard output; successful acceptance exits zero.

## Independent review and fix round 1

The first independent review found two Important issues:

1. Aggregate invariants alone could label evidence passed without requiring every
   bad-certificate and stale-fence security counter, including their zero-mutation
   guarantees. `lifecycle_evidence_passes` now requires the exact complete fault map
   and exact invariant map. Four regressions mutate rejection/mutation counters and
   prove each incomplete or unsafe report fails the gate.
2. The CLI needed protection from an accidentally expensive fleet request without
   creating a product/API hard limit. It now rejects more than 256 nodes by default
   before simulation and explains the explicit `--allow-large-fleet` override. The
   library API accepts every positive integer and has no absolute fleet-size ceiling;
   its time and storage use remain linear in the requested count.

The scoped independent rereview found both Important issues addressed, no remaining
Critical/Important/Minor findings, and returned **Ready: Yes**.

## Verification evidence

- The one- and sixteen-node fault matrix, evaluator regressions, deterministic encoding,
  CLI, and installer combination completed as `38 passed`:
  `uv run pytest tests/agent/test_failure_matrix.py
  tests/nodes/test_install_dgx_agent.py -q`.
- The agent suite completed as `514 passed, 1 deselected`. The one deselected test was
  `test_builds_one_self_contained_native_elf_with_isolated_module_smoke` (observed
  under pytest's `test_builds_one_self_contained0` temporary path), after its local
  PyInstaller build entered the known CPU-bound hang on the shared host; it is not
  recorded as green by this run.
- `scripts/accept-agent-lifecycle --nodes 1 --json` and `--nodes 16 --json` exited
  zero with canonical, explicitly simulated reports and the complete six-fault map.
- A 257-node CLI request failed with usage status 2 before simulation unless the
  explicit large-fleet override was supplied; the override removes only the CLI
  safety guard, not input validation.
- Pinned Ruff over the three changed Task 6 files completed with
  `All checks passed!`; direct `py_compile` of the simulator, failure-matrix test,
  and acceptance script exited zero; and `git diff --check` was clean.

Task 6 is complete and accepted at source commit
`b5c00e8255dd6037374182c46ce4b6896392ef59`.
