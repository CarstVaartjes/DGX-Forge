from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADAPTER = ROOT / "adapters/deepseek/mia-vllm/bin/mia-deepseek-dual"
IMAGE = (
    "ghcr.io/anemll/dspark-vllm-gx10"
    "@sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8"
)


def render(role: str) -> str:
    hostname = "spark-3542" if role == "head" else "spark-2297"
    completed = subprocess.run(
        [str(ADAPTER), "render", role],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "MIA_LOCAL_HOSTNAME": hostname},
    )
    return completed.stdout


def test_head_render_pins_mia_runtime_and_loopback_api() -> None:
    rendered = render("head")

    assert IMAGE in rendered
    assert "network_mode: host" in rendered
    assert "ipc: host" in rendered
    assert 'shm_size: "68719476736"' in rendered
    assert "restart: \"no\"" in rendered
    assert "source: /dev/infiniband" in rendered
    assert "target: /dev/infiniband" in rendered
    assert "memlock: -1" in rendered
    assert "stack: 67108864" in rendered
    assert "NODE_RANK: \"0\"" in rendered
    assert "VLLM_HOST_IP: 192.168.100.10" in rendered
    assert "--host 127.0.0.1" in rendered
    assert "--port 8888" in rendered
    assert "--node-rank" in rendered
    assert "--headless" not in rendered


def test_worker_render_is_headless_rank_one() -> None:
    rendered = render("worker")

    assert "NODE_RANK: \"1\"" in rendered
    assert "VLLM_HOST_IP: 192.168.100.11" in rendered
    assert "--headless" in rendered
    assert "HEADLESS: \"0\"" not in rendered


def test_render_pins_model_mounts_fabric_and_vllm_arguments() -> None:
    rendered = render("worker")

    required = (
        "source: /srv/models/snapshots/deepseek-v4-flash-0731",
        "target: /models/deepseek-ai/DeepSeek-V4-Flash-0731",
        "read_only: true",
        "source: /srv/models/runtime-cache/deepseek-agent-dual",
        "target: /runtime-cache",
        "DSPARK_MODEL: /models/deepseek-ai/DeepSeek-V4-Flash-0731",
        "DSPARK_ENCODING_FILE: /models/deepseek-ai/DeepSeek-V4-Flash-0731/encoding/encoding_dsv4.py",
        "VLLM_CACHE_ROOT: /runtime-cache/vllm",
        "FLASHINFER_WORKSPACE_BASE: /runtime-cache/flashinfer",
        "HF_HOME: /runtime-cache/huggingface",
        "NCCL_SOCKET_IFNAME: =enp1s0f1np1,enP2p1s0f1np1",
        "NCCL_IB_HCA: =rocep1s0f1:1,roceP2p1s0f1:1",
        "NCCL_IB_GID_INDEX: \"3\"",
        "MASTER_ADDR: 192.168.100.10",
        "MASTER_PORT: \"25000\"",
        "--tensor-parallel-size 2",
        "--pipeline-parallel-size 1",
        "--distributed-executor-backend mp",
        "--kv-cache-dtype nvfp4_ds_mla",
        "--max-model-len 1048576",
        "--max-num-seqs 6",
        "--max-num-batched-tokens 8192",
        "--gpu-memory-utilization 0.80",
        "--tokenizer-mode deepseek_v4",
        "--moe-backend flashinfer_b12x",
        "--tool-call-parser deepseek_v4",
        "--reasoning-parser deepseek_v4",
        "--generation-config vllm",
        "--enable-flashinfer-autotune",
    )
    for value in required:
        assert value in rendered
    assert '"method":"dspark","num_speculative_tokens":5' in rendered
    assert '"reasoning_effort":"low"' in rendered


