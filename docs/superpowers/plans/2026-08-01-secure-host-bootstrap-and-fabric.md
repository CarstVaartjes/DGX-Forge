# Secure Host Bootstrap and Fabric Implementation Plan

> **Status: completed.** The final sequence, observed results, and deviations
> from this original plan are recorded in the
> [installation record and lessons learned](../../installation-record.md).
> In particular, the accepted fabric used the pinned NVIDIA manual playbook,
> not Sync Cluster Assistant.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish key-only administration, a reproducible inventory, matched supported software, and a validated direct CX-7/RDMA/NCCL path between both Vonk Forge GPU nodes.

**Architecture:** The Mac administers both nodes with the dedicated 1Password SSH key. Read-only inventory is captured before mutations; GPU node 2 is updated and validated before GPU node 1. The point-to-point fabric is configured through an audited NVIDIA-supported path, and model work remains blocked until raw RDMA and NCCL tests pass.

**Tech Stack:** macOS OpenSSH, 1Password SSH agent and CLI, Bash, Python 3.12/pytest for script tests, JSON/TOML inventory, Vonk Forge Dashboard, pinned NVIDIA `vonk-node-playbooks`, `ib_write_bw`, NCCL tests.

## Global Constraints

- GPU node 1 is `carst@192.168.1.211`; GPU node 2 is `carst@192.168.1.212`.
- Do not disable password SSH until fresh 1Password-agent sessions pass on both nodes.
- Never copy private Mac key material to a GPU node and never enable SSH agent forwarding.
- Update GPU node 2 and validate it before updating GPU node 1.
- Record `earlyoom` state before stopping and disabling it on both nodes.
- The fabric has no default route and accepts traffic only between the two fabric peers.
- Do not install any non-AI container on either GPU node.
- Stop at the first failed safety or validation gate; do not continue on the other node.

---

### Task 1: Add the inventory collector and schema

**Files:**
- Create: `nodes/bin/collect-inventory`
- Create: `inventory/schema.json`
- Create: `tests/nodes/test_collect_inventory.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `collect-inventory` writes one JSON object to stdout and accepts no arguments.
- Produces: required JSON keys `hostname`, `boot_id`, `os`, `kernel`, `memory`, `swap`, `disks`, `earlyoom`, `nvidia`, `docker`, `interfaces`, `rdma`, and `thermal`.
- Consumed by: Task 3 and all later plans through `inventory/raw/node1-pre.json` and `inventory/raw/node2-pre.json`.

- [ ] **Step 1: Write a failing contract test**

```python
def test_inventory_has_required_sections(run_inventory):
    result = run_inventory()
    assert set(result) >= {
        "hostname", "boot_id", "os", "kernel", "memory", "swap", "disks",
        "earlyoom", "nvidia", "docker", "interfaces", "rdma", "thermal",
    }
    assert isinstance(result["interfaces"], list)
    assert isinstance(result["disks"], list)
```

- [ ] **Step 2: Run the test and verify the collector is absent**

Run: `pytest tests/nodes/test_collect_inventory.py -v`

Expected: FAIL because `nodes/bin/collect-inventory` does not exist.

- [ ] **Step 3: Implement the collector with read-only commands**

Use Bash to call `hostname`, `/etc/os-release`, `uname`, `/proc/meminfo`, `lsblk -J -b`, `df -B1`, `systemctl is-enabled/is-active earlyoom`, `nvidia-smi`, `docker version`, `docker compose version`, `ip -j`, `rdma -j`, `ibstat`, and available Vonk Forge thermal reporting. Assemble JSON with `jq -n`; return `null` for an unavailable optional command rather than failing the entire inventory.

```bash
#!/usr/bin/env bash
set -euo pipefail
jq -n \
  --arg hostname "$(hostname)" \
  --arg boot_id "$(cat /proc/sys/kernel/random/boot_id)" \
  --argjson memory "$(awk '/MemTotal|MemAvailable|SwapTotal|SwapFree/ {gsub(":", "", $1); printf "%s %s\n", $1, $2 * 1024}' /proc/meminfo | jq -Rn '[inputs|split(" ")|{(.[0]): (.[1]|tonumber)}]|add')" \
  '{hostname:$hostname, boot_id:$boot_id, memory:$memory}'
