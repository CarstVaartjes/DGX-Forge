# Add a DGX Spark node

This runbook adds exactly one DGX Spark to a fleet. It has no assumptions about
the fleet size, hostnames, or IP addresses. Repeat it independently for every
new node.

## Safety boundary

Trusted first contact requires comparing the serial digest and SSH host-key
fingerprints shown at the physical console. Do not accept values learned only
over the network. The installer pauses for this assertion and quarantines a
mismatch. Confirm working console or out-of-band recovery before enabling SSH
hardening.

The commands are dry runs unless `--apply` is present. Journals and sanitized,
content-addressed evidence live under `.state/spark-install`; credentials remain
behind `secret://` references.

## Start and inspect

```bash
bin/spark-install node start \
  --host NEW_NODE_ADDRESS --user ADMIN_USER \
  --credential-ref secret://ssh/admin \
  --display-name DISPLAY_NAME --label purpose=inference --json
```

Review the plan, then repeat it with `--apply` plus the console assertion,
administrator public-key path/fingerprint, and `--recovery-verified`. A missing
operator assertion produces a waiting journal rather than a partial acceptance.

```bash
bin/spark-install node status NODE_ID --json
bin/spark-install node resume NODE_ID --apply \
  --trusted-serial-sha256 SERIAL_DIGEST \
  --trusted-host-key-fingerprint HOST_KEY_FINGERPRINT \
  --admin-public-key /safe/path/admin.pub \
  --admin-key-fingerprint ADMIN_KEY_FINGERPRINT \
  --recovery-verified --json
```

Use `retry NODE_ID --apply` only after correcting a recorded failure. Use
`verify NODE_ID` to require the accepted terminal state.

## Propose the fleet record

```bash
bin/spark-install node emit-record NODE_ID >node-record.toml
```

This command does not modify Git. Review the sanitized record and add it to
`inventory/fleet.toml` in a normal commit. Keep physical links and fabric
relationships in the separate topology document; adding a node must not invent
topology.

If verification or recovery access fails, stop. Restore access through the
physical console, inspect the journal, and resume only after the trusted facts
match again.