def test_render_rejects_unknown_role() -> None:
    completed = subprocess.run(
        [str(ADAPTER), "render", "leader"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "role must be head or worker" in completed.stderr


def test_render_rejects_a_valid_role_on_the_wrong_physical_node() -> None:
    completed = subprocess.run(
        [str(ADAPTER), "render", "head"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "MIA_LOCAL_HOSTNAME": "spark-2297"},
    )

    assert completed.returncode == 2
    assert "head requires spark1/spark-3542" in completed.stderr


def fake_command(tmp_path: Path, name: str, body: str) -> Path:
    command = tmp_path / name
    command.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body, encoding="utf-8")
    command.chmod(0o755)
    return command


def _prepare_environment(
    tmp_path: Path,
    *,
    container_state: str = "absent",
    runtime_label: str = "a" * 64,
    container_image: str = IMAGE,
) -> tuple[dict[str, str], Path]:
    models_root = tmp_path / "models"
    docker_log = tmp_path / "docker.log"
    verifier_log = tmp_path / "verifier.log"
    snapshot = models_root / "snapshots/deepseek-v4-flash-0731"
    docker = fake_command(
        tmp_path,
        "docker",
        f'''printf "%s\\n" "$*" >> {docker_log!s}
if [[ ${{1:-}} == image && ${{2:-}} == inspect ]]; then
  if [[ $* == *--format* ]]; then printf "%s\n" "$FAKE_PINNED_IMAGE_ID"; fi
  exit 0
fi
if [[ ${{1:-}} == container && ${{2:-}} == inspect ]]; then
  [[ ${{FAKE_CONTAINER_STATE}} != absent ]]
  exit
fi
if [[ ${{1:-}} == inspect && ${{2:-}} == --format ]]; then
  case ${{3:-}} in
    *runtime-release*) printf "%s\\n" "$FAKE_RUNTIME_LABEL" ;;
    *checkpoint-manifest*) printf "%s\\n" "82e965c1caa019b31f4d776d0b3eddb0cc0d8e076f189822b8a3bbe3fa115121" ;;
    *prepare-fingerprint*) printf "%s\\n" "$FAKE_PREPARE_FINGERPRINT" ;;
    *Config.Image*) printf "%s\\n" "$FAKE_CONTAINER_IMAGE" ;;
    *.Image*) printf "%s\\n" "$FAKE_CONTAINER_IMAGE_ID" ;;
    *State.Status*) printf "%s\\n" "$FAKE_CONTAINER_STATE" ;;
    *State.ExitCode*) printf "0\\n" ;;
  esac
  exit 0
fi
if [[ ${{1:-}} == wait ]]; then
  mkdir -p -- "$FAKE_SNAPSHOT"
  printf "0\\n"
fi
''',
    )
    df = fake_command(
        tmp_path,
        "df",
        'printf "Filesystem 1-blocks Used Available Capacity Mounted on\\n"\n'
        'printf "fake 1000000000000 1 999999999999 1%% /srv/models\\n"\n',
    )
    verifier = tmp_path / "model_manifest.py"
    verifier.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        f"Path({str(verifier_log)!r}).write_text(' '.join(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    checkpoint_digest = (
        "82e965c1caa019b31f4d776d0b3eddb0cc0d8e076f189822b8a3bbe3fa115121"
    )
    release_digest = "a" * 64
    prepare_fingerprint = hashlib.sha256(
        f"{release_digest}:{checkpoint_digest}".encode()
    ).hexdigest()
    return (
        {
            **os.environ,
            "MIA_DOCKER_BIN": str(docker),
            "MIA_DF_BIN": str(df),
            "MIA_MODEL_MANIFEST_TOOL": str(verifier),
            "MIA_MODELS_ROOT": str(models_root),
            "MIA_LOCAL_HOSTNAME": "spark-2297",
            "MIA_RELEASE_SHA256": release_digest,
            "FAKE_CONTAINER_STATE": container_state,
            "FAKE_RUNTIME_LABEL": runtime_label,
            "FAKE_CONTAINER_IMAGE": container_image,
            "FAKE_CONTAINER_IMAGE_ID": "sha256:" + "c" * 64,
            "FAKE_PINNED_IMAGE_ID": "sha256:" + "c" * 64,
            "FAKE_PREPARE_FINGERPRINT": prepare_fingerprint,
            "FAKE_SNAPSHOT": str(snapshot),
        },
        docker_log,
    )