```

Expand this skeleton to every required section and validate the final object with `inventory/schema.json` in the test fixture.

- [ ] **Step 4: Run unit and static checks**

Run: `pytest tests/nodes/test_collect_inventory.py -v && shellcheck nodes/bin/collect-inventory`

Expected: all tests PASS and ShellCheck exits 0.

- [ ] **Step 5: Commit the collector**

```bash
git add .gitignore nodes/bin/collect-inventory inventory/schema.json tests/nodes/test_collect_inventory.py
git commit -m "feat: add GPU node inventory collector"
```

### Task 2: Install and configure the 1Password SSH public key

**Files:**
- Create: `config/ssh/vonk-node.conf.example`
- Create: `docs/runbooks/ssh-bootstrap.md`
- Create locally, never commit: `~/.ssh/vonk_node_admin.pub`

**Interfaces:**
- Produces: SSH aliases `vonk-node-1` and `vonk-node-2` used by every later command.
- Consumes: 1Password agent key whose comment is `Vonk Forge GPU node Admin`.

- [ ] **Step 1: Export only the public key from the 1Password agent**

```bash
agent_sock="$HOME/Library/Group Containers/2BUA8C4S2C.com.1password/t/agent.sock"
SSH_AUTH_SOCK="$agent_sock" ssh-add -L | grep ' Vonk Forge GPU node Admin$' > "$HOME/.ssh/vonk_node_admin.pub"
chmod 0644 "$HOME/.ssh/vonk_node_admin.pub"
ssh-keygen -lf "$HOME/.ssh/vonk_node_admin.pub"
```

Expected: fingerprint `SHA256:66yS2tf5iK+wvPkO44m++PWfI1q1BHS63BRMJqsPaqM` and no private-key file created.

- [ ] **Step 2: Install the public key on GPU node 1**

Run: `ssh-copy-id -i "$HOME/.ssh/vonk_node_admin.pub" carst@192.168.1.211`

Expected: one interactive Linux-password prompt followed by successful key installation.

- [ ] **Step 3: Install the public key on GPU node 2**

Run: `ssh-copy-id -i "$HOME/.ssh/vonk_node_admin.pub" carst@192.168.1.212`

Expected: one interactive Linux-password prompt followed by successful key installation.

- [ ] **Step 4: Write the SSH alias configuration**

```sshconfig
Host vonk-node-1
    HostName 192.168.1.211
    User carst
    IdentityAgent "~/Library/Group Containers/2BUA8C4S2C.com.1password/t/agent.sock"
    IdentityFile ~/.ssh/vonk_node_admin.pub
    IdentitiesOnly yes

Host vonk-node-2
    HostName 192.168.1.212
    User carst
    IdentityAgent "~/Library/Group Containers/2BUA8C4S2C.com.1password/t/agent.sock"
    IdentityFile ~/.ssh/vonk_node_admin.pub
    IdentitiesOnly yes
```

Save the committed example and install the same content under an included local SSH config path.

- [ ] **Step 5: Verify fresh non-password sessions**

Run: `ssh -o BatchMode=yes vonk-node-1 'hostname' && ssh -o BatchMode=yes vonk-node-2 'hostname'`

Expected: both hostnames print with exit code 0 and 1Password approves the key if requested.

- [ ] **Step 6: Commit the SSH runbook**

```bash
git add config/ssh/vonk-node.conf.example docs/runbooks/ssh-bootstrap.md
git commit -m "docs: add Vonk Forge GPU node SSH bootstrap"
```

### Task 3: Capture and normalize the pre-change inventory

**Files:**
- Create: `inventory/raw/node1-pre.json`
- Create: `inventory/raw/node2-pre.json`
- Create: `inventory/cluster.toml`
- Create: `docs/runbooks/inventory.md`

**Interfaces:**
- Produces: `inventory/cluster.toml` with `[hosts.node1]`, `[hosts.node2]`, and `[fabric]` tables.
- Required fields: `hostname`, `ssh_alias`, `lan_ip`, `memory_bytes`, `root_disk_bytes`, `root_free_bytes`, `driver`, `docker`, `compose`, `earlyoom_active`, and `earlyoom_enabled`.

- [ ] **Step 1: Copy and run the collector on both nodes**

```bash
scp nodes/bin/collect-inventory vonk-node-1:/tmp/collect-inventory
scp nodes/bin/collect-inventory vonk-node-2:/tmp/collect-inventory
ssh vonk-node-1 'bash /tmp/collect-inventory' > inventory/raw/node1-pre.json
ssh vonk-node-2 'bash /tmp/collect-inventory' > inventory/raw/node2-pre.json
```

- [ ] **Step 2: Validate both JSON documents**

Run: `pytest tests/nodes/test_collect_inventory.py -v --inventory-dir inventory/raw`

Expected: both documents satisfy `inventory/schema.json`.

- [ ] **Step 3: Create `inventory/cluster.toml` from observed values**

```toml
[hosts.node1]
role = "head"
ssh_alias = "vonk-node-1"
lan_ip = "192.168.1.211"

[hosts.node2]
role = "worker"
ssh_alias = "vonk-node-2"
lan_ip = "192.168.1.212"

