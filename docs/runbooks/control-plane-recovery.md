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
