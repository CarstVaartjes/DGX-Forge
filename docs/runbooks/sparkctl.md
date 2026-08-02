# `sparkctl` developer controller

`sparkctl` is the repository-local, developer-machine interface to the
content-addressed Spark profile catalog. It reads controller state from
`.state/sparkctl`, resolves friendly selectors to canonical Cluster Profile
IDs, and delegates admitted transitions to the profile switcher.

Run it from any directory with the repository launcher. During development,
`uv` can provide the one runtime dependency without changing the project lock:

```bash
uv run --no-project --with jsonschema -- /path/to/spark/bin/sparkctl status
uv run --no-project --with jsonschema -- /path/to/spark/bin/sparkctl status --json
```

`--json` may be placed before the command or on the command itself. Human
output is the default. `status --json` is the stable local interface intended
for the future NAS controller. It is a persisted snapshot and deliberately
reports no live published endpoints. `nodes status --json` performs a fresh,
read-only SSH probe of both Sparks; it is not persisted in controller state.

## Commands

```text
sparkctl catalog [--json]
sparkctl validate PROFILE_OR_SELECTOR [--json]
sparkctl status [--json]
sparkctl nodes status [--json]
sparkctl prepare PROFILE_OR_SELECTOR [--json]
sparkctl switch PROFILE_OR_SELECTOR [--restore PROFILE_OR_SELECTOR] [--dry-run] [--json]
sparkctl restore-default [--dry-run] [--json]
sparkctl endpoint ENDPOINT_ALIAS [--json]
sparkctl break-stale-lock [--json]
```

- `catalog` lists profiles, workload definitions, content hashes, maturity, and
  selector mappings. Planned profiles remain visible.
- `validate` resolves the selector, confirms the checked-in contracts loaded,
  collects live health and capacity from both Sparks, and runs admission.
  `valid: true` does not imply `admitted: true`.
- `status` reads only local controller state. It makes no SSH call. Endpoint
  availability is always fail-closed as `published_endpoints: {}` because a
  persisted snapshot cannot establish that either Spark is still alive.
- `nodes status` concurrently probes both configured Sparks and reports live
  host, NVIDIA, thermal, and direct-fabric health without retaining history or
  changing either node or the active profile. See
  [Live node health](node-health.md).
- `prepare` resolves a selector, acquires the same controller lock used by
  transitions, and requires a clean `stopped` state with no active profile or
  transitional target. It invokes each workload's declared `prepare` command
  concurrently on all of that workload's nodes, with the definition's
  operation-specific deadline applied independently to every node. Preparation
  does not run admission, change controller state, publish an endpoint, or
  activate a profile.
- `switch` resolves selectors before invoking the ordinary switch path.
  `--dry-run` reports only the truthful status, hashes, and restore intent
  exposed by the switcher; the CLI does not maintain a second action planner.
- `--restore` records a canonical restoration intent. It never restores during
  the same switch call.
- `restore-default` is a later, explicit ordinary switch to selector `default`.
  Run it only after outputs and provenance from temporary work are recovered.
- `endpoint` returns an address only for an alias published by an active,
  currently accepted profile after a fresh health probe confirms both Sparks
  are reachable and their exact boot IDs still match the successful
  activation. It holds the transition lock and runs the workload adapter's
  read-only health check before returning the address. Stopped, stale,
  rebooted, unhealthy, planned, dead, or unpublished endpoints are denied.
- `break-stale-lock` uses the state-store safety checks. It refuses a held lock,
  a live local PID, a lock written by a different controller host, or a lock
  younger than the configured threshold. A foreign-host record must be
  inspected and recovered on that host; age alone never authorizes removal.

Operational `validate`, `switch`, `restore-default`, and `endpoint` commands
use the same live health collector as `nodes status`. Health is projected into
the bounded admission inventory: node health, available memory, root-disk
space, and boot ID. A missing or failed probe blocks admission or publication;
it never falls back to stale local measurements. The checked-in
`agent-full-dual` profile resolves correctly but remains unactivatable while
`deepseek-agent-dual` has `verified` maturity. Its direct Mia runtime is
operational, but profile admission remains fail-closed until the definition is
`accepted`.

