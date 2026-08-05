# Spark agent PKI operations

This runbook operates the recommended Smallstep `step-ca` provider for DGX-Forge.
It is written for a small cluster, but contains no Spark name, address, or count.
Certificates last exactly 24 hours. The offline root private key never enters the
NAS, Docker, Compose, a job payload, or Git.

The implementation and configuration were checked against `smallstep/certificates`
tag `v0.30.2`: `api/sign.go`, `api/revoke.go`, `api/crl.go`, and
`authority/provisioner/jwk.go`. The JWK provisioner consumes one-use JWT IDs,
validates the base `/1.0/sign` or `/1.0/revoke` audience with one minute of
leeway, and binds token subject/SANs to the signed CSR.

## Layout and preflight

Use two physically separate locations. `OFFLINE_PKI_DIR` is removable media on
an offline workstation. `PKI_SECRET_DIR` and `STEP_CA_DATA_DIR` are on the NAS.
Compose bind-backed secret uid/gid/mode behavior is not portable, so set and
verify host ownership explicitly for UID 10001 (control-api) and the UID used by
the pinned step-ca image.

```sh
OFFLINE_PKI_DIR=/media/offline/dgx-forge-pki
PKI_SECRET_DIR=/srv/dgx-forge/secrets
STEP_CA_DATA_DIR=/srv/dgx-forge/step-ca
install -d -m 0700 "$OFFLINE_PKI_DIR" "$PKI_SECRET_DIR" "$STEP_CA_DATA_DIR"
umask 077
step version
docker version
```

Keep NTP healthy on the NAS and Sparks. Alert at 30 seconds of clock skew and
stop issuance before one minute; authorization tokens deliberately allow only
30 seconds and step-ca v0.30.2 allows at most one minute.

## Restricted LAN endpoint

Reserve `10.0.0.2` for the NAS and resolve the enrollment, agent, and registry
names below to that same address only on the management LAN:

```text
enroll.dgx-forge.lan   10.0.0.2
agents.dgx-forge.lan   10.0.0.2
registry.dgx-forge.lan 10.0.0.2
```

Caddy binds backend TLS only to `10.0.0.2:8443`. The NAS firewall permits that
port only from `10.0.0.0/24`, preferably narrowed to reserved Spark leases.
Enrollment exposes only `/agent/v1/enroll`; the agent and registry names require
the issued mTLS identity. Human control, inference, Grafana, and Hermes routes
are absent from this listener and remain tailnet-only.

Install the Caddy backend trust anchor and stable DNS names during each manual
Spark hardening/bootstrap. The installed agent initiates outbound long polling;
the manager does not scan the LAN. The certificate-bound `spk_` identity and a
fresh proxy-observed address within `DGX_MANAGEMENT_CIDRS` drive availability.
DHCP reservations improve operations but are not a correctness dependency.

## Create the offline root and online intermediate

Perform this block on the disconnected workstation. Store the root password in
a separate offline recovery medium. Generate an encrypted online intermediate
with path length zero and a one-year lifetime; rotate it before expiry.

```sh
openssl rand -base64 32 > "$OFFLINE_PKI_DIR/root-password"
openssl rand -base64 32 > "$OFFLINE_PKI_DIR/intermediate-password"
step certificate create "DGX Forge Offline Root" \
  "$OFFLINE_PKI_DIR/root_ca.crt" "$OFFLINE_PKI_DIR/root_ca.key" \
  --profile root-ca --kty OKP --curve Ed25519 --not-after 87600h \
  --password-file "$OFFLINE_PKI_DIR/root-password"
step certificate create "DGX Forge Agent Intermediate" \
  "$OFFLINE_PKI_DIR/intermediate_ca.crt" "$OFFLINE_PKI_DIR/intermediate_ca_key" \
  --profile intermediate-ca --kty OKP --curve Ed25519 --not-after 8760h \
  --ca "$OFFLINE_PKI_DIR/root_ca.crt" --ca-key "$OFFLINE_PKI_DIR/root_ca.key" \
  --ca-password-file "$OFFLINE_PKI_DIR/root-password" \
  --password-file "$OFFLINE_PKI_DIR/intermediate-password"
chmod 600 "$OFFLINE_PKI_DIR/root_ca.key" "$OFFLINE_PKI_DIR/intermediate_ca_key" \
  "$OFFLINE_PKI_DIR/root-password" "$OFFLINE_PKI_DIR/intermediate-password"
chmod 644 "$OFFLINE_PKI_DIR/root_ca.crt" "$OFFLINE_PKI_DIR/intermediate_ca.crt"
step certificate inspect "$OFFLINE_PKI_DIR/intermediate_ca.crt" --short
```