[fabric]
topology = "direct"
default_route = false
```

Add the measured values listed in the interface block. Do not invent fabric interface values before Cluster Assistant produces them.

- [ ] **Step 4: Check the numeric preconditions**

Run a small `jq` check that requires at least 100 GiB `MemAvailable`, no more than 1 GiB swap used, and at least 350 GiB free disk on each node. If disk is below the threshold, stop and record the exact deficit; do not delete anything automatically.

- [ ] **Step 5: Commit the pre-change inventory**

```bash
git add inventory docs/runbooks/inventory.md
git commit -m "docs: record pre-change GPU node inventory"
```

### Task 4: Harden SSH after key verification

**Files:**
- Create: `nodes/etc/ssh/sshd_config.d/90-vonk-admin.conf`
- Create: `docs/runbooks/ssh-recovery.md`

**Interfaces:**
- Produces: key-only SSH with local Vonk Forge Dashboard/console recovery documented.

- [ ] **Step 1: Add the managed drop-in**

```text
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
PermitRootLogin prohibit-password
```

- [ ] **Step 2: Install and syntax-check GPU node 2 first**

Run: `scp nodes/etc/ssh/sshd_config.d/90-vonk-admin.conf vonk-node-2:/tmp/ && ssh -t vonk-node-2 'sudo install -m 0644 /tmp/90-vonk-admin.conf /etc/ssh/sshd_config.d/90-vonk-admin.conf && sudo sshd -t'`

Expected: `sshd -t` exits 0. If it fails, remove only the new drop-in through the still-open session.

- [ ] **Step 3: Reload and verify GPU node 2**

Run: `ssh vonk-node-2 'sudo systemctl reload ssh' && ssh -o BatchMode=yes vonk-node-2 true`

Negative check: `ssh -o PubkeyAuthentication=no -o KbdInteractiveAuthentication=no -o PasswordAuthentication=yes -o BatchMode=yes vonk-node-2 true`

Expected: key check exits 0; negative check exits nonzero.

- [ ] **Step 4: Repeat syntax, reload, positive, and negative checks on GPU node 1**

Use the same commands with `vonk-node-1`. Keep an existing verified session open until the fresh session succeeds.

- [ ] **Step 5: Commit SSH hardening artifacts**

```bash
git add nodes/etc/ssh/sshd_config.d/90-vonk-admin.conf docs/runbooks/ssh-recovery.md
git commit -m "security: define key-only GPU node SSH"
```

### Task 5: Update both GPU nodes sequentially

**Files:**
- Create: `inventory/raw/node2-post-update.json`
- Create: `inventory/raw/node1-post-update.json`
- Create: `docs/runbooks/platform-update.md`

**Interfaces:**
- Consumes: NVIDIA Vonk Forge Dashboard and the current official Vonk Forge GPU node update/release notes.
- Produces: two matched post-update inventories and an update record with timestamps and release versions.

- [ ] **Step 1: Record release notes and recovery constraints**

Add the target Vonk Forge OS, driver, CUDA, firmware, and container-runtime versions plus the official release-note URLs to the runbook. Mark firmware rollback as unavailable unless NVIDIA documents a recovery procedure for the installed version.

- [ ] **Step 2: Update GPU node 2 through Vonk Forge Dashboard**

Put the platform in maintenance, update only GPU node 2, reboot it, and wait for key SSH and Dashboard access to return.

- [ ] **Step 3: Validate GPU node 2 before touching GPU node 1**

Run the collector and verify `nvidia-smi`, `docker run --rm --gpus all` with an NVIDIA ARM64 CUDA image, filesystem health, and interface visibility. Store the result as the GPU node 2 post-update inventory. Stop if any check fails.

- [ ] **Step 4: Update and validate GPU node 1**

Repeat the Dashboard update, reboot, collector, GPU-container, storage, and interface checks on GPU node 1.

- [ ] **Step 5: Compare versions exactly**

Use `jq` to compare Vonk Forge OS, kernel, driver, CUDA, Docker, and Compose values. Expected: no differences in the matched platform fields.

- [ ] **Step 6: Commit the update record**

```bash
git add inventory/raw docs/runbooks/platform-update.md
git commit -m "docs: record matched GPU node platform update"
```

### Task 6: Disable `earlyoom` with recorded evidence

**Files:**
- Create: `inventory/reports/earlyoom.json`
- Modify: `docs/runbooks/platform-update.md`

**Interfaces:**
- Produces: per-node before/after `enabled` and `active` states.

- [ ] **Step 1: Capture current state on both nodes**

Run: `ssh vonk-node-1 'systemctl is-enabled earlyoom; systemctl is-active earlyoom'` and the equivalent on GPU node 2. Record exit codes as well as text.

- [ ] **Step 2: Stop and disable GPU node 2, then GPU node 1**

Run: `ssh -t vonk-node-2 'sudo systemctl stop earlyoom && sudo systemctl disable earlyoom'`

Then run the same command on GPU node 1.

- [ ] **Step 3: Assert the service is not active or enabled**

Expected on each node: `systemctl is-active earlyoom` prints `inactive` and `systemctl is-enabled earlyoom` prints `disabled`; unexpected states fail the task.

- [ ] **Step 4: Commit evidence**

```bash
git add inventory/reports/earlyoom.json docs/runbooks/platform-update.md
git commit -m "ops: record earlyoom disablement"
```

### Task 7: Configure and inventory the direct CX-7 fabric

**Files:**
- Modify: `inventory/cluster.toml`
- Create: `inventory/reports/fabric.json`
- Create: `docs/runbooks/fabric.md`

**Interfaces:**
- Produces: exact `fabric_ip`, `interface`, `hca`, `gid_index`, `mtu`, `link_rate`, `NCCL_SOCKET_IFNAME`, `NCCL_IB_HCA`, `NCCL_IB_GID_INDEX`, `TP_SOCKET_IFNAME`, and `GLOO_SOCKET_IFNAME` for each node.

- [ ] **Step 1: Verify cable identity and physical link**

Record the cable part number and supported rate. Use `ethtool`, `ibstat`, and the Vonk Forge Dashboard to confirm both ends see the link. Stop if the cable cannot support the intended configuration.

- [ ] **Step 2: Run NVIDIA Sync Cluster Assistant**

Generate a separate GPU node 1-to-GPU node 2 cluster key on GPU node 1; never reuse the Mac administration key and never forward the Mac agent. Restrict the worker-side public key to the fabric source where Cluster Assistant supports it. Use GPU node 1 as head and GPU node 2 as worker, select the directly connected CX-7 ports, accept only a no-default-route point-to-point fabric, and save the assistant's report. If Cluster Assistant cannot complete, stop it and follow NVIDIA's official manual two-GPU node playbook while preserving the same key separation and inventory outputs.

- [ ] **Step 3: Record resolved fabric consumers**

Populate both host tables in `inventory/cluster.toml`. Derive RoCEv2 GID indexes from sysfs and verify each selected GID maps to the recorded fabric IPv4 address.

- [ ] **Step 4: Verify routing and MTU**

Run bidirectional `ping` with normal and non-fragmenting MTU-sized packets over the fabric. Run `ip route` and assert there is no default route on the fabric interface.

- [ ] **Step 5: Commit the fabric inventory**

```bash
git add inventory/cluster.toml inventory/reports/fabric.json docs/runbooks/fabric.md
git commit -m "ops: record direct CX-7 fabric"
```

### Task 8: Pass RDMA and NCCL acceptance gates

**Files:**
- Create: `scripts/validate-fabric`
- Create: `tests/scripts/test_validate_fabric.py`
- Create: `inventory/reports/rdma-nccl.json`
- Modify: `docs/runbooks/fabric.md`

**Interfaces:**
- Consumes: `inventory/cluster.toml`.
- Produces: JSON results for bidirectional RDMA write/read bandwidth and NCCL all-reduce.

- [ ] **Step 1: Write failing parser tests for benchmark output**

```python
def test_rejects_tcp_fallback(parse_nccl):
    result = parse_nccl("NET/Socket : Using enp...\nAvg bus bandwidth : 11.0")
    assert result.passed is False

