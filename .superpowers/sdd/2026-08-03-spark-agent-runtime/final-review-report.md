# Spark agent runtime — final review report

Date: 2026-08-05
Final reviewed commit: `b79296ae21ff717a4c38d577d407dbe690b942a5`
Review status: **Plan Ready: Yes**
Publication status: **Pending hosted CI**

## Six-task outcome

| Task | Accepted outcome |
|---|---|
| 1 | Strict agent configuration, identity, and durable fenced state with restart-safe terminal delivery. |
| 2 | Closed typed operation registry, bounded node probing, and a pinned, supervised NVIDIA lifecycle-tool adapter. |
| 3 | TUF-authorized, digest-pinned ORAS release installation plus typed workload operations and private read-only Distribution integration. |
| 4 | Outbound enrollment and long polling, durable result replay, fencing, and two-phase certificate rotation. |
| 5 | Stable A/B supervision, hardened systemd packaging, a generic per-Spark local installer, and the production Task 3 runtime wiring. |
| 6 | Deterministic one- and sixteen-node lifecycle acceptance covering six injected faults with explicit simulated-evidence boundaries. |

All six tasks completed their scoped implementation and independent review loops. The
final integrated review also accepted the combined runtime plan. This readiness result
does not remove the installed-host and physical Spark gates documented by the task
reports.

## Integrated review history

The initial whole-plan review found **1 Critical and 2 Important** issues: production
operations did not renew live leases; recovered compensation and operator-intervention
dispositions were not durably resolved; and simulator rollback evidence treated an
unchanged active slot as a rollback transition.

The corrections were integrated as follows:

- `b139ac77f7761b1cf0c5f429d37371b78ca4a047` durably persists and replays exact
  fenced waiting-for-operator dispositions without reinspection.
- `f386942dbe8982cf8ae20aa89e63610f7f29bc86` distinguishes pre-activation artifact
  rejection from an explicit failed-activation A/B restore transition.
- `18a87bf8995b23159bbeb0cd575bd8d345b76901` adds authenticated periodic lease
  renewal and propagates the shared renewable deadline through running handlers,
  supervised processes, ORAS, workload, and TUF boundaries.
- `b79296ae21ff717a4c38d577d407dbe690b942a5` propagates that same renewable deadline
  through recovered node probes while retaining the independent 15-second probe cap
  and all per-tool, output, and security bounds.

The heartbeat rereview found **3 Important and 1 Minor** issues covering live-handler
deadline propagation, recovered readiness, fatal handling of heartbeat join timeouts,
and scheduling margin. Its boundary follow-up found an additional Important issue in
copied scalar deadlines at ORAS, workload, and TUF execution boundaries. After those
fixes, the next integrated review found one Important node-probe recovery boundary
issue; the `b79296a` correction closed it. Each correction received a clean scoped
rereview. The final exact integrated rereview at `b79296a` found **0 Critical, 0
Important, and 0 Minor** issues and returned **Plan Ready: Yes**.

## Verification and release status

- Controller verification passed **195 agent/probe/recovery/simulator tests** and
  **68 control tests**.
- Pinned Ruff, direct `py_compile`, and exact diff checks passed.
- The literal local broad run is **not green**: it recorded 530 passes plus two known
  PyInstaller crashes/hangs on the shared host. Those two cases are not counted as
  passes or waived into a green result.
- Hosted CI for `b79296a` remains pending and is required before publication
  acceptance. This report makes no hosted-green claim for the final integrated commit.

The runtime plan is therefore review-ready, while release publication remains gated
on hosted CI and the later installed-host and physical-hardware evidence identified by
the task reports.
