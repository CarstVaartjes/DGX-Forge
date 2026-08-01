# Direct ConnectX-7 fabric

This runbook configures a two-DGX-Spark, directly cabled ConnectX-7 fabric.
The selected path is the official Spark-side CLI/manual fallback from
`dgx-spark-playbooks` commit `1fb66f059ee427c5a3678b3117ef73aab042b458`.
It deliberately does not use the Mac administration key for node-to-node
access and does not enable SSH agent forwarding. The direct fabric must never
receive a default route.

Do not populate `inventory/cluster.toml` or commit `inventory/reports/fabric.json`
until the post-configuration probes below have been captured. The staged
values are a manual plan, not final live evidence.

## Evidence and safety gate

Before applying the manual plan, retain all of the following:

- a photo of the label on the *single* QSFP112 DAC cable, including its part
  number,
- a rear-panel photo proving that one cable joins the same numbered ConnectX-7
  QSFP port on both Sparks, and
- the elevated `ethtool -m` output captured from both ends.

On the current hosts, one physical QSFP connection is exposed as two Linux
interfaces and RDMA HCAs. This is expected on DGX Spark; do **not** treat it as
two cables or select only one function:

| Node | Linux interfaces reported `LOWER_UP` | RDMA HCAs | Observed speed |
| --- | --- | --- | --- |
| Spark 1 | `enp1s0f1np1`, `enP2p1s0f1np1` | `rocep1s0f1`, `roceP2p1s0f1` | 200000 Mb/s each |
| Spark 2 | `enp1s0f1np1`, `enP2p1s0f1np1` | `rocep1s0f1`, `roceP2p1s0f1` | 200000 Mb/s each |

The controller's elevated, read-only `ethtool -m` evidence identifies the
installed cable at both ends as `Amphenol`, OUI `78:a7:14`, vendor PN
`NJAAKK-C106`, revision `B`, serial `APF261610697AC`: a 1 m passive copper,
PAM4 DAC. It agrees at both ends. Amphenol's primary material identifies the
`NJAAKK` family as QSFP 400G, 112G/lane passive DAC, but NVIDIA's current Sync
guide lists `NJAAKK-N911` (not `NJAAKK-C106`) and `Luxshare LMTQF022-SD-R` as
the supported models. Treat `C106` as an undocumented OEM/customer identifier,
not as confirmed supported hardware.

If the evidence must be recaptured, the controller may run this read-only,
elevated probe and attach its output to the change record:

```bash
for host in dgx-spark-1 dgx-spark-2; do
  ssh -o BatchMode=yes -o ForwardAgent=no "$host" \
    'sudo ethtool -m enp1s0f1np1; sudo ethtool -m enP2p1s0f1np1'
done
```

The cable PN remains an undocumented OEM/customer identifier. The selected
manual path may proceed only through the staged preflight below; it requires
both functions to be `UP` at 200000 Mb/s and preserves the management default
route. Any cable or link warning, failed preflight, Netplan error, route
change, or failed postcheck is a hard stop. Do not apply a manual workaround.

## Controller-run NVIDIA Sync operation

NVIDIA Sync is not the selected configuration path. This section remains only
as a controlled, supported alternative if the reviewed manual procedure cannot
be used; do not mix its generated Netplan file with the manual file below.

This is an interactive, sudo-authorized change and must be performed by the
controller/user, not through an unattended SSH command.

1. On the Mac that is on the Sparks' LAN, open NVIDIA Sync and import/add
   `dgx-spark-1` (`192.168.1.211`, Spark 1/head) and `dgx-spark-2`
   (`192.168.1.212`, Spark 2/worker). Keep management on Wi-Fi or 10 GbE.
2. Open **Settings > Cluster Assistant > Add New Cluster**. Select exactly the
   two Sparks and the direct-connect topology. Do not add a switch or a second
   cable. At **Network Check**, capture a clean topology/link result before
   selecting **Confirm Network Configuration**; the latter can mutate Netplan.
3. Only after the controller explicitly approves that clean gate, use the
   controller to enter sudo credentials in
   NVIDIA Sync. Do not copy the Mac `DGX Spark Admin` key to either Spark and
   do not enable agent forwarding. Confirm that the assistant creates a
   distinct inter-device key/aliases; if it exposes an `authorized_keys`
   source restriction, restrict the worker entry to the discovered fabric
   source address.
4. At network confirmation, accept only the two private point-to-point fabric
   subnets. There must be no gateway/default route on either fabric interface.
   Save screenshots of the 200 Gb/s checks and the successful cluster summary;
   use **Copy** in the success screen to save the network information.
5. Do not continue if an assistant readiness, topology, link-speed, or SSH
   check fails. Record that screen and use the official NVIDIA manual workflow
   only after a reviewed, reversible change procedure has been approved.

