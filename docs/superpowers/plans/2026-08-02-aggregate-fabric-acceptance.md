# Aggregate DGX Spark Fabric Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove that the single physical ConnectX-7 link delivers NVIDIA's required 200 Gb/s-class performance by adding a simultaneous two-function RDMA gate of at least 184 Gb/s in both directions, plus reproducible latency and error-counter evidence.

**Architecture:** Keep the existing per-function RDMA and NCCL checks as diagnostics, but distinguish the two Linux/RoCE functions from the one physical QSFP link. Run both RDMA-write functions concurrently for each direction, require their same-interval aggregate to reach 184 Gb/s, enforce the documented per-function and NCCL regression floors, and capture fixed latency tests and RDMA counters around the live run.

**Tech Stack:** Python 3.12 standard library, `concurrent.futures`, pytest, NVIDIA perftest (`ib_write_bw`, `ib_read_bw`, `ib_write_lat`), `rdma`, OpenSSH, JSON, Markdown.

## Global Constraints

- One physical QSFP cable connects one 200000 Mb/s port on each Spark; the two Linux/RoCE functions are not two independent 200 Gb/s rails.
- Aggregate RDMA write bandwidth must be at least 184 Gb/s in Spark 1 to Spark 2 and Spark 2 to Spark 1 directions.
- A valid aggregate contains exactly two successful component measurements whose execution intervals overlap.
- Per-function write bandwidth must remain at least 98.01 Gb/s; per-function read bandwidth must remain at least 72.37 Gb/s.
- NCCL must select both RoCE HCAs and report at least 17.44 GB/s bus bandwidth.
- Latency uses a fixed command and records its distribution as a baseline; no absolute latency threshold is invented for the first accepted run.
- RDMA error counters are captured before and after the tests; growing retry, sequence, or timeout errors fail acceptance.
- Worker access continues through `dgx-spark-2-fabric`, with `BatchMode=yes`, `ForwardAgent=no`, and strict host-key checking.
- The validator remains non-mutating apart from bounded temporary perftest files that it removes.

---

### Task 1: Enforce per-function and simultaneous aggregate bandwidth

**Files:**
- Modify: `scripts/validate_fabric.py`
- Test: `tests/scripts/test_validate_fabric.py`

**Interfaces:**
- Produces: `run_aggregate_rdma_write(runner, server_host, client_host, *, base_port: int) -> dict[str, Any]`.
- Extends: `run_one_rdma(..., minimum_bandwidth_gbps: float) -> dict[str, Any]` with monotonic start/end timestamps in the result.
- Produces report records named `ib_write_bw:aggregate:<client>-><server>` with component results, overlap seconds, aggregate Gb/s, and the 184 Gb/s floor.

- [x] **Step 1: Write failing threshold and concurrency tests**

Add tests that prove a 97 Gb/s write fails the 98.01 floor, a 72 Gb/s read fails the 72.37 floor, a 17 GB/s NCCL result fails the 17.44 floor, and two concurrent 92.5 Gb/s components pass the 184 Gb/s aggregate gate. Use a `threading.Barrier(2)` in the injected component runner so a sequential implementation cannot pass accidentally.

```python
def test_aggregate_requires_two_overlapping_components(validate_module):
    barrier = threading.Barrier(2)

    def component(*args, **kwargs):
        barrier.wait(timeout=1)
        return {
            "passed": True,
            "bandwidth_gbps": 92.5,
            "started_monotonic": 10.0,
            "finished_monotonic": 12.0,
        }

    result = validate_module.run_aggregate_rdma_write(
        runner, worker, head, base_port=13000, run_component=component
    )
    assert result["aggregate_bandwidth_gbps"] == 185.0
    assert result["overlap_seconds"] == 2.0
```

- [x] **Step 2: Run the focused tests and confirm failure**

Run: `uv run --with pytest pytest tests/scripts/test_validate_fabric.py -v`

Expected: FAIL because the aggregate function, explicit floors, and interval fields do not exist.

- [x] **Step 3: Implement the minimal fail-closed aggregate gate**

Add constants for the four approved floors. Use `ThreadPoolExecutor(max_workers=2)` to run one `ib_write_bw` component per HCA concurrently. Record `time.monotonic()` immediately around each client call. Require exactly two results, a positive interval intersection, and an aggregate at or above 184 Gb/s. Apply the per-function floor inside `run_one_rdma` and the NCCL floor inside `run_nccl`.

```python
PHYSICAL_LINK_MIN_GBPS = 184.0
WRITE_FUNCTION_MIN_GBPS = 98.01
READ_FUNCTION_MIN_GBPS = 72.37
NCCL_MIN_GB_PER_SECOND = 17.44

overlap = min(item["finished_monotonic"] for item in components) - max(
    item["started_monotonic"] for item in components
)
if overlap <= 0:
    raise GateError("aggregate RDMA component intervals did not overlap")
aggregate = sum(item["bandwidth_gbps"] for item in components)
if aggregate < PHYSICAL_LINK_MIN_GBPS:
    raise GateError(f"aggregate RDMA write {aggregate:.2f} Gb/s is below 184.00 Gb/s")
```

- [x] **Step 4: Run the focused tests**

Run: `uv run --with pytest pytest tests/scripts/test_validate_fabric.py -v`