## Durable preparation

Run preparation only after deploying the exact digest-qualified runtime
release and while `sparkctl status` reports a clean stopped state:

```bash
uv run --no-project --with jsonschema -- \
  bin/sparkctl prepare default --json
```

The adapter owns the durable node-local preparation job. For each workload, the
controller submits Spark 2 with role `worker` and Spark 1 with role `head`
concurrently, using the declared 86,400-second deadline independently for each
call. It reports every workload/node result, role, timeout, return code, and
bounded diagnostic independently, in the definition's deterministic
`start_order` even when the calls finish in a different order. A timeout or
failure on one Spark does not prevent the other Spark from starting or being
collected.

Worker-first and head-first ordering applies to runtime startup and shutdown,
not artifact preparation. Both Sparks must download and prepare in parallel.

A client-side timeout returns status `in-progress`, `resumable: true`, and exit
code `8`. It does not issue `stop`, kill the remote job, write controller
state, or change the active profile. Re-run the same command to reattach to the
deterministic preparation job. A nonzero adapter result is `failed` and exit
code `6`; a non-clean controller state is `blocked` and exit code `3`.

Preparation starting or finishing does not advance Model Definition maturity.
The separate prepared, verified, and accepted evidence gates remain required.

## Remote container prerequisite

Profile transitions must start and stop containers noninteractively. On each
dedicated Spark, the trusted `carst` administrator therefore belongs to the
`docker` group. This is root-equivalent access and must not be extended to
untrusted accounts.

After adding the group membership, close the SSH session and reconnect before
testing because an existing login retains its original supplementary groups:

```bash
sudo usermod -aG docker carst
exit
ssh dgx-spark-1  # or dgx-spark-2
id -nG
docker version --format '{{.Server.Version}}'
```

The group list must include `docker`, and the server query must succeed. For
live-collector failures, continue with the Docker-specific troubleshooting in
[Live node health](node-health.md).

## Exit codes

| Code | Meaning |
| ---: | --- |
| `0` | Successful read, admitted dry-run, or completed transition |
| `2` | Invalid arguments, selector, catalog, or controller configuration |
| `3` | Admission blocked or endpoint unavailable |
| `4` | `nodes status`: at least one node is `critical` or `unreachable` |
| `5` | Local health collector, schema, inventory, or baseline failure before probing |
| `6` | Transition or explicit restoration failed |
| `7` | Switch-lock conflict or unsafe stale-lock override |
| `8` | Durable preparation is still running after the client deadline; rerun to resume |

CLI errors and switch diagnostics are bounded and redact common credential,
authorization, token, password, secret, and private-key forms. Do not place
credentials in profile files, command arguments, or remote diagnostic output.
Argument failures that include a sensitive option use generic error text so a
whitespace-separated option value cannot be echoed by the parser.

## Safe bring-up checks

These commands do not mutate either Spark node. `catalog` and `status` are
local; `validate` and `nodes status` perform live read-only probes:

```bash
uv run --no-project --with jsonschema -- bin/sparkctl catalog --json
uv run --no-project --with jsonschema -- bin/sparkctl validate default --json
uv run --no-project --with jsonschema -- bin/sparkctl status --json
uv run --no-project --with jsonschema -- bin/sparkctl nodes status --json
```

At the current milestone, `catalog` succeeds, `validate default` exits `3` with
the verified-not-accepted maturity denial, and `status` reports `stopped` when
no local state has been written. On 2026-08-02, `nodes status --json` exited `0` with both
nodes healthy, Docker available, and no warnings or errors. It exits `4` if a
later probe finds either node critical or unreachable. The controller/profile
framework and live health are implemented. The pinned Mia DeepSeek runtime is
installed, running and quality-verified, but is not yet accepted. Performance
fine-tuning plus sustained thermal, repeated lifecycle and reboot gates are
deferred to the final cross-model optimization phase.
