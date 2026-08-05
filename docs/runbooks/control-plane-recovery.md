# Back up and recover the control plane

Use `deploy/compose/bin/backup-control-plane OUTPUT ENCRYPT_COMMAND`. The
encryption command must read plaintext from standard input and write an
authenticated encrypted stream to standard output. Plaintext production
backups are not supported. The archive includes a PostgreSQL custom dump,
Compose/Caddy configuration, and a canonical SHA-256 manifest.

Test every backup on a disposable Docker host. Inspection decrypts in memory,
rejects links and unsafe paths, verifies the exact file set and every checksum,
and rejects non-canonical or modified archives.

Restore is destructive and therefore requires the literal `--apply` flag:

```bash
deploy/compose/bin/restore-control-plane BACKUP DECRYPT_COMMAND --apply
```

The script stops API and worker, obtains the offline maintenance boundary,
verifies and stages the archive, then invokes `pg_restore --clean --if-exists`.
Afterward, start the services, run health/readiness checks, inspect recent audit
events, and compare the checked-out repository commit with the expected base.

## Acceptance levels

Run the portable, non-destructive acceptance on every change:

```bash
scripts/accept-control-recovery --output inventory/reports/control-plane-recovery.json --json
```

This exercises canonical backup inspection, restoration into a distinct clean
filesystem root, integrity checks, and fail-closed route state. It deliberately
uses a transparent test transform and records that fact. It is not evidence of
production encryption or physical host-loss recovery.

Before a real release, repeat the Compose backup and restore with authenticated
encryption on a second generic Docker-capable Linux machine. Retain only the
sanitized report, image/repository digests, counts, and pass/fail results. Never
copy database contents, credentials, prompts, or model responses into evidence.

## Service-host state outside the database archive

The control-plane archive is only one part of a complete NAS backup. The same
encrypted, authenticated off-host generation must also contain:

- the `tailscale-state` Docker volume;
- both Tailscale OAuth credential files;
- `/srv/dgx-forge/ai-devbox/home`, `workspaces`, `cache`, and
  `ssh-host-keys`;
- the AI devbox authorized-public-key source file; and
- step-ca state and all online PKI material listed in the agent PKI runbook.

Treat devbox home as credential-bearing secret data. Record the Tailscale node
ID, both advertised Service names, and both devbox SSH host-key fingerprints in
the encrypted manifest or a separately authenticated recovery record.

On a replacement host, restore secret files and volumes before starting the
one Compose project. Restore home/workspaces/cache as UID/GID 1100 and the SSH
host-key directory as root with mode 0700. Start the stack, then verify database
health, CA health, Tailscale status, both named Services, devbox key-only login,
and the expected SSH fingerprints.

If Tailscale state is unavailable, the file-backed scoped OAuth client performs
unattended tagged re-enrollment and the exact service auto-approvals restore
advertisements. Verify the new gateway node and revoke the orphaned old node.
If OAuth is unavailable, recovery fails closed with no tailnet ingress.

Loss of devbox host keys is different: it creates a new SSH identity and must be
handled as a security-sensitive recovery event. Do not clear strict client
host-key state until the new fingerprint has been verified out of band.
