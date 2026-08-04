# Existing Components and Build-vs-Buy Review

**Date:** 2026-08-03

**Scope:** The generic Docker-hosted DGX-Forge control plane, one-to-N Spark
installation, outbound agent, Git-backed administration, observability, and
platform updates.

## Outcome

DGX-Forge still needs a small amount of domain-specific software: the
Git-to-fleet reconciler, fenced node-operation state machine, immutable
Spark identity/evidence approval, placement logic, and the CLI/web workflows
that operate those concepts. Those boundaries do not exist as an off-the-shelf
product.

Several infrastructure concerns should not be implemented locally. Adopt the
maintained components below and keep each as a separate service or pinned host
tool. The NAS remains a generic Docker-capable service host; no decision depends
on UGREEN-specific APIs or a fixed fleet size.

## Decisions

| Concern | Existing component | Decision | DGX-Forge boundary |
|---|---|---|---|
| Machine PKI | Smallstep `step-ca` | Adopt as the recommended production CA; retain the built-in issuer as a zero-dependency bootstrap/development provider | DGX-Forge owns enrollment grants, physical evidence approval, node inventory, certificate-to-node binding, and audit. `step-ca` owns issuance, renewal, revocation, CA key lifecycle, and policy. |
| General secret management | HashiCorp Vault | Do not require for the small-cluster default | Vault is a good provider when an operator already runs it, but adds sealing, storage, policy, token, backup, and HA operations beyond this project's PKI-only need. Keep the CA interface open for a future Vault provider. |
| Workload identity | SPIRE | Do not adopt now | SPIRE's workload/node attestation is valuable at larger or heterogeneous scale, but it adds server and per-node agent machinery. Revisit when DGX Spark has a trustworthy TPM/device-attestation path or workload-level SVIDs become a requirement. |
| Release/artifact storage | CNCF Distribution registry plus ORAS | Adopt | OCI stores immutable blobs/manifests. Agents pull exact digest references with a pinned ORAS client. Keep the control API artifact route only for bounded bootstrap/recovery artifacts, not normal release distribution. |
| Update trust and rollback protection | The Update Framework (TUF) | Adopt | TUF authorizes versions and digests and protects against rollback/freeze/mix-and-match attacks. OCI transports the bytes; TUF metadata determines whether they are installable. |
| Host installation/hardening | Ansible roles plus Ansible Runner | Adopt | Bootstrap may use SSH once. It transfers/runs a pinned local role bundle; after enrollment, typed agent operations invoke the same local Runner boundary. Do not run an autonomous `ansible-pull` loop that bypasses DGX-Forge jobs and audit. |
| DGX Spark lifecycle collectors | NVIDIA Enterprise Manageability scripts | Adopt behind a pinned, tested adapter | Install the exact MIT-licensed bundle as a DGX-Forge release target. NVIDIA supplies reference implementations for device, hardware, firmware, OS, driver, software, diagnostic, reset-reason, and reboot/kernel-rollback collection. DGX-Forge owns bounded invocation, normalization, redaction, evidence retention, and the job fence. Keep the existing collector only for DGX-Forge-specific fabric/runtime fields and explicit compatibility fallback. |
| Fresh or replacement-node provisioning | NVIDIA DGX Spark cloud-init/OEMDATA workflow | Adopt as an installation-mode option | Use NVIDIA's BaseOS/FastOS customization and OEMDATA contracts for fresh/reimaged devices. `spark-install` supplies a versioned DGX-Forge seed/policy and resumes at physical identity approval; already-running devices retain the one-time SSH bootstrap path. DGX-Forge does not fork the NVIDIA installer or embed fixed users, names, or addresses. |
| Ubuntu fleet management | Canonical Landscape | Optional provider, not a default service | NVIDIA recommends Landscape for enterprise DGX Spark fleets, but DGX-Forge's small-cluster default already needs its domain agent and control plane. Operators that already run Landscape may use it for Ubuntu policy/package channels; it never becomes model/profile authority or a second routine mutation path. |
| Spark cabling/bootstrap | NVIDIA Sync Cluster Assistant | Optional operator aid | It can validate and configure supported small direct/switch topologies, but its documented device limits and SSH setup do not define DGX-Forge inventory, identity, topology, or normal control. Import only reviewed evidence; never infer a fleet-size limit from it. |
| Durable operation delivery | PostgreSQL queue with `SKIP LOCKED` | Keep current design | NATS JetStream and Temporal still provide at-least-once execution and require additional services. They do not remove DGX-Forge's need for node fences, mutation inspection, compensation, and operator-wait states. PostgreSQL is appropriate for this small fleet. |
| Python/TypeScript API clients | `openapi-python-client`, `openapi-typescript`, and `openapi-fetch` | Adopt | FastAPI OpenAPI is the contract. Generated clients/types are checked for drift in CI. Thin DGX wrappers retain safe polling, request-ID reuse, and typed domain errors. |
| Host/GPU telemetry | Prometheus node exporter, NVIDIA DCGM exporter, and Grafana Alloy | Adopt | Exporters bind to loopback on each Spark. Alloy scrapes locally and sends mTLS `remote_write` outbound through Caddy to the NAS. The Spark opens no listener to the LAN and the DGX agent does not reimplement exporter metrics. |
| Dashboards and alerts | Grafana and Prometheus | Adopt; already planned | Grafana owns charts, dashboard variables, and alert visualization. DGX-Forge web shows operational state/actions and links to the relevant provisioned dashboard instead of rebuilding Grafana. |
| Inference gateway administration | LiteLLM Admin UI | Reuse for gateway-specific administration | LiteLLM owns virtual keys, teams, spend, request logs, and gateway status. Dynamic model authority remains disabled: DGX-Forge Git repositories own model/profile/placement definitions and publish generated LiteLLM routes. DGX-Forge admin links to the scoped LiteLLM UI rather than duplicating it. |
| Container-host administration | Portainer Edge Agent | Optional for the NAS only | Portainer can help an operator inspect Docker services. It does not replace the Spark agent or Git reconciler and must not receive Spark control authority or become a required dependency. |
| Agent A/B activation | systemd service plus DGX-Forge supervisor | Keep current design | `systemd-sysupdate` is aimed at image/resource updates and is not a portable replacement for the agent's application-level reconnect/readiness/fence rollback contract. Revisit for full OS-image management. |