def test_prepare_starts_a_durable_exact_revision_job(tmp_path: Path) -> None:
    environment, docker_log = _prepare_environment(tmp_path)

    completed = subprocess.run(
        [str(ADAPTER), "prepare", "worker"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.stdout == "prepared role=worker\n"
    models_root = Path(environment["MIA_MODELS_ROOT"])
    assert (models_root / "runtime-cache/deepseek-agent-dual/tmp").is_dir()
    assert (models_root / "runtime-cache/deepseek-agent-dual/prepare").is_dir()
    assert (models_root / "snapshots/deepseek-v4-flash-0731").is_dir()
    assert (models_root / "manifests/deepseek-v4-flash-0731.json").read_bytes() == (
        ROOT / "manifests/deepseek-v4-flash-0731.json"
    ).read_bytes()
    calls = docker_log.read_text(encoding="utf-8")
    assert "image inspect " + IMAGE in calls
    assert "run --detach --name mia-deepseek-dual-prepare" in calls
    assert "--restart no" in calls
    assert "--pull never" in calls
    assert "--network bridge" in calls
    assert "--read-only" in calls
    assert "--cap-drop ALL" in calls
    assert "--security-opt no-new-privileges" in calls
    assert "--user " in calls
    assert "NVIDIA_VISIBLE_DEVICES=void" in calls
    assert "spark.runtime-release=" + "a" * 64 in calls
    assert "spark.checkpoint-manifest=82e965" in calls
    assert "HF_HUB_DISABLE_XET=1" in calls
    assert "HF_HUB_DISABLE_SYMLINKS=1" in calls
    assert "deepseek-ai/DeepSeek-V4-Flash-0731" in calls
    assert "9e165c30e2704aec5d9d593cce3eebd58bbef1cb" in calls
    assert '"max_workers":1' in calls
    assert '"max_workers":8' not in calls
    assert "local_dir_use_symlinks" in calls
    assert "METADATA_DIR=/snapshots/.deepseek-v4-flash-0731.metadata-" in calls
    assert r'\"$STAGING_DIR' not in calls
    assert 'rm -rf -- "$STAGING_DIR/.cache"' not in calls
    assert 'mv -- "$STAGING_DIR/.cache" "$METADATA_DIR"' in calls
    assert "--gpus" not in calls
    assert "wait mia-deepseek-dual-prepare" in calls


def test_prepare_reattaches_to_the_matching_running_job(tmp_path: Path) -> None:
    environment, docker_log = _prepare_environment(
        tmp_path, container_state="running"
    )

    completed = subprocess.run(
        [str(ADAPTER), "prepare", "worker"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.stdout == "prepared role=worker\n"
    calls = docker_log.read_text(encoding="utf-8")
    assert "wait mia-deepseek-dual-prepare" in calls
    assert "run --detach" not in calls
    assert "start mia-deepseek-dual-prepare" not in calls


def test_prepare_refuses_a_job_from_a_different_release(tmp_path: Path) -> None:
    environment, docker_log = _prepare_environment(
        tmp_path,
        container_state="running",
        runtime_label="b" * 64,
    )

    completed = subprocess.run(
        [str(ADAPTER), "prepare", "worker"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 1
    assert "preparation container fingerprint mismatch" in completed.stderr
    calls = docker_log.read_text(encoding="utf-8")
    assert "run --detach" not in calls
    assert "wait mia-deepseek-dual-prepare" not in calls


def test_prepare_refuses_a_matching_job_that_uses_another_image(
    tmp_path: Path,
) -> None:
    environment, docker_log = _prepare_environment(
        tmp_path,
        container_state="running",
        container_image="ghcr.io/example/wrong@sha256:" + "d" * 64,
    )

    completed = subprocess.run(
        [str(ADAPTER), "prepare", "worker"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 1
    assert "preparation container image mismatch" in completed.stderr
    calls = docker_log.read_text(encoding="utf-8")
    assert "start mia-deepseek-dual-prepare" not in calls
    assert "wait mia-deepseek-dual-prepare" not in calls


def test_vendored_checkpoint_manifest_matches_the_checked_manifest() -> None:
    assert (
        ROOT
        / "adapters/deepseek/mia-vllm/manifests/deepseek-v4-flash-0731.json"
    ).read_bytes() == (ROOT / "manifests/deepseek-v4-flash-0731.json").read_bytes()


def test_start_and_stop_control_only_the_local_role(tmp_path: Path) -> None:
    log = tmp_path / "docker.log"
    docker = fake_command(tmp_path, "docker", f'printf "%s\\n" "$*" >> {log!s}\n')
    environment = {
        **os.environ,
        "MIA_DOCKER_BIN": str(docker),
    }

    subprocess.run(
        [str(ADAPTER), "start", "worker"],
        cwd=ROOT,
        check=True,
        env={**environment, "MIA_LOCAL_HOSTNAME": "spark-2297"},
    )
    subprocess.run(
        [str(ADAPTER), "stop", "head"],
        cwd=ROOT,
        check=True,
        env={**environment, "MIA_LOCAL_HOSTNAME": "spark-3542"},
    )

    calls = log.read_text(encoding="utf-8")
    assert "spark2.env" in calls
    assert "up --detach --no-build --pull never --remove-orphans" in calls
    assert "spark1.env" in calls
    assert "down --timeout 120 --remove-orphans" in calls


def _verification_fixture(tmp_path: Path) -> dict[str, str]:
    models_root = tmp_path / "models"
    manifest_dir = models_root / "manifests"
    snapshot = models_root / "snapshots/deepseek-v4-flash-0731"
    manifest_dir.mkdir(parents=True)
    snapshot.mkdir(parents=True)
    shutil.copyfile(
        ROOT / "manifests/deepseek-v4-flash-0731.json",
        manifest_dir / "deepseek-v4-flash-0731.json",
    )

    sys_class_net = tmp_path / "sys/class/net"
    rails = (
        (
            "enp1s0f1np1",
            "192.168.100.11",
            "rocep1s0f1",
            "0000:0000:0000:0000:0000:ffff:c0a8:640b",
        ),
        (
            "enP2p1s0f1np1",
            "192.168.101.11",
            "roceP2p1s0f1",
            "0000:0000:0000:0000:0000:ffff:c0a8:650b",
        ),
    )
    sys_class_infiniband = tmp_path / "sys/class/infiniband"
    for interface, _ip, hca, gid in rails:
        interface_dir = sys_class_net / interface
        interface_dir.mkdir(parents=True)
        (interface_dir / "mtu").write_text("1500\n", encoding="utf-8")
        (interface_dir / "operstate").write_text("up\n", encoding="utf-8")
        port = sys_class_infiniband / hca / "ports/1"
        (port / "gid_attrs/ndevs").mkdir(parents=True)
        (port / "gid_attrs/types").mkdir(parents=True)
        (port / "gids").mkdir(parents=True)
        (port / "state").write_text("4: ACTIVE\n", encoding="utf-8")
        (port / "gid_attrs/ndevs/3").write_text(interface + "\n", encoding="utf-8")
        (port / "gid_attrs/types/3").write_text("RoCE v2\n", encoding="utf-8")
        (port / "gids/3").write_text(gid + "\n", encoding="utf-8")

    docker = fake_command(tmp_path, "docker", ":\n")
    ip = fake_command(
        tmp_path,
        "ip",
        'case "$*" in\n'
        '  *enp1s0f1np1) printf "2: enp1s0f1np1 inet 192.168.100.11/24 scope global enp1s0f1np1\\n" ;;\n'
        '  *enP2p1s0f1np1) printf "3: enP2p1s0f1np1 inet 192.168.101.11/24 scope global enP2p1s0f1np1\\n" ;;\n'
        'esac\n',
    )
    verifier_log = tmp_path / "verifier.log"
    verifier = tmp_path / "model_manifest.py"
    verifier.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        f"Path({str(verifier_log)!r}).write_text(' '.join(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    return {
        **os.environ,
        "MIA_DOCKER_BIN": str(docker),
        "MIA_IP_BIN": str(ip),
        "MIA_LOCAL_HOSTNAME": "spark-2297",
        "MIA_MODELS_ROOT": str(models_root),
        "MIA_MODEL_MANIFEST_TOOL": str(verifier),
        "MIA_SYS_CLASS_NET": str(sys_class_net),
        "MIA_SYS_CLASS_INFINIBAND": str(sys_class_infiniband),
        "VERIFIER_LOG": str(verifier_log),
    }


def test_verify_checks_the_exact_offline_snapshot_manifest(tmp_path: Path) -> None:
    environment = _verification_fixture(tmp_path)

    completed = subprocess.run(
        [str(ADAPTER), "verify", "worker"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.stdout == "verified role=worker\n"
    invocation = Path(environment["VERIFIER_LOG"]).read_text(encoding="utf-8")
    assert invocation == (
        "verify --manifest "
        f"{environment['MIA_MODELS_ROOT']}/manifests/deepseek-v4-flash-0731.json "
        "--snapshot "
        f"{environment['MIA_MODELS_ROOT']}/snapshots/deepseek-v4-flash-0731"
    )


def test_verify_rejects_a_changed_checkpoint_manifest_before_snapshot_scan(
    tmp_path: Path,
) -> None:
    environment = _verification_fixture(tmp_path)
    manifest = (
        Path(environment["MIA_MODELS_ROOT"])
        / "manifests/deepseek-v4-flash-0731.json"
    )
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    completed = subprocess.run(
        [str(ADAPTER), "verify", "worker"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 1
    assert "checkpoint manifest digest mismatch" in completed.stderr
    assert not Path(environment["VERIFIER_LOG"]).exists()


def test_verify_rejects_a_stale_roce_gid_mapping(tmp_path: Path) -> None:
    environment = _verification_fixture(tmp_path)
    gid = (
        Path(environment["MIA_SYS_CLASS_INFINIBAND"])
        / "rocep1s0f1/ports/1/gids/3"
    )
    gid.write_text(
        "0000:0000:0000:0000:0000:ffff:c0a8:640a\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [str(ADAPTER), "verify", "worker"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 1
    assert "fabric GID value mismatch: rocep1s0f1/1/3" in completed.stderr


def test_vendored_verifier_matches_the_audited_repository_tool() -> None:
    assert (
        ROOT / "adapters/deepseek/mia-vllm/tools/model_manifest.py"
    ).read_bytes() == (ROOT / "tools/model_manifest.py").read_bytes()


def test_installed_release_resolves_its_default_verifier(tmp_path: Path) -> None:
    release = tmp_path / "release"
    shutil.copytree(ROOT / "adapters/deepseek/mia-vllm", release)
    environment = _verification_fixture(tmp_path / "fixture")
    verifier_log = Path(environment["VERIFIER_LOG"])
    (release / "tools/model_manifest.py").write_text(
        "from pathlib import Path\n"
        "import sys\n"
        f"Path({str(verifier_log)!r}).write_text(' '.join(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    environment.pop("MIA_MODEL_MANIFEST_TOOL")

    completed = subprocess.run(
        [str(release / "bin/mia-deepseek-dual"), "verify", "worker"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.stdout == "verified role=worker\n"
    assert verifier_log.read_text(encoding="utf-8").startswith("verify --manifest ")


def test_worker_health_never_calls_the_head_api(tmp_path: Path) -> None:
    curl_log = tmp_path / "curl.log"
    docker = fake_command(
        tmp_path,
        "docker",
        'if [[ ${1:-} == inspect ]]; then printf "true\\n"; fi\n',
    )
    curl = fake_command(tmp_path, "curl", f'printf "%s\\n" "$*" >> {curl_log!s}\n')

    completed = subprocess.run(
        [str(ADAPTER), "health", "worker"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "MIA_DOCKER_BIN": str(docker),
            "MIA_CURL_BIN": str(curl),
            "MIA_LOCAL_HOSTNAME": "spark-2297",
        },
    )

    assert completed.stdout == "healthy role=worker\n"
    assert not curl_log.exists()


def test_worker_inference_is_rejected_without_calling_curl(tmp_path: Path) -> None:
    curl_log = tmp_path / "curl.log"
    curl = fake_command(tmp_path, "curl", f'printf "%s\\n" "$*" >> {curl_log!s}\n')

    completed = subprocess.run(
        [str(ADAPTER), "infer", "worker"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "MIA_CURL_BIN": str(curl),
            "MIA_LOCAL_HOSTNAME": "spark-2297",
        },
    )

    assert completed.returncode == 2
    assert "infer is head-only" in completed.stderr
    assert not curl_log.exists()


def test_adapter_never_owns_cross_node_transport() -> None:
    source = ADAPTER.read_text(encoding="utf-8")

    for forbidden in ("ssh ", "scp ", "rsync "):
        assert forbidden not in source
