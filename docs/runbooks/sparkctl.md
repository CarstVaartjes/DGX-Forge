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
  activation. Stopped, stale, rebooted, unhealthy, planned, or unpublished
  endpoints are denied.
- `break-stale-lock` uses the state-store safety checks. It refuses a held lock,
  a live local PID, or a lock younger than the configured threshold.

Operational `validate`, `switch`, `restore-default`, and `endpoint` commands
use the same live health collector as `nodes status`. Health is projected into
the bounded admission inventory: node health, available memory, root-disk
space, and boot ID. A missing or failed probe blocks admission or publication;
it never falls back to stale local measurements. The checked-in
`agent-full-dual` profile resolves correctly but remains unactivatable while
`deepseek-agent-dual` has `planned` maturity.

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
the planned-maturity denial, and `status` reports `stopped` when no local state
has been written. `nodes status` exits `4` if either node is critical or
unreachable.
