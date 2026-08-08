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

## Round 2 review remediation — 2026-08-08

Restored the NVIDIA-owned provenance at the documentation evidence boundary:
the operator fabric runbook, installation record, and historical 2026-08-01
plan/spec now identify the pinned `dgx-spark-playbooks` source. The fabric
runbook also names the upstream `discover-sparks` helper exactly. Vonk-owned
service names, paths, aliases, and commands remain canonical elsewhere.

Repaired the authoritative identity-cleanup plan with its actual legacy to
canonical mapping (`spark_profiles` to `cluster_profiles`, `sparkctl` to
`vonkctl`, `dgx_*` to `vonk_*`, and the corresponding service/settings and
user-facing contract mappings). The plan now distinguishes forbidden
Vonk-owned legacy tokens from preserved, explicitly labeled NVIDIA/upstream
and raw-evidence identifiers.

Reverted unrelated Grafana and Tailscale default image upgrades to their
audited versions and digests in both Compose declarations and the image lock.
The supply-chain manifest was regenerated because it cryptographically binds
the image lock. Focused tests now assert the restored upstream names and that
the default Grafana and Tailscale declarations match the audited lock.

Fresh verification:

```text
bash tests/runbooks/test_fabric_safety.sh
fabric runbook safety invariants: PASS

uv run --isolated --frozen --with pytest --with pyyaml pytest -q \
  deploy/compose/tests/test_tailscale.py tests/runbooks/test_nas_compose.py
14 passed

scripts/verify-supply-chain --generate --json
ok=true; 7 images; 4 SBOMs

uv run --isolated --frozen --with pytest pytest -q \
  tests/scripts/test_verify_supply_chain.py
38 passed

docker compose --env-file deploy/compose/tests/test.env \
  -f deploy/compose/compose.yaml config --quiet
exit 0

git diff --check
exit 0
```

## Round 3 review remediation — 2026-08-08

Restored the remaining external provenance that the mechanical identity rename
had rewritten inside upstream names and immutable values. The Mia design now
matches the executable workload, adapter, inventory evidence, and pre-cleanup
record exactly:

- `MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark`
- `ghcr.io/anemll/dspark-vllm-gx10`
- speculative API method `dspark`

The DS4 installation record and completed implementation plan again use the
drafter repository, filename, and runtime setting from the checked manifest and
Compose contract: `bleysg/DeepSeek-V4-Flash-DSpark-drafter-GGUF`,
`DSpark-drafter-Q2K-Q8-0731.gguf`, and `DS4_CONT_DSPARK=1`.

The multi-runtime research record now preserves the actual external candidate
identities exposed by its URLs, including `dgx-trellis2` and
`Trellis2-DGX-Spark-Docker`. The same audit corrected adjacent NVIDIA,
DS4-on-Spark, and community DGX Spark labels where the cleanup had falsely
presented upstream/vendor resources as Vonk products. Vonk-owned node IDs,
services, controller commands, placement language, and package contracts remain
canonical.

No new prose-only regression test was added: it would freeze human wording
rather than exercise behavior. The existing adapter and contract suites already
validate the executable source, image, manifest, DS4 environment, and artifact
identities; a focused comparison against those executable contracts was run in
this remediation.

Fresh verification:

```text
113 passed — documentation contracts plus DS4 and Mia adapter contracts
14 passed  — focused Compose and NAS runbook tests
38 passed  — supply-chain verifier tests
bash tests/runbooks/test_fabric_safety.sh: PASS
external provenance comparison against manifests/Compose: PASS
top-level Compose render: exit 0
DS4 Compose render: exit 0
scripts/verify-supply-chain --json: ok=true; 7 images; 4 SBOMs
git diff --check: exit 0
```

## Round 4 review remediation — 2026-08-08

Restored the remaining upstream and NVIDIA product names identified by review
without changing Vonk-owned node, service, command, path, or contract names.
The three historical Mia/runtime documents now use the executable image name
`ghcr.io/anemll/dspark-vllm-gx10`. Model overviews preserve the DSpark drafter
name and the exact `dgx-trellis2` and `Trellis2-DGX-Spark-Docker` repository
names exposed by their URLs.

The NVIDIA documentation references again identify DGX Spark, DGX Dashboard,
Enterprise Manageability, clustering, update, recovery, and networking guides
as NVIDIA-owned resources. An adjacent-doc scan also corrected the same
mechanical rewrite in closely related model summaries and historical plans;
Vonk-owned placement and runtime terminology remains canonical.

A focused provenance regression test now binds the immutable Mia image and the
external DSpark, TRELLIS.2, and NVIDIA link labels. Its initial red run failed
all three checks against the rewritten names, then passed after the corrections.
The existing model-overview contract assertion was updated from the falsified
generic drafter label to DSpark.

Fresh verification:

```text
103 passed — provenance docs plus DS4/Mia adapter and cluster contract tests
bash tests/runbooks/test_fabric_safety.sh: PASS
14 passed — focused Compose and NAS runbook tests
38 passed — supply-chain verifier tests
scripts/verify-supply-chain --json: ok=true; 7 images; 4 SBOMs
top-level Compose render: exit 0
git diff --check: exit 0
```
