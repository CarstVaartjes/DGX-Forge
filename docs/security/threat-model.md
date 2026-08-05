# DGX Forge platform threat model

## Security objectives

The platform must not publish an unhealthy model, execute repository-selected
code, confuse a mutable address with physical node identity, disclose prompts or
credentials, or permit an unreviewed repository state to reach the cluster.
Git is desired-state authority. PostgreSQL is operational state only.

## Trust boundaries

| Boundary | Assets and attacker | Prevention | Detection and recovery | Executable evidence |
|---|---|---|---|---|
| Tailnet human ingress | Sessions, inference keys; unauthorized tailnet or LAN client | Tagged userspace Tailscale gateway, separately granted exact named Services, no human LAN listener, private gateway-to-Caddy network, Caddy body/header and API auth controls | Tailnet policy tests and bounded API logs; revoke user, gateway node, tag, or Service and withdraw routes | `deploy/compose/tests/test_tailscale.py`, `test_networking.py`, `control/tests/test_api.py` |
| Restricted Spark LAN ingress | Enrollment grant, agent identity, registry artifacts; hostile LAN client | One NAS-IP-bound backend port, firewall management-CIDR restriction, route-minimal SNI split, mTLS for agent/registry, no human routes | Caddy/control audit, certificate revocation, enrollment expiry, firewall review | `deploy/compose/tests/test_agent_ingress.py`, `control/tests/security/test_agent_identity.py` |
| Admin browser | Proposal intent; malicious site or compromised browser | Same-origin API only, SameSite/HttpOnly session, double-submit CSRF, typed forms, explicit diff confirmation | Audit request/actor/base/targets; revoke session | web unit/Playwright tests, authorization matrix |
| CLI token | Administrator capability; local unprivileged user | HTTPS origin validation, regular non-symlink token file, bounded JSON, no token in argv/output | API audit and token rotation | `tests/spark_profiles/test_control_client.py` |
| Git/code host | Desired state; malicious contributor or remote | Full immutable commit IDs, protected-branch reachability, exact required checks, signed commits, one-way PR-only release policy | Proposal/commit digests and CI; revert through reviewed PR | repository/proposal/git-policy/reconcile tests |
| Repository content | Parsers and worker; malicious committed files | Allowlisted roots, object reads, no hooks/protocols, blob/size checks, canonical typed serializers, local-only endpoints, immutable adapter executable paths | Validation results and rejected proposal audit | `control/tests/security/test_untrusted_repository.py`, `test_boundaries.py` |
| PostgreSQL | Jobs, sessions, audit; database attacker or accidental misuse | Private data network, file secrets, migrations, checks/fences; no model/profile authority tables | Health alert, encrypted backup, audit/count verification | migration, job, backup/recovery tests |
| Control worker | Cluster mutation; forged reconcile job, stale/crashed worker | Generic jobs cannot enqueue reconciliation; the worker matches queued content to the exact checked-out plan before mutation; transactional claims, leases, attempt fences, sorted node leases, unknown-kind failure, shared online lock | Worker-starvation alerts and job evidence; reclaim expired attempt | job/worker/reconcile tests |
| Agent enrollment and identity | Agent impersonation, enrollment replay, stolen certificate | Caddy mTLS accepts a 24-hour client-auth-only certificate for one canonical node; enrollment grants are hashed, node-bound, single-use, and short-lived; Smallstep JWK authorization is one-use and fixed-policy | Local PostgreSQL revocation denies immediately; retry only unconfirmed Smallstep serials; certificate loss requires console-verified re-enrollment | `control/tests/test_step_ca.py`, `tests/runbooks/test_agent_pki.py` |
| Agent CA boundary | Online issuer compromise, root theft, forged provider response | The offline root private key is never mounted; step-ca gets encrypted intermediate material and public provisioner JWK, while control-api alone gets the private JWK; fixed URL/root, bounded TLS HTTP, exact CSR/certificate/chain validation | Rotate the online intermediate/provisioner, revoke affected nodes, preserve local denial during remote uncertainty, restore CA DB and PostgreSQL from one backup generation | `control/tests/test_step_ca.py`, `deploy/compose/tests/test_agent_ingress.py` |
| Control-to-agent operation protocol | Cross-node claim, stale fence, malicious payload | A versioned shared wheel accepts only allowlisted operations; every claim binds job, node, attempt, fence, commit, digest, and UTC deadline. Reject unknown fields, commands, filesystem paths, credentials, and documents over 64 KiB | Persist bounded progress/result evidence; reject expired or superseded fences, mark the operation for retry/operator review, and retain the prior attempt for audit | `control/tests/security/test_agent_protocol.py`, `agent_protocol/tests/test_contracts.py` |
| Agent result channel | Result exfiltration or secret-bearing diagnostic output | Result schema applies the same recursive secret/path rejection and 64 KiB limit; control redacts failure reasons before persistence | Treat unexpected result rejection as a security event, rotate exposed credentials, revoke the certificate, and recollect only approved bounded evidence | protocol boundary and logging tests |
| Agent credential storage | Certificate theft from an agent or control host | Store only public certificate metadata in PostgreSQL; private keys remain in protected node-local storage and are never accepted in protocol messages | Revoke the affected serial, quarantine its node, issue a replacement after console identity verification, and review all operations under the stolen serial | agent migration, protocol boundary, and recovery runbooks |
| Agent presence and management address | Spoofed proxy headers, DHCP churn or address reuse, stale observations, and accidental routing over a direct fabric | Caddy deletes every incoming `X-DGX-Agent-*` value and, only after mTLS verification, supplies the direct peer address plus a private proxy-auth token; middleware converts this to typed scope state. Control binds it to the certificate-authenticated `spk_` ID, requires a canonical address inside `DGX_MANAGEMENT_CIDRS`, excludes `DGX_DIRECT_FABRIC_CIDRS`, and expires observations after 150 seconds | Invalid or stale observations fail closed. An address change publishes maintenance before replacement validation, so the old address is withdrawn and cannot reappear after a rejected replacement | `control/tests/test_presence.py`, `control/tests/test_routes.py`, `control/tests/security/test_agent_identity.py`, `deploy/compose/tests/test_agent_ingress.py` |
| Spark SSH | Root policy and model runtime; hostile network/node impostor | Trusted console assertion, strict host keys, no shell interpolation, explicit endpoint, staged digest-checked scripts, recovery gate | Identity quarantine and resumable journal; console rollback | install identity/remote/steps tests |
| LiteLLM/Caddy routes | Inference availability; shadow model/upstream; dead publisher | Routes only from an exact checked-out plan and `ready` fleet identity, fresh policy-bounded management observation, and repository-declared workload port; address replacement enters maintenance before probing. Hermes candidates additionally require accepted maturity and `local_only = true`; ordered duplicate deployments contain only management-IP URLs. The generated config requires a matching SHA-256 lease issued after supervisor startup and bounded by presence expiry | Route-state and lease-expiry alert; empty bootstrap on worker death, restart, invalid replacement, or expired presence | routes/LiteLLM/Hermes policy tests |
| Metrics and logs | Operational metadata, prompts/secrets; curious viewer | Stable bounded labels, separate scrape token, centralized redaction/truncation, role-gated content-addressed logs | Secret-leak tests, rotation, checksum verification | metrics/logging/observability tests |
| Backup storage | Database/config/Hermes state copies; backup thief or tampering | Required external authenticated encryption, canonical manifest/checksums, 0600 files, no plaintext production mode; Hermes data/workspaces included and disposable cache omitted | Restore verification before destructive action; Hermes remains stopped pending fresh presence/routes; disposable-host drill | offline and backup/restore tests |
| Docker service host | All services; host admin, disk loss | Separate least-privilege containers, read-only roots, numeric users, private networks, digest-pinned images, bounded volumes/logs | Supply-chain verification, host-loss restore, alerts | Compose and release acceptance gates |
| Tailscale gateway recovery | Human ingress identity; stolen OAuth secret, lost state, or stale extra Service | File-backed OAuth client limited to `auth_keys` for `tag:dgx-gateway`, persisted state, exact Service auto-approvals, exact exported three-Service map, HTTPS-only listeners, no wildcard or LAN fallback | Revoke OAuth client/node/tag, verify status and exported map, restore encrypted state or create one reviewed replacement | `deploy/compose/tests/test_tailscale.py`, Tailscale runbook |
| Hermes Agent | Prompts, sessions, repository credentials and terminal tools; hostile tailnet user, prompt injection, or container escape | Separate tailnet and API identities, read-only root, `no-new-privileges`, empty capability allowlist, no host ports/socket/devices/control networks, three exact networks, fixed local LiteLLM alias, explicit CORS | Revoke user/API/repository credentials, stop service, inspect bounded logs, restore encrypted data/workspaces and require fresh routes | `test_hermes_agent.py`, `hermes-agent-runtime.sh`, Hermes runbook |
| Hermes host egress | NAS/Spark/control services; malicious tool or prompt-driven network access | One-off script resolves the exact bridge and installs an owned source-bound chain denying management, direct-fabric, metadata, and sibling Docker subnets while preserving DNS/Internet | `--verify`, host firewall audit, stop Hermes on drift; no Docker self-repair privilege | `test_hermes_egress.py`, Hermes runbook |

