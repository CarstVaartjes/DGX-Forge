# Model profile switching

`sparkctl` changes the complete desired state of both DGX Sparks by activating
a named Cluster Profile. It does not start or stop an individual model outside
an accepted profile. The controller runs on the developer machine; endpoints
remain loopback-only on the Sparks and are reached through SSH tunnels until
the separate NAS control plane exists.

## Safety model

Each activation is serialized by `.state/sparkctl/switch.lock` and recorded
atomically in `.state/sparkctl/state.json`. Before any state or remote process
change, the controller resolves a selector or canonical profile ID and runs
admission against the exact profile hash, definition hashes, maturity records,
accepted combination evidence, and current inventory.

The checked-in `agent-full-dual` intent is currently `planned`. Its presence in
the catalog does not make it activatable. Until its adapter, checkpoint
manifest digest, and acceptance evidence have been recorded, activation must
return `blocked` without issuing a remote lifecycle command.

Use a dry run before an accepted transition:

```text
sparkctl switch PROFILE --dry-run
```

A dry run acquires the controller lock and performs resolution and admission.
It never calls a Spark backend command and never saves controller state.

## Transition order

For an admitted activation the controller:

1. checks whether an unchanged workload is eligible for retention;
2. verifies retained workloads are healthy;
3. writes `transitioning` state with no active profile, withdrawing published
   endpoint metadata before stopping changed services;
4. stops changed distributed workloads head first and worker second;
5. runs `verify-release` after every stop sequence;
6. verifies target runtime prerequisites;
7. starts distributed workloads worker first and head second;
8. runs model-identity health checks and the adapter's pinned inference quality
   gate; and
9. atomically publishes only accepted, healthy endpoints with the exact active
   profile and definition fingerprints.

A workload is retained only when the persisted active profile hash still
matches the catalog, its persisted definition hash is unchanged, its placement
and endpoint aliases are identical in the old and new profiles, and its live
health command succeeds. Merely sharing a logical workload ID is insufficient.

For the dual-Spark DeepSeek adapter, the controller appends the role argument
derived from the declared rank order:

```text
spark2: profile-start deepseek-agent-dual worker
spark1: profile-start deepseek-agent-dual head
spark1: profile-stop deepseek-agent-dual head
spark2: profile-stop deepseek-agent-dual worker
```

These commands are executed through the strict key-only SSH backend. OpenSSH
passes the complete POSIX-quoted argv as one command to the remote login shell;
the controller does not interpolate shell syntax, enable SSH agent forwarding,
or assume an always-present gateway.

## Failure behavior

Any start, health, or quality-gate failure withdraws every target endpoint and
stops all target processes that may have started, including a command that
returned failure after creating a process. Cleanup follows each definition's
declared stop order and then verifies resource release. Successful cleanup is
persisted as `stopped`; a failed stop or release check is persisted as
`degraded`. Diagnostics and remote output retained by the report are bounded.

The controller never chooses another profile and never automatically restarts
the previous heavyweight profile. A persisted `transitioning` or `degraded`
state blocks another automatic activation until the operator has inspected the
reported node, workload, operation, and diagnostic detail and performed manual
recovery.

## Explicit restoration

Restoration is request state, not Cluster Profile metadata:

```text
sparkctl switch creative-3d --restore default
```

The selector `default` resolves to canonical profile `agent-full-dual`.
Restoration is a second ordinary, admitted transition after the temporary
profile succeeds and its output provenance has been captured. Its report is
nested separately under `restoration`. The top-level report retains the
temporary producing profile hash and definition hashes even when the final
controller state records the restored profile. A failed restoration is
reported separately and does not rewrite artifact provenance or trigger a
fallback profile.

## Recovery checklist

When status is `degraded` or `transitioning` after interruption:

1. preserve `.state/sparkctl/state.json` and the reported diagnostics;
2. inspect each implicated adapter directly over key-only SSH;
3. stop only the declared workload processes in head-first order where
   distributed;
4. run the matching `verify-release` command on every declared node; and
5. repair controller state only after both Sparks are known to be stopped.

Do not delete model snapshots, output artifacts, runtime caches, or logs as a
switch-recovery shortcut.
