# Pre-change inventory

This runbook records the read-only baseline captured before SSH hardening,
updates, or fabric configuration. Run it only through the key-authenticated
aliases from [SSH bootstrap](ssh-bootstrap.md). Do not use passwords, bypass
host-key checking, or run the collector with `sudo`.

The committed raw documents intentionally exclude serial numbers, machine IDs,
SSH host keys, and private-key material. They do contain operational network
metadata such as interface addresses, so this repository must remain private.

## Capture

Stream the collector over SSH so it does not create or modify a file on either
Spark:

```bash
set -euo pipefail
mkdir -p inventory/raw
ssh -o BatchMode=yes dgx-spark-1 'bash -s' \
  < nodes/bin/collect-inventory > inventory/raw/spark1-pre.json
ssh -o BatchMode=yes dgx-spark-2 'bash -s' \
  < nodes/bin/collect-inventory > inventory/raw/spark2-pre.json
```

Validate both captures against `inventory/schema.json`:

```bash
uv run --with pytest --with jsonschema \
  pytest tests/nodes/test_collect_inventory.py -v \
  --inventory-dir inventory/raw
```

`inventory/cluster.toml` is the normalized consumer-facing inventory. Populate
it only from the raw documents or supplementary read-only probes. Do not add a
fabric interface, address, MTU, RDMA device, GID index, or NCCL setting until
Cluster Assistant has configured and verified it.

The NVIDIA driver and Docker client versions require supplementary probes on
DGX Spark because the raw collector cannot currently normalize unified GPU
memory and the unprivileged account cannot query the Docker daemon:

```bash
for host in dgx-spark-1 dgx-spark-2; do
  ssh -o BatchMode=yes "$host" '
    nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n1
    docker --version
    docker compose version --short
  '
done
```

In `cluster.toml`, `memory_bytes` maps to `memory.total_bytes`, and the root
disk fields map to the size and available bytes for the `/` filesystem.
`inactive` and `not-found` map to false for the two earlyoom booleans. The
Docker field records the observed client version, not an assertion that the
daemon is reachable.

## Numeric preconditions

Each node must have at least 100 GiB available memory, no more than 1 GiB of
swap in use, and at least 350 GiB free on the root filesystem. This check prints
the measured values and exact disk deficit before failing; it never deletes or
changes data.

```bash
set -euo pipefail
minimum_memory=$((100 * 1024 * 1024 * 1024))
maximum_swap_used=$((1 * 1024 * 1024 * 1024))
minimum_root_free=$((350 * 1024 * 1024 * 1024))

for inventory_file in inventory/raw/spark1-pre.json \
  inventory/raw/spark2-pre.json; do
  jq -e \
    --argjson minimum_memory "$minimum_memory" \
    --argjson maximum_swap_used "$maximum_swap_used" \
    --argjson minimum_root_free "$minimum_root_free" '
      . as $inventory
      | (.swap.total_bytes - .swap.free_bytes) as $swap_used
      | (.filesystems[] | select(.mountpoint == "/")) as $root
      | {
          hostname,
          memory_available_bytes: .memory.available_bytes,
          swap_used_bytes: $swap_used,
          root_free_bytes: $root.available_bytes,
          root_free_deficit_bytes:
            ([0, ($minimum_root_free - $root.available_bytes)] | max)
        } as $result
      | $result,
        ($inventory.memory.available_bytes >= $minimum_memory
         and $swap_used <= $maximum_swap_used
         and $root.available_bytes >= $minimum_root_free)
    ' "$inventory_file" | tee /dev/stderr | tail -n 1 | grep -qx true
done
```

If any condition fails, stop and record the printed values. In particular, do
not delete images, caches, models, or user data to recover disk space.

## Observed baseline on 2026-08-01

| Measurement | Spark 1 | Spark 2 |
| --- | ---: | ---: |
| Hostname | `spark-3542` | `spark-2297` |
| LAN address | `192.168.1.211` | `192.168.1.212` |
| Total memory | 130,663,231,488 B | 130,663,231,488 B |
| Available memory | 126,990,147,584 B | 126,946,283,520 B |
| Swap used | 0 B | 0 B |
| Root filesystem size | 4,031,871,553,536 B | 4,031,871,553,536 B |
| Root filesystem free | 3,787,009,835,008 B | 3,786,993,606,656 B |
| NVIDIA driver | `580.173.02` | `580.173.02` |
| Docker client | `29.2.1` | `29.2.1` |
| Docker Compose | `5.0.2` | `5.0.2` |
| earlyoom | not installed; inactive | not installed; inactive |
| Temperature at capture | 38 C | 38 C |

Both nodes passed all three numeric preconditions. The LAN interfaces reported
the expected addresses but marked them dynamic at the operating-system layer;
the static behavior is therefore presumed to come from DHCP reservations and
must be confirmed at the router before relying on it.

The collector recorded `nvidia = null` because DGX Spark reports GPU memory as
`N/A` for unified memory and the current parser rejects that value. Driver and
temperature were confirmed separately with read-only `nvidia-smi` queries.
The collector also recorded a null Docker engine object because `carst` could
not access `/var/run/docker.sock`; `docker --version` confirmed the client
version, but daemon health remains unverified until privileged bootstrap.

The collector returned `rdma = null`, so RDMA tooling or device state remains
unverified, and no fabric address was configured at capture time. Two wired
ports had carrier but no address on each node. These are observations only;
later fabric discovery must identify the actual cabled ports before changing
their configuration.
