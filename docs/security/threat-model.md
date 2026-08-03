# DGX Forge platform threat model

## Security objectives

The platform must not publish an unhealthy model, execute repository-selected
code, confuse a mutable address with physical node identity, disclose prompts or
credentials, or permit an unreviewed repository state to reach the cluster.
Git is desired-state authority. PostgreSQL is operational state only.

## Trust boundaries

| Boundary | Assets and attacker | Prevention | Detection and recovery | Executable evidence |
|---|---|---|---|---|
| Public Caddy ingress | Sessions, inference keys; unauthenticated network client | Caddy-only published ports, TLS, body limit, headers, API auth/RBAC/CSRF | Correlated bounded API metrics/logs; revoke tokens, withdraw routes | `deploy/compose/tests/test_networking.py`, `control/tests/test_api.py` |
| Admin browser | Proposal intent; malicious site or compromised browser | Same-origin API only, SameSite/HttpOnly session, double-submit CSRF, typed forms, explicit diff confirmation | Audit request/actor/base/targets; revoke session | web unit/Playwright tests, authorization matrix |
| CLI token | Administrator capability; local unprivileged user | HTTPS origin validation, regular non-symlink token file, bounded JSON, no token in argv/output | API audit and token rotation | `tests/spark_profiles/test_control_client.py` |
| Git/code host | Desired state; malicious contributor or remote | Full immutable commit IDs, protected-branch reachability, exact required checks, signed commits, one-way PR-only release policy | Proposal/commit digests and CI; revert through reviewed PR | repository/proposal/git-policy/reconcile tests |
| Repository content | Parsers and worker; malicious committed files | Allowlisted roots, object reads, no hooks/protocols, blob/size checks, canonical typed serializers, local-only endpoints, immutable adapter executable paths | Validation results and rejected proposal audit | `control/tests/security/test_untrusted_repository.py`, `test_boundaries.py` |
| PostgreSQL | Jobs, sessions, audit; database attacker or accidental misuse | Private data network, file secrets, migrations, checks/fences; no model/profile authority tables | Health alert, encrypted backup, audit/count verification | migration, job, backup/recovery tests |
| Control worker | Cluster mutation; stale/crashed worker | Transactional claims, leases, attempt fences, sorted node leases, unknown-kind failure, shared online lock | Worker-starvation alerts and job evidence; reclaim expired attempt | job/worker/reconcile tests |
| Agent enrollment and identity | Agent impersonation, enrollment replay, stolen certificate | Enrollment binds one canonical node ID to a short-lived certificate serial/fingerprint; use authenticated, single-use enrollment material and reject revoked, expired, or node-mismatched certificates | Audit enrollment and certificate serials; revoke the node and certificate on mismatch, then require console-verified re-enrollment | agent migration and protocol boundary tests |
| Control-to-agent operation protocol | Cross-node claim, stale fence, malicious payload | A versioned shared wheel accepts only allowlisted operations; every claim binds job, node, attempt, fence, commit, digest, and UTC deadline. Reject unknown fields, commands, filesystem paths, credentials, and documents over 64 KiB | Persist bounded progress/result evidence; reject expired or superseded fences, mark the operation for retry/operator review, and retain the prior attempt for audit | `control/tests/security/test_agent_protocol.py`, `agent_protocol/tests/test_contracts.py` |
| Agent result channel | Result exfiltration or secret-bearing diagnostic output | Result schema applies the same recursive secret/path rejection and 64 KiB limit; control redacts failure reasons before persistence | Treat unexpected result rejection as a security event, rotate exposed credentials, revoke the certificate, and recollect only approved bounded evidence | protocol boundary and logging tests |
| Agent credential storage | Certificate theft from an agent or control host | Store only public certificate metadata in PostgreSQL; private keys remain in protected node-local storage and are never accepted in protocol messages | Revoke the affected serial, quarantine its node, issue a replacement after console identity verification, and review all operations under the stolen serial | agent migration, protocol boundary, and recovery runbooks |
| Spark SSH | Root policy and model runtime; hostile network/node impostor | Trusted console assertion, strict host keys, no shell interpolation, explicit endpoint, staged digest-checked scripts, recovery gate | Identity quarantine and resumable journal; console rollback | install identity/remote/steps tests |
| LiteLLM/Caddy routes | Inference availability; shadow model/upstream | Routes only from eligible accepted snapshot, exact upstream allowlist, policy subset, atomic generation, maintenance on failure | Route-state alert and generation digests; retain prior/maintenance config | routes/LiteLLM tests |
| Metrics and logs | Operational metadata, prompts/secrets; curious viewer | Stable bounded labels, separate scrape token, centralized redaction/truncation, role-gated content-addressed logs | Secret-leak tests, rotation, checksum verification | metrics/logging/observability tests |
| Backup storage | Database/config copies; backup thief or tampering | Required external authenticated encryption, canonical manifest/checksums, 0600 files, no plaintext production mode | Restore verification before destructive action; disposable-host drill | offline and backup/restore tests |
| Docker service host | All services; host admin, disk loss | Separate least-privilege containers, read-only roots, numeric users, private networks, digest-pinned images, bounded volumes/logs | Supply-chain verification, host-loss restore, alerts | Compose and release acceptance gates |

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

An agent security incident has a deliberate recovery boundary: do not reuse an
enrollment secret or certificate after suspected impersonation, replay, theft,
or exfiltration. Quarantine the node, revoke its certificate, invalidate any
running fence, rotate affected credentials, inspect durable attempt evidence,
and re-enroll only after an independent console identity check. A stale or
rejected result is not success evidence; the parent job remains recoverable
through an explicit retry or operator decision.
