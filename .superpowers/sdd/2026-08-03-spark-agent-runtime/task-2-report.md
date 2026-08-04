# Task 2 report — closed operation registry and typed Spark probes

## TDD evidence

The Task 1 baseline was established before Task 2 changes:

```sh
uv run --project agent pytest agent/tests -q
uv run pytest tests/nodes/test_collect_health.py -q
```

Result: 113 agent tests and 24 existing collector tests passed.

The implementation was developed through separate RED/GREEN slices:

- Registry RED: `test_operations.py` failed collection with
  `ModuleNotFoundError: No module named 'dgx_agent.operations'`. The first
  registry slice then passed 2 tests before replay, inspection, fencing, and
  redacted-failure cases were added.
- NVIDIA boundary RED: `test_nvidia_tools.py` failed collection with
  `ModuleNotFoundError: No module named 'dgx_agent.nvidia_tools'`. Strict policy
  parsing, descriptor verification, exact reviewed metadata, and per-tool
  normalizers were then implemented incrementally.
- Probe RED: `test_probe.py` failed collection with
  `ModuleNotFoundError: No module named 'dgx_agent.probe'`. Fixed invocation,
  bounded process capture, total deadline/output limits, process-group cleanup,
  and collector/tool adapters were then added incrementally.
- Security-review RED: repeated rejection of a mode-unsafe bundle directory
  increased `/proc/self/fd` from 12 to 22. The directory verifier was corrected
  to close its descriptor on every success and error path; the regression and
  adjacent owner/type tests then passed 2/2.

The final focused suite contains 85 tests. Test fixtures construct immutable
policy values with fixture-local hashes only after proving that production
`InstalledPolicy.load` accepts exclusively the compiled reviewed contract.
Neither production code nor tests download, extract, or depend on a bundle
archive.

## Exact installed-policy contract

The installed policy is schema version 1. Its root object has exactly these
fields (missing, extra, or duplicate fields are rejected):

```json
{
  "schema_version": 1,
  "bundle_version": "0.1.0",
  "bundle_sha256": "0eb1c93dd839b6bd4136cc8b79ea04a1e44fd637ff6afa6ee9568951a4c179f3",
  "bundle_root": "/absolute/canonical/path",
  "tools": [],
  "support_files": [],
  "health": {}
}
```

Each `tools` entry has exactly `name`, `version`, `executable`, `sha256`,
`arguments`, `timeout_seconds`, and `output_limit_bytes`. The complete ordered
tool set is:

| name | version | bundle-relative executable | SHA-256 | exact arguments |
| --- | --- | --- | --- | --- |
| `device_identity` | `1.1.0` | `bin/device_identity.py` | `110acb65e54092a63d93f8d0448855717323c7251bbaf661a7d6cb41836f2dcf` | `--stdout-json --no-write-file --quiet` |
| `hardware_config` | `1.0.0` | `bin/hardware_config.py` | `07c05c03f65e9b707bc18ebd2ec010ac1622701fa0b87858014a5b71fd1af5bb` | `--stdout-json --no-write-file --quiet` |
| `firmware_reporter` | `1.0.0` | `bin/firmware_reporter.py` | `c5887cb8b456295ea937a44cf05d8c1a3fa64b2ac8239f35be61e8deb358d387` | `--stdout-json --no-write-file --quiet` |
| `os_build_identity` | `1.0.0` | `bin/os_build_identity.py` | `ee2f06d7ae25438ed0a7258eeeecdde76dba24c5c82f9dec510c361b9d75f6f9` | `--stdout-json --no-write-file --quiet` |
| `driver_inventory_reporter` | `1.0.0` | `bin/driver_inventory_reporter.py` | `f5f90c05f077f1cd6fa387d1f6eac3b7f40b7d859c6e5886c73ec03629fdfc26` | `--stdout-json --no-write-file --quiet` |
| `spark_diagctl_health` | `1.1.0` | `bin/spark_diagctl.py` | `03de23664d3a24295ce605075be957328f47c24fa37afb7bbfe60988cbee42c2` | `--stdout-json --no-write-file --quiet health` |
| `reset_reason_reporter` | `1.1.0` | `bin/reset_reason_reporter.py` | `212b49f894e4703cc85743217a0a9d9f2bb5891702266df84b907df960d83774` | `--stdout-json --no-write-file --quiet` |

The only permitted imported support files are represented by objects with
exactly `relative_path`, `sha256`, and `size_bytes`:

| path | bytes | SHA-256 |
| --- | ---: | --- |
| `bin/common/asset_id.py` | 8072 | `35277c9d42c97960434f10e7f8dfda0a7e12cfbe00aec0d86ea88099c5ac9eca` |
| `bin/common/cli_base.py` | 15147 | `0b1f72a2056cbb5a3c717e7853b7f4d986a4b91b7920eadab68888b101f1b1da` |
| `bin/common/output.py` | 9200 | `6938255c277aa5b3b2e805a2cbfdc52d86c5d19910591cb42272a7eb280e2426` |
| `bin/common/__init__.py` | 754 | `a3b4329f7500a2f9d95369ba32b3eb563c27a76d6d96d9f98dac1c1fc41b938a` |

The `health` object has exactly `executable`, `sha256`, `cpu_sample_ms`,
`fabric_pairs`, `timeout_seconds`, and `output_limit_bytes`. Each fabric pair
has exactly `interface` and `hca`. The adapter derives the collector argv as
`--json --cpu-sample-ms <value>` followed by each explicit
`--interface <value> --hca <value>` pair; claims cannot alter any value.

## File inventory

- `agent/src/dgx_agent/operations.py`: frozen operation context and immutable
  inspection/execution records; source-closed `NODE_PROBE` dispatch; exact
  fenced replay and stable failure results.
- `agent/src/dgx_agent/nvidia_tools.py`: immutable installed-policy model,
  compiled reviewed artifact lock, descriptor-safe integrity checks, and seven
  strict allowlist normalizers.
- `agent/src/dgx_agent/probe.py`: fixed-policy orchestration, descriptor-bound
  bounded process runner, collector compatibility normalization, and canonical
  output limits.
- `agent/tests/test_operations.py`, `agent/tests/test_nvidia_tools.py`, and
  `agent/tests/test_probe.py`: registry, policy, evidence, process, deadline,
  cleanup, replay, and attack coverage.

`nodes/bin/collect-health` was not changed; its existing explicit interface is
adapted and normalized by `probe.py`.

## Security review

- Dispatch accepts only the exact protocol `AgentClaim` type and the compiled
  `NODE_PROBE` enum member with an empty payload. No plugin, import, callable,
  command, path, environment, tool selector, or timeout is claim-controlled.
- Policy and artifact paths are opened component-by-component without symlink
  following. Policy reads are bounded and require regular trusted files.
  Present unsafe owners, modes, file types, sizes, hashes, or support-directory
  members are hard typed failures; absent bundles/tools are capabilities marked
  unavailable and never trigger PATH fallback.
- Every executable is opened, metadata-checked, size-checked, hashed, rewound,
  and retained before the first child starts. Linux executes the retained
  `/proc/self/fd/<n>` object, closing the pathname swap race. Imported Python
  support is the exact reviewed `bin/common` set, with user site and bytecode
  writes disabled.
- Child calls use exact argv, a fixed cwd/environment, closed stdin, no shell,
  closed descriptors except the verified executable, and a new process group.
  Stdout/stderr are incrementally bounded. Timeout, flood, and successful
  daemon cases terminate and reap descendants.
- The total probe deadline includes integrity verification and is bounded by
  both 15 seconds and the claim deadline. Per-process, 256-KiB aggregate raw,
  and 64-KiB canonical result ceilings are enforced independently.
- Collector documents are duplicate-safe bounded UTF-8 JSON. Each source has
  a schema-specific allowlist; recursive freezing gives deterministic output.
  Unknown fields and error/meta text are discarded. UUID/GUID, MAC, IPv4/IPv6,
  and slash/backslash path shapes are rejected even in otherwise allowlisted
  string fields. Boot IDs are intentionally omitted because their UUID shape
  conflicts with the explicit no-UUID evidence requirement.
- Only known internal typed errors can select a persisted stable error code.
  Exception messages, paths, stdout, stderr, and collector error strings never
  enter the canonical `AgentResult`.

## Verification

```sh
uv run --project agent pytest agent/tests/test_operations.py agent/tests/test_probe.py agent/tests/test_nvidia_tools.py -v
```

Result: 85 passed in 0.50s.

```sh
uv run pytest tests/nodes/test_collect_health.py -q
uv run --project agent pytest agent/tests -q
uv run --project agent python -m compileall -q agent/src
git diff --check
```

Results: 24 collector tests passed in 2.77s; 198 agent tests passed in 5.36s;
bytecode compilation and whitespace validation exited successfully with no
output.

## Remaining concerns

- Task 5 must install the exact TUF/OCI-authorized artifacts and materialize
  this policy contract with privileged ownership and safe modes. Task 2 does
  not install, fetch, or update those artifacts.
- Tests validate the real Linux process boundary with purpose-built local
  executables and validate reviewed NVIDIA documents through fixtures/fakes;
  on-node ARM64 execution of the installed reviewed bundle remains a Task 5
  integration responsibility.
- Descriptor execution, process groups, procfs, and subreaper cleanup are
  intentionally Linux-specific, matching the target DGX Spark runtime.
- Release/workload handlers, network transport, supervision, and installation
  remain out of scope for Tasks 3–6.