Expected: PASS, including low-bandwidth, non-overlap, partial-failure, and concurrent-pass cases.

- [x] **Step 5: Commit the executable gate**

```bash
git add scripts/validate_fabric.py tests/scripts/test_validate_fabric.py
git commit -m "fix: require aggregate Spark fabric bandwidth"
```

### Task 2: Capture fixed latency and RDMA error-counter evidence

**Files:**
- Modify: `scripts/validate_fabric.py`
- Test: `tests/scripts/test_validate_fabric.py`

**Interfaces:**
- Produces: `parse_rdma_latency(output: str) -> RDMALatencyResult`.
- Produces: `run_rdma_latency(runner, head, worker) -> list[dict[str, Any]]` for both functions and directions.
- Produces: `capture_rdma_counters(runner, head, worker) -> dict[str, Any]` before and after traffic.
- Adds top-level report fields `latency`, `rdma_counters_before`, and `rdma_counters_after`.

- [x] **Step 1: Write failing parser and counter-delta tests**

Use a saved representative `ib_write_lat` fixture with the fixed message size and iteration count. Assert extraction of minimum, maximum, typical, average, standard deviation, p99, and p99.9 microseconds. Add counter fixtures proving unchanged zeros pass and a growing `packet_seq_err`, `rnr_nak_retry_err`, or `local_ack_timeout_err` fails.

```python
def test_rejects_growing_rdma_error_counter(validate_module):
    before = {"packet_seq_err": 0}
    after = {"packet_seq_err": 1}
    with pytest.raises(validate_module.GateError, match="packet_seq_err"):
        validate_module.validate_counter_delta(before, after)
```

- [x] **Step 2: Run tests and confirm failure**

Run: `uv run --with pytest pytest tests/scripts/test_validate_fabric.py -v`

Expected: FAIL because latency and counter interfaces do not exist.

- [x] **Step 3: Implement fixed latency and counter capture**

Require `/usr/bin/ib_write_lat` during preflight. Run it with the pinned HCA, port, GID index 3, 8-byte payload, 10,000 iterations, and CPU-frequency warning suppression in both directions for both functions. Capture `rdma statistic show` before and after all RDMA traffic, preserve the raw command evidence, normalize only named error counters, and fail when any monitored counter grows.

- [x] **Step 4: Run focused and full offline tests**

Run: `uv run --with pytest pytest tests/scripts/test_validate_fabric.py tests/scripts -v && git diff --check`

Expected: PASS with no live SSH invocation from tests.

- [x] **Step 5: Commit latency and counter evidence**

```bash
git add scripts/validate_fabric.py tests/scripts/test_validate_fabric.py
git commit -m "feat: record Spark fabric latency and errors"
```

### Task 3: Run live acceptance and reconcile documentation

**Files:**
- Modify: `inventory/reports/rdma-nccl.json`
- Modify: `docs/runbooks/fabric.md`
- Modify: `docs/installation-record.md`
- Modify: `docs/architecture-overview.md`
- Modify: `docs/superpowers/specs/2026-08-01-dual-dgx-spark-platform-design.md`
- Modify: `docs/superpowers/specs/2026-08-02-multi-runtime-model-profiles-design.md`

**Interfaces:**
- Consumes: the updated `scripts/validate-fabric` wrapper and key-only Spark SSH aliases.
- Produces: a checked-in report proving or rejecting 184 Gb/s aggregate performance in both directions plus the first latency baseline.

- [x] **Step 1: Run the read-only preflight**

Run:

```bash
scripts/validate-fabric --inventory inventory/cluster.toml \
  --output /tmp/rdma-nccl-preflight.json --preflight-only
```

Expected: exit 0, both functions mapped to the pinned HCAs/GID 3, physical link speed 200000 Mb/s, MTU 1500, and no fabric default route.

- [x] **Step 2: Run the live aggregate acceptance**

Run:

```bash
scripts/validate-fabric --inventory inventory/cluster.toml \
  --output inventory/reports/rdma-nccl.json
```

Expected: aggregate writes at least 184 Gb/s in both directions, per-function diagnostics above their floors, both latency directions recorded, no growing monitored error counter, NCCL `NET/IB` on both HCAs, bus bandwidth at least 17.44 GB/s, and overall `status: passed`. If it fails, keep the failed report, leave distributed Model Definitions blocked, and diagnose without lowering a threshold.

- [x] **Step 3: Update the operational record from measured evidence**

Replace “two 200 Gb/s rails” with “one 200 Gb/s physical link exposed through two PCIe/RoCE functions.” Record the exact aggregate results, latency baseline, counter result, NCCL result, command, date, and NVIDIA's 184 Gb/s lower bound. Do not describe the link as accepted if either direction failed.

- [x] **Step 4: Verify documents and repository tests**

Run:

```bash
uv run --with pytest pytest tests -v
git diff --check
```

Also verify every local Markdown link in the changed documents resolves.

- [x] **Step 5: Commit the accepted evidence and documentation**

```bash
git add inventory/reports/rdma-nccl.json docs/runbooks/fabric.md \
  docs/installation-record.md docs/architecture-overview.md \
  docs/superpowers/specs/2026-08-01-dual-dgx-spark-platform-design.md \
  docs/superpowers/specs/2026-08-02-multi-runtime-model-profiles-design.md
git commit -m "docs: record aggregate Spark fabric acceptance"
```