def test_accepts_ib_transport(parse_nccl):
    result = parse_nccl("NET/IB : Using rocep1s0f1\nAvg bus bandwidth : 20.0")
    assert result.transport == "IB"
```

- [ ] **Step 2: Run tests and verify failure**

Run: `pytest tests/scripts/test_validate_fabric.py -v`

Expected: FAIL because the validation parser does not exist.

- [ ] **Step 3: Implement the validation wrapper**

The script reads TOML, starts `ib_write_bw` and `ib_read_bw` servers remotely, runs clients in both directions, then launches NVIDIA `all_reduce_perf` with the recorded NCCL variables. It must fail if logs show `NET/Socket`, mismatched HCA/GID consumers, nonzero benchmark exits, or no measured bandwidth.

- [ ] **Step 4: Run parser/static tests**

Run: `pytest tests/scripts/test_validate_fabric.py -v && shellcheck scripts/validate-fabric`

Expected: PASS.

- [ ] **Step 5: Run live fabric acceptance**

Run: `scripts/validate-fabric --inventory inventory/cluster.toml --output inventory/reports/rdma-nccl.json`

Expected: transport `IB`, both RDMA directions pass, NCCL all-reduce exits 0, and no socket fallback appears.

- [ ] **Step 6: Commit the validated substrate**

```bash
git add scripts/validate-fabric tests/scripts/test_validate_fabric.py inventory/reports/rdma-nccl.json docs/runbooks/fabric.md
git commit -m "test: validate GPU node RDMA and NCCL fabric"
```