## Why these choices fit the target scale

The likely deployment has one Docker service host, one PostgreSQL database,
and a small number of Sparks and administrators. A separate message broker,
workflow cluster, service mesh, workload-identity server, or mandatory Vault
cluster would increase failure modes more than it reduces DGX-Forge code.

Conversely, PKI, software-update trust, artifact distribution, configuration
application, and GPU telemetry are security-sensitive standards-heavy areas.
Using established components there removes meaningful custom protocol and
parser work without turning the product into a distributed-systems platform.

The NVIDIA lifecycle bundle reviewed on 2026-08-04 is the official
`enterprise-lifecycle-integration-scripts-20260520-1602.zip` download, whose
SHA-256 is
`0eb1c93dd839b6bd4136cc8b79ea04a1e44fd637ff6afa6ee9568951a4c179f3`.
Its package identifies the tools as version `0.1.0` and includes NVIDIA's MIT
license. That review digest is evidence, not a floating dependency: release
engineering must mirror the exact bytes into the OCI/TUF path and may advance
the lock only through a reviewed repository change.

The resulting deployment is a composed system, not a monolith:

```text
Caddy
  -> DGX control API / worker / web
  -> LiteLLM
  -> Grafana
  -> agent mTLS and metrics ingress

PostgreSQL        step-ca          OCI registry
Prometheus        Grafana          LiteLLM

Spark (outbound only)
  -> DGX agent -> control API
  -> pinned NVIDIA lifecycle tools behind fixed typed operations
  -> ORAS -> OCI registry
  -> Alloy -> Prometheus remote_write through Caddy
  -> node_exporter + DCGM exporter on loopback
```

## Authority boundaries

These are deliberately non-overlapping:

- Git is the only authority for fleet, topology, model, profile, release, and
  desired deployment documents.
- PostgreSQL is operational state: jobs, fences, observations, enrollment,
  reconciliation, audit, and rollout progress.
- TUF metadata is release trust authority; OCI is only content transport.
- `step-ca` is certificate authority; DGX-Forge decides which physical node may
  enroll and records the node/certificate relationship.
- LiteLLM is inference traffic policy and accounting, not model repository
  authority.
- Grafana is visualization, not cluster mutation authority.
- Ansible roles describe idempotent local host policy, but every mutation is
  initiated and journaled by installation mode, recovery mode, or a fenced
  agent operation.
- NVIDIA lifecycle tools are node-local implementation dependencies, not a
  transport or authority. Their JSON is untrusted bounded input until the
  DGX-Forge adapter validates, normalizes, and redacts it.
- Landscape and NVIDIA Sync are optional external operator tools. Their state
  cannot make a node eligible, change repository desired state, or bypass a
  fenced operation.

## Plan impact

The existing task count does not increase. The work is folded into the task
that already owns each boundary:

1. PKI Tasks 4-5 add the separate `step-ca` service/provider and keep the
   built-in provider for bootstrap/development.
2. Agent Runtime Task 3 uses OCI/ORAS for normal content-addressed releases;
   the already implemented API artifact endpoint becomes bootstrap/recovery
   only.
3. Unified Admin Tasks 1-4 generate both clients from OpenAPI and add links to
   LiteLLM/Grafana for their native administration surfaces.
4. Platform Update Tasks 1-3 replace custom signed-manifest trust with TUF
   roles/metadata while retaining the DGX compatibility manifest as a TUF
   target. OCI references transport target blobs.
5. Installation/onboarding policy steps become versioned Ansible roles invoked
   through Ansible Runner, with SSH limited to first bootstrap and explicit
   recovery.
6. Observability installs node exporter, DCGM exporter, and Alloy per Spark;
   control-plane metrics remain native because they describe DGX-specific
   operational state.
7. Agent Runtime Tasks 2 and 5 add a fixed-path adapter and install a pinned
   NVIDIA Enterprise Manageability bundle. Routine probes combine its safe
   platform evidence with the existing DGX-Forge fabric/runtime fields;
   diagnostic/log modes remain explicit, bounded, and redacted.
8. Agent Migration Tasks 1, 3, 5, and 6 add fresh/reimage cloud-init mode,
   record NVIDIA-tool provenance, and distinguish DGX OS maintenance from
   DGX-Forge application releases. Existing-node SSH bootstrap remains a
   one-time compatibility path.
9. Platform Update Tasks retain TUF/OCI for DGX-Forge. NVIDIA
   `spark_updatectl.py` contributes reboot readiness, next-boot kernel, and
   rollback evidence only; it does not replace NAS generations or agent A/B
   fan-out.

## Primary references

- [Smallstep step-ca overview](https://smallstep.com/docs/step-ca/) and
  [provisioners](https://smallstep.com/docs/step-ca/provisioners/)
- [Vault PKI secrets engine](https://developer.hashicorp.com/vault/docs/secrets/pki)
- [SPIRE concepts](https://spiffe.io/docs/latest/spire-about/spire-concepts/)
- [OCI Distribution Specification](https://github.com/opencontainers/distribution-spec/blob/main/spec.md),
  [CNCF Distribution deployment](https://distribution.github.io/distribution/about/deploying/),
  and [ORAS pull](https://oras.land/docs/commands/oras_pull/)
- [The Update Framework specification](https://theupdateframework.github.io/specification/latest/)
- [Ansible Runner](https://docs.ansible.com/projects/runner/en/stable/index.html)
  and [ansible-pull](https://docs.ansible.com/projects/ansible-core/devel/cli/ansible-pull.html)
- [NVIDIA DGX Spark Enterprise Manageability](https://docs.nvidia.com/dgx/dgx-spark/enterprise-manageability.html),
  [Enterprise Lifecycle Integration](https://docs.nvidia.com/dgx/dgx-spark/enterprise-fleet-lifecycle.html),
  and [custom installation with cloud-init](https://docs.nvidia.com/dgx/dgx-spark/enterprise-custom-install.html)
- [DGX Spark clustering and Cluster Assistant boundaries](https://docs.nvidia.com/dgx/dgx-spark/spark-clustering.html)
- [Canonical Landscape self-hosted deployment](https://documentation.ubuntu.com/landscape/explanation/landscape/self-hosted-landscape/)
- [PostgreSQL `SKIP LOCKED`](https://www.postgresql.org/docs/current/sql-select.html),
  [NATS JetStream consumers](https://docs.nats.io/nats-concepts/jetstream/consumers),
  and [Temporal durable execution](https://docs.temporal.io/)
- [openapi-python-client](https://github.com/openapi-generators/openapi-python-client),
  [openapi-typescript](https://openapi-ts.dev/introduction), and
  [openapi-fetch](https://openapi-ts.dev/openapi-fetch/)
- [NVIDIA DCGM exporter](https://docs.nvidia.com/datacenter/dcgm/latest/installation/install-dcgm-exporter.html),
  [Prometheus node exporter](https://prometheus.io/docs/guides/node-exporter/), and
  [Grafana Alloy remote write](https://grafana.com/docs/alloy/latest/reference/components/prometheus/prometheus.remote_write/)
- [Grafana provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/)
  and [LiteLLM Admin UI](https://docs.litellm.ai/docs/proxy/ui)
- [Portainer Edge Agent](https://docs.portainer.io/admin/environments/add/docker/edge)
  and [systemd-sysupdate](https://man7.org/linux/man-pages/man8/systemd-sysupdate.8.html)