Transfer only `root_ca.crt`, `intermediate_ca.crt`, the encrypted
`intermediate_ca_key`, and its password file to the NAS. Do not transfer the
offline root private key. The root certificate becomes both
`step-ca-root-certificate` and the Caddy `agent-client-ca` trust anchor.

## Create the narrow JWK provisioner

Generate a deployment-specific ES256 JWK pair on the NAS. The private JWK is
mounted only into control-api. step-ca receives only the public JWK in its
generated configuration; it receives no `encryptedKey` for this provisioner.

```sh
step crypto jwk create \
  "$PKI_SECRET_DIR/agent-ca-public.jwk" "$PKI_SECRET_DIR/agent-ca-credential" \
  --kty EC --crv P-256 --no-password --insecure
AGENT_CA_PROVISIONER_KID="$(step crypto jwk thumbprint < "$PKI_SECRET_DIR/agent-ca-public.jwk")"
jq --arg kid "$AGENT_CA_PROVISIONER_KID" '.kid=$kid | .alg="ES256" | .use="sig"' \
  "$PKI_SECRET_DIR/agent-ca-public.jwk" > "$PKI_SECRET_DIR/agent-ca-public.with-kid.jwk"
jq --arg kid "$AGENT_CA_PROVISIONER_KID" '.kid=$kid | .alg="ES256" | .use="sig"' \
  "$PKI_SECRET_DIR/agent-ca-credential" > "$PKI_SECRET_DIR/agent-ca-credential.with-kid"
mv "$PKI_SECRET_DIR/agent-ca-public.with-kid.jwk" "$PKI_SECRET_DIR/agent-ca-public.jwk"
mv "$PKI_SECRET_DIR/agent-ca-credential.with-kid" "$PKI_SECRET_DIR/agent-ca-credential"
jq --slurpfile key "$PKI_SECRET_DIR/agent-ca-public.jwk" \
  '.authority.provisioners[0].key=$key[0]' deploy/compose/step-ca/ca.json \
  > "$STEP_CA_DATA_DIR/ca.json"
chmod 600 "$PKI_SECRET_DIR/agent-ca-credential" "$STEP_CA_DATA_DIR/ca.json"
test "$(jq -r '.authority.provisioners[0].key.kid' "$STEP_CA_DATA_DIR/ca.json")" = "$AGENT_CA_PROVISIONER_KID"
```

The tracked template fixes the JWK provisioner to 24 hours, disables direct CA
renewal and Smallstep extensions, and uses a client-auth-only template. Normal
renewal is a new `/1.0/sign` request: DGX-Forge first authenticates the existing
mTLS identity, then submits the new node-signed CSR under fixed policy.
CRL generation is enabled with `generateOnRevoke`, a one-hour cache duration,
and a 30-minute renewal period. The control provider accepts only a correctly
signed CRL whose update window is current and bounded to that configured hour.

## Start and verify the production provider

Set `STEP_CA_CONFIG_FILE`, `AGENT_CA_PROVISIONER_KID`, and all file variables in
`.env`. The normal production selection is exactly these two files:

```sh
docker compose --env-file deploy/compose/.env \
  -f deploy/compose/compose.yaml -f deploy/compose/compose.step-ca.yaml config --quiet
docker compose --env-file deploy/compose/.env \
  -f deploy/compose/compose.yaml -f deploy/compose/compose.step-ca.yaml up -d
docker compose --env-file deploy/compose/.env \
  -f deploy/compose/compose.yaml -f deploy/compose/compose.step-ca.yaml ps
docker compose --env-file deploy/compose/.env \
  -f deploy/compose/compose.yaml -f deploy/compose/compose.step-ca.yaml exec step-ca \
  step ca health --ca-url https://127.0.0.1:9000 --root /run/secrets/root_ca.crt
```

Only Caddy publishes a port. step-ca and control-api share the internal `ca`
network. The worker has `DGX_AGENT_RUNTIME=disabled` and loads no CA, proxy, or
agent credential. Inspect the rendered mounts and confirm no root private key.

## Revocation and uncertain remote results

Use the administrator API/CLI node-revoke operation. DGX-Forge commits local
node retirement and certificate revocation first, so Caddy-forwarded identities
are rejected immediately. It then requests passive step-ca revocation, which
prevents provider renewal. Confirmed serials receive `ca_revoked_at`; retries
send only unconfirmed serials.

If the API reports `local revocation complete; remote CA revocation is
uncertain`, do not undo local state. Restore CA reachability and repeat the same
node-revoke command. Repetition is idempotent in effect. If step-ca accepted a
request but its response was lost, inspect the CA database/audit log for that
decimal serial; retain the local denial and record manual reconciliation.

An enrollment stuck in `issuing` is also deliberately never retried. Search the
step-ca audit trail by node subject and issuance time, revoke any possibly issued
serial, then clear/reject the enrollment only through an audited operator
procedure. Never automatically resubmit its authorization token.

```sh
docker compose --env-file deploy/compose/.env \
  -f deploy/compose/compose.yaml -f deploy/compose/compose.step-ca.yaml logs --since 30m step-ca
docker compose --env-file deploy/compose/.env \
  -f deploy/compose/compose.yaml -f deploy/compose/compose.step-ca.yaml logs --since 30m control-api
```

## Expiry and identity-loss recovery

An active node renews before expiry using its existing mTLS identity and a new
node-signed CSR. After expiry, private-key loss, disk replacement, or full Spark
replacement, renewal is unavailable. Certificate loss is treated the same way.
An administrator must verify fresh hardware evidence and create a fresh
enrollment grant that is short-lived, explicit, and node-bound. The Spark
generates a new key locally and goes through normal
approval. You must not copy another Spark's certificate or private identity.

## Intermediate rotation with overlap

Create a new encrypted path-length-zero intermediate under the same offline
root. Stage its certificate/key/password, stop issuance briefly, update both
step-ca and control-api mounts atomically, and start them together. Caddy trusts
the offline root, so certificates from the old and new intermediates overlap for
the old leaf's remaining 24 hours. Verify new issuance, then retain the old
intermediate certificate for audit until every old leaf has expired. Never run
two active issuers with the same provisioner private credential.

```sh
docker compose --env-file deploy/compose/.env \
  -f deploy/compose/compose.yaml -f deploy/compose/compose.step-ca.yaml stop control-api step-ca
sha256sum "$PKI_SECRET_DIR/intermediate_ca.crt" "$PKI_SECRET_DIR/intermediate_ca_key"
docker compose --env-file deploy/compose/.env \
  -f deploy/compose/compose.yaml -f deploy/compose/compose.step-ca.yaml up -d step-ca control-api
docker compose --env-file deploy/compose/.env \
  -f deploy/compose/compose.yaml -f deploy/compose/compose.step-ca.yaml ps
```

For root rotation, distribute an overlap trust bundle containing old and new
root certificates to Caddy first, rotate intermediates and all leaves, wait at
least 24 hours, then remove the old root.