NVIDIA documents Cluster Assistant at
<https://docs.nvidia.com/sync/latest/cluster-assistant.html>. It writes the
managed cluster plan as `/etc/netplan/99-nvidia-sync-cluster.yaml`; do not
hand-edit that file while the cluster relationship exists.

### Reversal plan

The normal reversible path is **Settings > Clusters > ... > Delete** in NVIDIA
Sync. That removes the cluster relationship and its generated node-to-node SSH
configuration. If the controller has first preserved management access and
needs a Netplan-only emergency rollback, NVIDIA's documented command sequence
is below. It moves the generated file rather than deleting it; `netplan try`
provides an interactive rollback window.

```bash
ssh -o BatchMode=yes -o ForwardAgent=no dgx-spark-1 \
  'sudo install -d -m 0700 /root/netplan-disabled && \
   sudo mv /etc/netplan/99-nvidia-sync-cluster.yaml /root/netplan-disabled/ && \
   sudo netplan generate && sudo netplan try'
ssh -o BatchMode=yes -o ForwardAgent=no dgx-spark-2 \
  'sudo install -d -m 0700 /root/netplan-disabled && \
   sudo mv /etc/netplan/99-nvidia-sync-cluster.yaml /root/netplan-disabled/ && \
   sudo netplan generate && sudo netplan try'
```

Do not run this rollback remotely without a working management path. Removing
Netplan alone does not remove the generated cluster SSH relationship.

## Selected manual CLI rollout

The official two-Spark playbook assigns the active `f1` function pair to two
point-to-point subnets. The staged plan uses the current Linux MTU/default,
`1500`; no jumbo-MTU value is assumed without primary evidence or a successful
live validation.

| Node | Interface | HCA | Planned IPv4 | MTU |
| --- | --- | --- | --- | --- |
| Spark 1/head | `enp1s0f1np1` | `rocep1s0f1` | `192.168.100.10/24` | 1500 |
| Spark 1/head | `enP2p1s0f1np1` | `roceP2p1s0f1` | `192.168.101.10/24` | 1500 |
| Spark 2/worker | `enp1s0f1np1` | `rocep1s0f1` | `192.168.100.11/24` | 1500 |
| Spark 2/worker | `enP2p1s0f1np1` | `roceP2p1s0f1` | `192.168.101.11/24` | 1500 |

`nodes/bin/configure-direct-fabric` is an audited, idempotent installer. It
only manages `/etc/netplan/99-dgx-spark-direct-fabric.yaml`, refuses to mix
with `99-nvidia-sync-cluster.yaml`, requires both functions to be up at
200000 Mb/s, sets `dhcp4: false` on both fabric interfaces, verifies that the
management default route is not on the fabric, and uses `netplan try` for both
installation and rollback. It has no SSH or private-key handling.

### Stage and inspect the worker first

The following first two commands only stage the reviewed script and run its
read-only preflight. They do not change either Spark. Do not add `-A` or enable
agent forwarding.

```bash
scp nodes/bin/configure-direct-fabric dgx-spark-2:/tmp/configure-direct-fabric
ssh -o BatchMode=yes -o ForwardAgent=no dgx-spark-2 \
  'bash /tmp/configure-direct-fabric --node spark2 --check'
```

If—and only if—the preflight prints a pass result with a Wi-Fi/10 GbE
management default route, the controller/user can make the first approved
change on the worker:

```bash
ssh -t -o BatchMode=yes -o ForwardAgent=no dgx-spark-2 \
  'sudo bash /tmp/configure-direct-fabric --node spark2 --apply'
```

Review `netplan try` at the console and accept only if management remains
reachable. If it is not accepted, it automatically rolls back. Do not continue
to Spark 1 if worker application, route preservation, or link checks fail.

After the worker result is recorded, repeat the same staged preflight and
interactive application for the head:

```bash
scp nodes/bin/configure-direct-fabric dgx-spark-1:/tmp/configure-direct-fabric
ssh -o BatchMode=yes -o ForwardAgent=no dgx-spark-1 \
  'bash /tmp/configure-direct-fabric --node spark1 --check'
ssh -t -o BatchMode=yes -o ForwardAgent=no dgx-spark-1 \
  'sudo bash /tmp/configure-direct-fabric --node spark1 --apply'
```

### Separate head-to-worker cluster key

Only after both Netplan applications and both postchecks pass, generate the
Ed25519 key on Spark 1. The private key never leaves Spark 1; only its public
key is transferred through the controller. This workflow does not copy the Mac
administration key and does not forward an agent.

```bash
ssh -o BatchMode=yes -o ForwardAgent=no dgx-spark-1 '
  set -euo pipefail
  key="$HOME/.ssh/dgx_spark_fabric_ed25519"
  test ! -e "$key" && test ! -e "$key.pub"
  umask 077
  ssh-keygen -q -t ed25519 -N "" -f "$key" \
    -C "spark1-to-spark2-fabric"
  cat "$key.pub"
' > /tmp/dgx_spark_fabric_ed25519.pub
```

