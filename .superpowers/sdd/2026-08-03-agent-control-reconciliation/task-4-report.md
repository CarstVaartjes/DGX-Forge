# Task 4 report: remove SSH from production worker wiring

## Result

Production control no longer uses SSH, SCP, `sparkctl`, profile switching, or
deployment scripts as its routine Spark transport. Sparks retain administrator
SSH for onboarding and explicit break-glass recovery. Normal work is a durable,
fenced agent reconciliation over the outbound mTLS agent protocol.

The API and worker are separate image targets. The API retains the repository,
Git, signing material, and OpenSSH client required for reviewed repository
administration. The worker image contains none of those inputs or executables,
has no repository mount or cluster-egress network, and receives only its
database credential, an independent worker-authority secret, route publication
storage, and supervisor acknowledgement storage.

## Repository-less worker authority

The API evaluates the exact persisted reconciliation and repository policy.
Each request and response uses a domain-separated canonical HMAC document bound
to:

- schema version and a fresh nonce;
- reconciliation ID, base commit, and immutable plan digest;
- the SHA-256 digest of canonical published-route policy inputs;
- repository current/eligible state;
- commit-pinned Hermes deployments; and
- an issuance/expiry interval bounded to 15 seconds.

The worker clears prior state, loads the persisted-plan and authenticated
presence snapshot in a short transaction, closes that transaction, and only
then performs the bounded internal HTTP request. The locked reconciliation tick
recomputes the exact identity and route scope using cache-only methods. Route
publication also consumes cached deployments only, so neither PostgreSQL row
locks nor the publication file lock contain network I/O. Transport failure,
expiry, a signed negative, or identity/route drift still enters the locked tick
and follows the existing durable withdrawal/quiescence path.

The client disables environment proxies and redirects, bounds timeout and
response size, rejects malformed or inconsistent signed decisions, and cannot
reuse a prior positive after any failed prefetch. The API reads Git head before
eligibility/policy evaluation and again after commit-pinned Hermes evaluation;
a concurrent head change discards every deployment.

## Legacy and upgrade behavior

The direct subprocess implementation is isolated in `legacy_runtime.py` and is
available only in development/test with the exact selector
`DGX_LEGACY_DIRECT_TRANSPORT=explicit-test-only`. Production rejects any legacy
selector. Old unlinked queued or expired jobs are quarantined as
`waiting-for-operator` without creating a new attempt. Generic probe creation is
rejected; probes arise only as persisted `node.probe` reconciliation operations.

## RED/GREEN evidence

- Initial worker-authority tests failed because the service did not authenticate
  reconciliation/plan identity and `HttpWorkerAuthority` had no prefetch cache.
  The final authority file passes 21 tests.
- Route-drift tests proved a presence change after prefetch must fail during the
  locked authority check, before publication. The completed-owner transport-
  failure regression proves a failed prefetch still withdraws existing routes.
- A real PostgreSQL `NOWAIT` test proves reconciliation, Job, AgentNode,
  AgentCertificate, and AgentPresence rows are all lockable during the prefetch
  callback, demonstrating the snapshot transaction has closed before the remote
  call.
- A real `AtomicRouteBundlePublisher.publish()` test publishes twice after one
  prefetch and proves the HTTP opener remains at exactly one call while the file
  lock and render paths run.
- The built worker target contains no `ssh`, `scp`, `git`, `sparkctl`, repository,
  profile, or deployment-script path and imports the production worker normally.
- The integration merge had dropped the executable bit from
  `scripts/accept-platform-lifecycle`; the gate exposed it, the mode was restored,
  and lifecycle acceptance passed.

## Final verification

- control tests excluding the separately executed PostgreSQL file: **636 passed**;
- actual PostgreSQL reconciliation races: **41 passed**;
- focused authority/reconciliation/API/settings/migration/runtime matrix:
  **141 passed** before the final added regressions, followed by **109 passed** in
  independent review and **21 passed** for the final authority file;
- Compose tests: **64 passed**;
- agent protocol tests: **321 passed**;
- agent lifecycle/failure simulation plus platform lifecycle: **13 passed**;
- supply-chain tests: **17 passed** with `PYTHONMALLOC=malloc` and
  `PYTHONDONTWRITEBYTECODE=1` to avoid the documented local CPython 3.12 allocator
  crash; direct supply-chain verification passed with manifest SHA-256
  `91d9ca7283665c5925981df69376711645adab3acf51d9aa96cacb1e6cd9bc8e`;
- built worker-image inspection: **1 passed**;
- Ruff 0.16.1, compileall, Compose JSON render, and `git diff --check`: passed;
- independent security rereview: **0 Critical, 0 Important, 0 Minor**.

The unchanged native PyInstaller smoke remains affected locally by the already
documented CPython 3.12/PyInstaller SIGSEGV; 546 other agent tests passed. This
does not touch Task 4 code and remains covered by hosted Linux/macOS CI.
