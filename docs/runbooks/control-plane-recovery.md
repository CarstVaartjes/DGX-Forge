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