Install the public key on Spark 2 with the two Spark 1 fabric addresses as
source restriction and OpenSSH's `restrict` option. `restrict` denies agent,
port, X11, and PTY forwarding while still permitting the noninteractive SSH
processes required for cluster work.

```bash
{
  printf 'restrict,from="192.168.100.10,192.168.101.10" '
  cat /tmp/dgx_spark_fabric_ed25519.pub
} | ssh -o BatchMode=yes -o ForwardAgent=no dgx-spark-2 '
  set -euo pipefail
  umask 077
  install -d -m 0700 "$HOME/.ssh"
  cat >> "$HOME/.ssh/authorized_keys"
  chmod 0600 "$HOME/.ssh/authorized_keys"
'
```

On Spark 1, create a narrow fabric-only alias (the user and home path must
match the live account):

```sshconfig
Host dgx-spark-2-fabric
    HostName 192.168.100.11
    User carst
    BindAddress 192.168.100.10
    IdentityFile ~/.ssh/dgx_spark_fabric_ed25519
    IdentitiesOnly yes
    ForwardAgent no
```

Verify from Spark 1 with `ssh -o ForwardAgent=no dgx-spark-2-fabric hostname`.
Do not add `Host *`, do not add the fabric key to an agent, and do not copy its
private component to Spark 2 or the Mac.

### Manual rollback

Run the same reviewed script from a working management session. It retains the
managed Netplan file under `/root/dgx-spark-fabric-rollback/` and uses
`netplan try`; it does not remove the head-only SSH key, which must be removed
separately only if the cluster relationship is intentionally dismantled.

```bash
ssh -t -o BatchMode=yes -o ForwardAgent=no dgx-spark-1 \
  'sudo bash /tmp/configure-direct-fabric --node spark1 --rollback'
ssh -t -o BatchMode=yes -o ForwardAgent=no dgx-spark-2 \
  'sudo bash /tmp/configure-direct-fabric --node spark2 --rollback'
```

## Post-success collection and acceptance

The controller must capture the following output from both nodes after the
manual configuration completes. It is read-only except for the already-approved
manual operation.

```bash
for host in dgx-spark-1 dgx-spark-2; do
  ssh -o BatchMode=yes -o ForwardAgent=no "$host" '
    sudo cat /etc/netplan/99-nvidia-sync-cluster.yaml
    ip -br link
    ip -br addr
    ip route
    rdma link show
    for d in /sys/class/infiniband/*; do
      for p in "$d"/ports/*; do
        [ -d "$p" ] || continue
        for g in "$p"/gids/[0-9]*; do
          [ -e "$g" ] || continue
          i=${g##*/}
          printf "%s/%s gid[%s]=%s type=%s netdev=%s\\n" \
            "${d##*/}" "${p##*/}" "$i" "$(cat "$g")" \
            "$(cat "$p/gid_attrs/types/$i" 2>/dev/null || true)" \
            "$(cat "$p/gid_attrs/ndevs/$i" 2>/dev/null || true)"
        done
      done
    done
  '
done
```

Run the script's exact bidirectional check from each node after both plans are
accepted; it validates route selection, no fabric default route, IPv4-to-RoCEv2
GID mapping, normal ping, and a non-fragmenting MTU-sized ping.

```bash
ssh -o BatchMode=yes -o ForwardAgent=no dgx-spark-2 \
  'sudo bash /tmp/configure-direct-fabric --node spark2 --postcheck'
ssh -o BatchMode=yes -o ForwardAgent=no dgx-spark-1 \
  'sudo bash /tmp/configure-direct-fabric --node spark1 --postcheck'
```

For every configured interface pair, verify the address listed in Netplan maps
to a non-link-local `RoCE v2` GID for that interface/HCA. Record **both**
interface/HCA/GID combinations: a physical DGX Spark QSFP link has two
functions, so a single `fabric_ip`, `interface`, `hca`, and `gid_index` field
is insufficient until the inventory model is extended or explicitly represents
both consumers.

Run this from each node for each corresponding peer address and interface,
substituting only values captured above. The IPv4 non-fragmenting payload is
the MTU minus the 20-byte IPv4 and 8-byte ICMP headers.

```bash
iface='<configured-interface>'
peer='<corresponding-peer-fabric-ip>'
mtu="$(cat "/sys/class/net/$iface/mtu")"
ping -I "$iface" -c 3 "$peer"
ping -I "$iface" -M do -s "$((mtu - 28))" -c 3 "$peer"
test -z "$(ip route show default dev "$iface")"
```

Only after those checks pass, record the exact values for every fabric function
in `inventory/cluster.toml` and `inventory/reports/fabric.json`, including the
resolved `NCCL_SOCKET_IFNAME`, `NCCL_IB_HCA`, `NCCL_IB_GID_INDEX`,
`TP_SOCKET_IFNAME`, and `GLOO_SOCKET_IFNAME`. Do not infer these from the
management LAN or from link-local GIDs.