## Role matrix

Viewer is read-only. Operator may enqueue jobs, preview proposals, and plan or
enqueue eligible reconciliation. Administrator additionally submits repository
changes and performs release-policy transitions. The executable
`MUTATION_ROLES` matrix is required to equal every mutating `/api/v1` route.

Offline bootstrap/recovery is not an API role. It requires host access, an
exclusive lock proving API and worker are stopped, and explicit destructive
confirmation for restore.

## Residual risks

Physical compromise of a Spark or control host, a malicious signed base image,
and compromise of all protected-branch administrators remain outside software
prevention. Recovery depends on independent console access, off-host encrypted
backups, pinned image/SBOM verification, and protected code-host credentials.
Hardware acceptance is never inferred from simulation and requires explicitly
approved targets.

Hermes intentionally has terminal and Internet tooling. Prompt injection or a
malicious repository can therefore alter its persisted state, disclose a
credential available inside its own container, or act through that credential.
The empty Linux-capability allowlist, read-only root, network segmentation,
host egress chain, narrow repository credentials, and encrypted recovery limit
blast radius; they do not make agent-executed code trustworthy. The pinned
image must pass the runtime harness with no added capability before deployment.

An agent security incident has a deliberate recovery boundary: do not reuse an
enrollment secret or certificate after suspected impersonation, replay, theft,
or exfiltration. Quarantine the node, revoke its certificate, invalidate any
running fence, rotate affected credentials, inspect durable attempt evidence,
and re-enroll only after an independent console identity check. A stale or
rejected result is not success evidence; the parent job remains recoverable
through an explicit retry or operator decision.

Smallstep revocation is passive in v0.30.2: it prevents CA renewal but does not
make an already-issued leaf disappear from every TLS verifier. DGX-Forge's
database and Caddy-to-control identity validator are therefore the immediate
revocation boundary. A control database outage fails agent authorization
closed. The remaining exposure is bounded by the 24-hour leaf lifetime.

Management addresses remain observations, not cryptographic service identities.
If DHCP reassigns a Spark address immediately to a hostile LAN host, traffic to
an already-published inference endpoint could reach that host until the
150-second observation window and reconciliation withdraw the route. DHCP
reservations, network admission controls, the upstream application key, and
alerting on address changes reduce this residual risk; hard-coded per-node IPs
would not remove it and are not used as an identity control.
