# Temporary Local Release Workaround Design

**Date:** 2026-08-06

**Status:** Withdrawn; must not be implemented or used

**Superseded by:** The protected tag workflow in
[`platform-release-publication.md`](../../runbooks/platform-release-publication.md)

## Decision

DGX-Forge will wait for GitHub Actions and use the normal protected release
workflow. A developer workstation, personal access token, administrator
override, or manually uploaded package is not an official build or release
authority.

The previously considered incident path is withdrawn in full. In particular,
locally built images cannot be uploaded with a personal token and later made
official merely by attaching GitHub OIDC or TUF evidence. Doing so would attest
to already supplied bytes and silently elevate the local uploader into the
trusted build authority. It would not prove that the protected workflow built
the bytes from the reviewed tag.

## Consequences

- The stable `vX.Y.Z` tag workflow is the only path that may build and publish
  DGX-Forge platform artifacts.
- Required pull-request checks remain enabled and must pass before merge.
- No release job accepts pre-uploaded workstation artifacts as build input.
- No PAT, long-lived registry credential, local signing key, branch-protection
  bypass, or alternate TUF authority is introduced.
- Manually published package versions are disposable candidates only. They are
  never installable platform releases and provide no reusable release evidence.
- The delegated platform authority remains a separate protected OIDC boundary;
  its successful receipt is required before a release can update `stable`.

## Historical context

This document records why the workaround was rejected after a GitHub Actions
service incident. It is retained to prevent the same trust-boundary mistake
from being reintroduced. The current release procedure and exact `v0.1.0`
sequence are maintained only in the platform publication runbook.
