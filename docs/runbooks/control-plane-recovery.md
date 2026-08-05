# Back up and recover the control plane

Use `deploy/compose/bin/backup-control-plane OUTPUT ENCRYPT_COMMAND`. The
encryption command must read plaintext from standard input and write an
authenticated encrypted stream to standard output. Plaintext production
backups are not supported. The canonical, checksum-bound archive contains the
PostgreSQL custom dump, Compose configuration, Hermes `data`, and Hermes
`workspaces`. Hermes cache is deliberately excluded.

Back up these service-host items in the same authenticated encrypted off-host
generation:

- the `tailscale-state` volume and both scoped OAuth credential files;
- the external Hermes API-key file;
- step-ca state and all online PKI material listed in the agent PKI runbook; and
- expected repository/image digests and the Tailscale gateway node ID.

Test every backup on a disposable Docker host. Inspection decrypts in memory,
rejects links and unsafe paths, verifies exact files and checksums, and rejects
non-canonical or modified archives.

Restore is destructive and requires the literal flag:

```bash
HERMES_DATA_ROOT=/srv/dgx-forge/hermes HERMES_UID=1100 HERMES_GID=1100 \
  deploy/compose/bin/restore-control-plane BACKUP DECRYPT_COMMAND --apply
```

The script stops API, worker, and Hermes; takes the offline lock; verifies and
stages the archive; restores PostgreSQL; atomically installs the Hermes trees;
restores owner-only permissions; and leaves Hermes stopped. Cache is neither
restored nor required. Restore the API key and Tailscale/PKI state separately.

Run portable acceptance on every change:

```bash
scripts/accept-control-recovery \
  --output inventory/reports/control-plane-recovery.json --json
```

Before release, repeat authenticated-encryption backup and restore on a second
Docker-capable Linux host. Retain only sanitized evidence.

After restore, verify database and CA health, then start the normal project.
Hermes must wait for fresh authenticated Spark presence, a successful accepted
local model probe, and a new LiteLLM lease. Verify the three exact Tailscale
Services, API-key continuity, session visibility, workspace contents, and the
absence of cloud model configuration.

If Tailscale state is unavailable, scoped OAuth performs unattended tagged
re-enrollment and exact Service auto-approval. Verify the new node and revoke
the old node. If OAuth is unavailable, ingress remains closed. If Hermes data
is unavailable, repeat setup; never expose a LAN port or add a remote model.
