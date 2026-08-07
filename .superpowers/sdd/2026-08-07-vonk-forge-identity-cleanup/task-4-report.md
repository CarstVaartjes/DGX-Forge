# Task 4 report — contracts, Compose, release, and documentation namespaces

Status: COMPLETE

## Delivered

- Migrated owned evidence keys to `vonk_forge`, media types to
  `application/vnd.vonk-forge.*`, schema and SPIFFE identifiers to
  `vonk-forge`, and application settings to `VONK_*` without compatibility
  aliases.
- Renamed the WorkloadRun import source, API, workflow, tests, web page, routes,
  generated Python client, and generated TypeScript contract. Updated bounded
  per-node package projections and release evidence fields consistently.
- Standardized Compose on `vonk-forge-control`, `/srv/vonk-forge`, Vonk runtime
  socket paths, `svc:vonk-forge`, canonical hostnames, and
  `ghcr.io/carstvaartjes/vonk-forge-*` image names.
- Made Hermes an explicit `hermes` Compose profile that is absent from the
  default rendered service set; its setup service remains opt-in as well.
- Replaced the pull-only operator story with a self-contained source-first
  local service-host path: review a commit, build owned images locally, render
  the checked-in graph, and start the default services without claiming the
  later automated NAS implementation exists.
- Regenerated OpenAPI clients, protocol wheel hashes, uv locks, SBOMs, supply
  chain evidence, and Compose image locks. External upstream/vendor/raw
  identities remain only at integration and evidence boundaries.

## Verification

```text
$ npm test --prefix control/web -- --run
63 passed, 1 skipped

$ uv run --project control --frozen --group dev --with-editable . pytest -q \
    control/tests/test_workload_run_importer.py control/tests/test_workload_run_api.py \
    control/tests/test_pki.py control/tests/test_settings.py control/tests/test_oci_bundle.py
76 passed

$ uv run --isolated --project agent_protocol --frozen --with-editable agent_protocol \
    --with pytest --with jsonschema pytest -q agent_protocol/tests/test_workload_packages.py
46 passed

$ uv run --isolated --frozen --with pytest --with pyyaml pytest -q \
    deploy/compose/tests tests/runbooks/test_nas_compose.py \
    tests/cluster_profiles/test_contracts.py
104 passed after correcting the final external-source expectation

$ uv run --isolated --frozen --with pytest pytest -q \
    tests/scripts/test_verify_supply_chain.py
38 passed

$ scripts/verify-supply-chain --generate --json
ok=true; 7 images; 4 SBOMs

$ docker compose --env-file deploy/compose/tests/test.env \
    -f deploy/compose/compose.yaml config --quiet
$ docker compose --env-file deploy/compose/tests/test.env \
    -f deploy/compose/compose.yaml --profile hermes config --quiet
$ git diff --check
all exited 0
```

JSON Schema 2020-12 validation, all changed JSON parsing, and Compose/GitHub YAML
parsing also passed. Running `scripts/generate-control-clients` twice produced
byte-identical diffs.

The identity guard is intentionally still red for later cleanup work: 414
owned matches remain, confined to historical `.superpowers` records and the
adapter, agent vendor-tool, legacy bin/config, inventory evidence, node vendor,
and remaining test areas. Task 4's schemas, control source/generated clients,
Compose, workflows, scripts, README, and user-facing documentation contribute
no remaining owned match.

## Round 1 review remediation — 2026-08-08

The review findings were addressed without retaining the out-of-scope NAS
source-first deployment path or the new Hermes profile behavior. The NAS
runbook again documents the existing verified, digest-pinned release-bundle
and host-updater contract, and Hermes retains its pre-cleanup Compose behavior.

Content-addressed identities were rebuilt rather than copied mechanically:

- DS4 runtime manifest:
  `2234211542a5db7aadba0d1ac65dd0b9513488ee78fc27ba427e8695065660e3`
- Mia runtime manifest:
  `11fa4d36945ed6530daf29f8b4342feaab90ad9cd47fa505cfd9858a358ebf37`
- DS4 release lock:
  `c11e4bce2c20e8047666d5d1c4c87dac164d01f7a5ffcf92452321cb53d65a45`
- Mia release lock:
  `372ac4707f006a7bbba7c49db7577687151cd00ac3f1ad93a5b67addb72bcd5a`

The lock filenames and promoted deployment digests now agree with each lock's
canonical parsed digest. A regression test checks this invariant. The review
also exposed stale definition locks and a stale TripoSG runtime artifact hash
left by the original mechanical rename; those derived identities and the
Vonk-owned maturity fields were regenerated. Upstream repository URLs,
checkpoint IDs, image references, and vendor identifiers were restored and
remain unchanged at external evidence boundaries.

The source-bundle media type migration is complete across schema, producers,
consumers, fixtures, OpenAPI, generated clients, and the web UI:
`application/vnd.vonk-forge.source-bundle.v1+tar`. Supply-chain evidence was
regenerated from current inputs; the manifest digest is
`4fbc574ba350c8a21f84ee7acdec49d09c36e06b6ba4c140449225d167d8d9b4`.

Fresh pre-commit verification:

```text
138 passed — cluster-profile contracts/catalog, Compose, and NAS runbook
48 passed  — workload package and release-lock contracts
73 passed  — DS4 and Mia adapter contracts
38 passed  — supply-chain verifier tests
scripts/verify-supply-chain --json: ok=true; 7 images; 4 SBOMs
git diff --check: exit 0
```

Five control repository-integration tests require the regenerated lock files
to exist in committed `HEAD`; they are rerun after this remediation commit and
their result is recorded in the final handoff.

Post-commit verification made the regenerated repository objects visible:

```text
134 passed — focused control source-bundle, API, workflow, and package-plan tests
63 passed, 1 skipped — control web tests
Compose render with the step-ca overlay: exit 0
```

The repository-wide identity guard remains a later cleanup gate. It reports
835 owned and 10 allowlisted external matches after restoring the historical
evidence required by this review; no claim is made that this task closes that
repository-wide backlog.