## Backup and restore consistency

Back up the generated public config, encrypted intermediate material and
password, provisioner private JWK, root certificate, step-ca database, and the
PostgreSQL control database. The offline root stays in its own offline backup.
To obtain a consistent CA snapshot, stop issuance/control-api, stop step-ca,
snapshot its data, and dump PostgreSQL before restarting. Encrypt the archive
with the operator backup system and test restoration on an isolated network.

```sh
BACKUP_DIR=/srv/dgx-forge/backups/pki-staging
install -d -m 0700 "$BACKUP_DIR"
docker compose --env-file deploy/compose/.env \
  -f deploy/compose/compose.yaml -f deploy/compose/compose.step-ca.yaml stop control-api step-ca
docker compose --env-file deploy/compose/.env \
  -f deploy/compose/compose.yaml -f deploy/compose/compose.step-ca.yaml exec -T postgres \
  pg_dump -U control -d control --format=custom > "$BACKUP_DIR/control.pgdump"
tar -C /srv/dgx-forge -czf "$BACKUP_DIR/online-pki.tgz" \
  step-ca secrets/agent-ca-credential secrets/agent-ca-public.jwk secrets/intermediate_ca.crt \
  secrets/intermediate_ca_key secrets/step-ca-password secrets/step-ca-root-certificate
docker compose --env-file deploy/compose/.env \
  -f deploy/compose/compose.yaml -f deploy/compose/compose.step-ca.yaml run --rm --no-deps \
  --entrypoint /bin/sh -v "$BACKUP_DIR:/backup" step-ca \
  -c 'tar -C /home/step/db -czf /backup/step-ca-db.tgz .'
docker compose --env-file deploy/compose/.env \
  -f deploy/compose/compose.yaml -f deploy/compose/compose.step-ca.yaml up -d step-ca control-api
```

Restore the step-ca data/config/secrets and PostgreSQL dump from the same backup
generation. Verify CA health, compare intermediate and provisioner public-key
fingerprints, and test one disposable enrollment before restoring ingress.

```sh
docker compose --env-file deploy/compose/.env \
  -f deploy/compose/compose.yaml -f deploy/compose/compose.step-ca.yaml stop control-api step-ca
docker compose --env-file deploy/compose/.env \
  -f deploy/compose/compose.yaml -f deploy/compose/compose.step-ca.yaml run --rm --no-deps \
  --entrypoint /bin/sh -v "$BACKUP_DIR:/backup:ro" step-ca \
  -c 'test -z "$(find /home/step/db -mindepth 1 -print -quit)" && tar -C /home/step/db -xzf /backup/step-ca-db.tgz'
docker compose --env-file deploy/compose/.env \
  -f deploy/compose/compose.yaml -f deploy/compose/compose.step-ca.yaml exec -T postgres \
  pg_restore -U control -d control --clean --if-exists < "$BACKUP_DIR/control.pgdump"
```

## Built-in-to-step-ca migration

Built-in mode is an explicit bootstrap/development overlay, not a second active
issuer. Under the same offline root, prepare step-ca and its deployment-specific
provisioner, validate it on an isolated network, stop control-api, and replace
`compose.builtin-ca.yaml` with `compose.step-ca.yaml`. Existing leaves continue
through the root trust anchor; all new issuance uses Smallstep. Do not merge both
overlays—the settings guard rejects mixed provider material.

```sh
docker compose --env-file deploy/compose/.env \
  -f deploy/compose/compose.yaml -f deploy/compose/compose.builtin-ca.yaml down
docker compose --env-file deploy/compose/.env \
  -f deploy/compose/compose.yaml -f deploy/compose/compose.step-ca.yaml config --quiet
docker compose --env-file deploy/compose/.env \
  -f deploy/compose/compose.yaml -f deploy/compose/compose.step-ca.yaml up -d
```
