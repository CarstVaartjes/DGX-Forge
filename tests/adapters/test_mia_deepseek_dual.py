from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ADAPTER = ROOT / "adapters/deepseek/mia-vllm/bin/mia-deepseek-dual"
IMAGE = (
    "ghcr.io/anemll/dspark-vllm-gx10"
    "@sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8"
)
QUALITY = ROOT / "adapters/deepseek/mia-vllm/validation/quality.py"
QUALITY_FIXTURES = (
    ROOT / "adapters/deepseek/mia-vllm/validation/quality-fixtures.json"
)


def load_quality():
    spec = importlib.util.spec_from_file_location("mia_quality", QUALITY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _completion(
    content: str | None = None,
    *,
    reasoning: str | None = None,
    tool_calls: list[dict[str, object]] | None = None,
    prompt_tokens: int = 32,
) -> dict[str, object]:
    message: dict[str, object] = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-test",
        "model": "deepseek",
        "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": 8,
            "total_tokens": prompt_tokens + 8,
        },
    }


def _passing_quality_responses() -> dict[str, object]:
    return {
        "english_exact": _completion("SPARK QUALITY OK"),
        "latin_script": _completion(
            "The distributed runtime answers clearly in English."
        ),
        "no_repetition": _completion(
            "A concise answer has varied words and finishes normally."
        ),
        "no_xml_leak": _completion("Reasoning remains private; the answer is clean."),
        "streaming": [
            {"choices": [{"delta": {"content": "STREAM_"}}]},
            {"choices": [{"delta": {"content": "OK"}}]},
            "[DONE]",
        ],
        "reasoning_off": _completion("OFF_OK"),
        "reasoning_low": _completion("4", reasoning="brief private reasoning"),
        "reasoning_high": _completion("7", reasoning="detailed private reasoning"),
        "reasoning_max": _completion("11", reasoning="maximum private reasoning"),
        "tool_call": _completion(
            None,
            tool_calls=[
                {
                    "id": "call_test",
                    "type": "function",
                    "function": {
                        "name": "get_temperature",
                        "arguments": '{"city":"Amsterdam"}',
                    },
                }
            ],
        ),
        "long_411_regression": _completion("LONG_CONTEXT_OK", prompt_tokens=512),
    }


def _rendered_compose_command(role: str) -> list[str]:
    lines = (ROOT / "adapters/deepseek/mia-vllm/compose.yaml").read_text(
        encoding="utf-8"
    ).splitlines()
    marker = lines.index("      - |")
    body: list[str] = []
    for line in lines[marker + 1 :]:
        if not line.startswith("        "):
            break
        body.append(line[8:])
    script = "\n".join(body) + "\n"
    headless = "" if role == "head" else "--headless"
    return ["bash", "-lc", script.replace("${HEADLESS_FLAG}", headless)]


def _valid_compose_config(role: str) -> dict[str, object]:
    return {
        "services": {
            "runtime": {
                "image": IMAGE,
                "network_mode": "host",
                "ipc": "host",
                "shm_size": 68719476736,
                "restart": "no",
                "pull_policy": "never",
                "gpus": [{"count": -1}],
                "devices": [
                    {
                        "source": "/dev/infiniband",
                        "target": "/dev/infiniband",
                    }
                ],
                "ulimits": {
                    "memlock": {"soft": -1, "hard": -1},
                    "stack": {"soft": 67108864, "hard": 67108864},
                },
                "environment": {
                    "HF_HUB_OFFLINE": "1",
                    "TRANSFORMERS_OFFLINE": "1",
                    "HF_HUB_DISABLE_XET": "1",
                    "NODE_RANK": "0" if role == "head" else "1",
                    "DSPARK_MODEL": "/models/deepseek-ai/DeepSeek-V4-Flash-0731",
                    "DSPARK_ENCODING_FILE": "/models/deepseek-ai/DeepSeek-V4-Flash-0731/encoding/encoding_dsv4.py",
                    "VLLM_HOST_IP": (
                        "192.168.100.10" if role == "head" else "192.168.100.11"
                    ),
                    "MASTER_ADDR": "192.168.100.10",
                    "MASTER_PORT": "25000",
                    "NCCL_SOCKET_IFNAME": "=enp1s0f1np1,enP2p1s0f1np1",
                    "NCCL_IB_HCA": "=rocep1s0f1:1,roceP2p1s0f1:1",
                    "NCCL_IB_GID_INDEX": "3",
                    "TP_SOCKET_IFNAME": "enp1s0f1np1,enP2p1s0f1np1",
                    "GLOO_SOCKET_IFNAME": "enp1s0f1np1,enP2p1s0f1np1",
                    "VLLM_CACHE_ROOT": "/runtime-cache/vllm",
                    "FLASHINFER_WORKSPACE_BASE": "/runtime-cache/flashinfer",
                    "MTP_NUM_TOKENS": "5",
                    "DEFAULT_THINKING": "low",
                    "VLLM_USE_B12X_MOE": "1",
                },
                "volumes": [
                    {
                        "type": "bind",
                        "source": "/srv/models/snapshots/deepseek-v4-flash-0731",
                        "target": "/models/deepseek-ai/DeepSeek-V4-Flash-0731",
                        "read_only": True,
                    },
                    {
                        "type": "bind",
                        "source": "/srv/models/runtime-cache/deepseek-agent-dual",
                        "target": "/runtime-cache",
                    },
                    {
                        "type": "bind",
                        "source": "/srv/models/runtime-cache/deepseek-agent-dual/tmp",
                        "target": "/tmp",
                    },
                    {
                        "type": "bind",
                        "source": "/srv/models/outputs/deepseek-agent-dual",
                        "target": "/outputs",
                    },
                    {
                        "type": "bind",
                        "source": "/srv/models/logs/deepseek-agent-dual",
                        "target": "/logs",
                    },
                ],
                "command": _rendered_compose_command(role),
            }
        }
    }


def test_quality_fixture_covers_every_required_gate() -> None:
    fixtures = json.loads(QUALITY_FIXTURES.read_text(encoding="utf-8"))

    assert [case["id"] for case in fixtures["cases"]] == [
        "english_exact",
        "latin_script",
        "no_repetition",
        "no_xml_leak",
        "streaming",
        "reasoning_off",
        "reasoning_low",
        "reasoning_high",
        "reasoning_max",
        "tool_call",
        "long_411_regression",
    ]
    by_id = {case["id"]: case for case in fixtures["cases"]}
    reasoning_off = by_id["reasoning_off"]["request"]
    assert reasoning_off["chat_template_kwargs"] == {"thinking": False}
    assert "reasoning_effort" not in reasoning_off
    for effort in ("low", "high", "max"):
        request = by_id[f"reasoning_{effort}"]["request"]
        assert request["chat_template_kwargs"] == {"thinking": True}
        assert request["reasoning_effort"] == effort
    long_prompt = fixtures["cases"][-1]["request"]["messages"][0]["content"]
    assert len(long_prompt.split()) > 411


def test_quality_runner_writes_bounded_structured_evidence(tmp_path: Path) -> None:
    quality = load_quality()
    evidence_path = tmp_path / "quality.json"

    evidence = quality.run_quality(
        fixtures_path=QUALITY_FIXTURES,
        output_path=evidence_path,
        request=lambda case: _passing_quality_responses()[case["id"]],
        release_sha256="a" * 64,
        boot_id="boot-id-1",
    )

    assert evidence["status"] == "passed"
    assert evidence["release_sha256"] == "a" * 64
    assert evidence["boot_id"] == "boot-id-1"
    assert all(gate["passed"] for gate in evidence["gates"])
    assert evidence_path.stat().st_size <= 65536
    assert json.loads(evidence_path.read_text(encoding="utf-8")) == evidence


def test_quality_runner_records_external_request_failures(tmp_path: Path) -> None:
    quality = load_quality()
    evidence_path = tmp_path / "quality.json"

    with pytest.raises(quality.QualityFailure, match="request failed"):
        quality.run_quality(
            fixtures_path=QUALITY_FIXTURES,
            output_path=evidence_path,
            request=lambda _case: (_ for _ in ()).throw(RuntimeError("offline")),
            release_sha256="a" * 64,
            boot_id="boot-id-1",
        )

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["status"] == "failed"
    assert evidence["error"] == "english_exact: request failed: offline"


@pytest.mark.parametrize(
    ("case_id", "bad_response", "message"),
    [
        ("english_exact", _completion("almost"), "exact content"),
        ("latin_script", _completion("только кириллица"), "script drift"),
        ("no_repetition", _completion("echo " * 40), "repetition loop"),
        ("no_xml_leak", _completion("<think>secret</think>answer"), "XML leakage"),
        ("streaming", [{"choices": [{"delta": {"content": "STREAM_OK"}}]}], "stream terminator"),
        ("reasoning_off", _completion("OFF_OK", reasoning="leaked"), "reasoning off"),
        ("reasoning_low", _completion("4"), "reasoning low"),
        ("reasoning_high", _completion("7"), "reasoning high"),
        ("reasoning_max", _completion("11"), "reasoning max"),
        ("tool_call", _completion("no tool"), "tool call"),
        ("long_411_regression", _completion("LONG_CONTEXT_OK", prompt_tokens=411), ">411"),
    ],
)
def test_quality_runner_rejects_each_gate(
    tmp_path: Path,
    case_id: str,
    bad_response: object,
    message: str,
) -> None:
    quality = load_quality()
    responses = _passing_quality_responses()
    responses[case_id] = bad_response

    with pytest.raises(quality.QualityFailure, match=message):
        quality.run_quality(
            fixtures_path=QUALITY_FIXTURES,
            output_path=tmp_path / "quality.json",
            request=lambda case: responses[case["id"]],
            release_sha256="a" * 64,
            boot_id="boot-id-1",
        )


def test_reasoning_alias_is_checked_when_reasoning_content_is_null() -> None:
    quality = load_quality()
    fixtures = json.loads(QUALITY_FIXTURES.read_text(encoding="utf-8"))
    by_id = {case["id"]: case for case in fixtures["cases"]}
    response = _completion("4")
    message = response["choices"][0]["message"]
    message["reasoning_content"] = None
    message["reasoning"] = "private reasoning from the vLLM alias"

    assert quality._evaluate(by_id["reasoning_low"], response)["passed"] is True

    response["choices"][0]["message"]["content"] = "OFF_OK"
    with pytest.raises(quality.QualityFailure, match="reasoning off"):
        quality._evaluate(by_id["reasoning_off"], response)


def test_startup_identity_record_is_container_release_and_boot_qualified(
    tmp_path: Path,
) -> None:
    quality = load_quality()
    output = tmp_path / "startup.json"
    logs = (
        "Initializing a V1 LLM engine with config: "
        "model='/models/deepseek-ai/DeepSeek-V4-Flash-0731', "
        "max_seq_len=1048576, tensor_parallel_size=2, pipeline_parallel_size=1\n"
    )
    release = "a" * 64
    container = "b" * 64

    quality.record_startup_identity(
        output_path=output,
        logs=logs,
        release_sha256=release,
        boot_id="boot-1",
        container_id=container,
    )
    quality.validate_startup_identity(
        record_path=output,
        release_sha256=release,
        boot_id="boot-1",
        container_id=container,
    )

    with pytest.raises(quality.QualityFailure, match="container_id mismatch"):
        quality.validate_startup_identity(
            record_path=output,
            release_sha256=release,
            boot_id="boot-1",
            container_id="c" * 64,
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


def test_adapter_runtime_pins_match_model_definition() -> None:
    with (ROOT / "config/workloads/deepseek-agent-dual.toml").open("rb") as source:
        definition = tomllib.load(source)
    adapter = ADAPTER.read_text(encoding="utf-8")

    assignments = dict(
        re.findall(
            r"^(image_reference|checkpoint_manifest_sha256|checkpoint_revision|"
            r"minimum_free_memory_bytes|minimum_free_disk_bytes|"
            r"stop_memory_tolerance_bytes)=([^\n]+)$",
            adapter,
            flags=re.MULTILINE,
        )
    )
    assert assignments == {
        "image_reference": definition["image"]["reference"],
        "checkpoint_manifest_sha256": definition["checkpoint"]["manifest_sha256"],
        "checkpoint_revision": definition["checkpoint"]["revision"],
        "minimum_free_memory_bytes": str(
            definition["resources"]["minimum_free_memory_bytes"]
        ),
        "minimum_free_disk_bytes": str(
            definition["resources"]["minimum_free_disk_bytes"]
        ),
        "stop_memory_tolerance_bytes": str(
            definition["resources"]["stop_memory_tolerance_bytes"]
        ),
    }
    assert adapter.count(definition["checkpoint"]["manifest_sha256"]) == 1


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
    boot_id = tmp_path / "boot_id"
    boot_id.write_text("boot-1\n", encoding="utf-8")
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemAvailable:       125000000 kB\n", encoding="utf-8")
    environment = {
        **os.environ,
        "MIA_DOCKER_BIN": str(docker),
        "MIA_MODELS_ROOT": str(tmp_path / "models"),
        "MIA_RELEASE_SHA256": "a" * 64,
        "MIA_BOOT_ID_PATH": str(boot_id),
        "MIA_MEMINFO_PATH": str(meminfo),
        "MIA_IDLE_SECONDS": "0",
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
    environment = {
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
    _add_preflight_commands(environment, tmp_path)
    return environment


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
        '''if [[ $* == *"State.Running"* ]]; then printf "true\\n"
elif [[ $* == *"Config.Image"* ]]; then printf "%s\\n" "$FAKE_IMAGE_REFERENCE"
elif [[ $* == *"Config.Env"* ]]; then printf "NODE_RANK=1\\n"
fi
''',
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
            "FAKE_IMAGE_REFERENCE": IMAGE,
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


def test_compose_validation_rejects_runtime_pin_drift() -> None:
    quality = load_quality()
    rendered = _valid_compose_config("worker")

    quality.validate_compose(rendered, "worker")
    rendered["services"]["runtime"]["environment"]["HF_HUB_OFFLINE"] = "0"
    with pytest.raises(quality.QualityFailure, match="HF_HUB_OFFLINE"):
        quality.validate_compose(rendered, "worker")


@pytest.mark.parametrize(
    ("drift", "expected"),
    [
        ("gpu", "all-GPU"),
        ("stack", "stack ulimit"),
        ("mount", "volume list"),
        ("extra_mount", "volume list"),
        ("mtp", "speculative-config"),
        ("distributed", "--nnodes 2"),
        ("duplicate", "runtime command mismatch"),
    ],
)
def test_compose_validation_rejects_critical_lane_drift(
    drift: str, expected: str
) -> None:
    quality = load_quality()
    rendered = _valid_compose_config("worker")
    runtime = rendered["services"]["runtime"]
    if drift == "gpu":
        runtime["gpus"] = []
    elif drift == "stack":
        runtime["ulimits"]["stack"] = 8192
    elif drift == "mount":
        runtime["volumes"][1]["source"] = "/tmp/mutable-cache"
    elif drift == "extra_mount":
        runtime["volumes"].append(
            {
                "type": "bind",
                "source": "/tmp/extra",
                "target": "/extra",
            }
        )
    elif drift == "mtp":
        runtime["command"][2] = runtime["command"][2].replace(
            '"num_speculative_tokens":5', '"num_speculative_tokens":1'
        )
    elif drift == "distributed":
        runtime["command"][2] = runtime["command"][2].replace("--nnodes 2", "")
    else:
        runtime["command"][2] += "--max-model-len 411\n"

    with pytest.raises(quality.QualityFailure, match=expected):
        quality.validate_compose(rendered, "worker")


def test_compose_validation_rejects_wrong_role_network_config() -> None:
    quality = load_quality()
    rendered = _valid_compose_config("worker")
    rendered["services"]["runtime"]["environment"]["VLLM_HOST_IP"] = (
        "192.168.100.10"
    )

    with pytest.raises(quality.QualityFailure, match="VLLM_HOST_IP"):
        quality.validate_compose(rendered, "worker")


def test_head_identity_and_startup_logs_require_exact_pins() -> None:
    quality = load_quality()
    quality.validate_models({"data": [{"id": "deepseek"}]})
    quality.validate_startup_logs(
        "Initializing a V1 LLM engine with config: "
        "model='/models/deepseek-ai/DeepSeek-V4-Flash-0731', "
        "max_seq_len=1048576, tensor_parallel_size=2, pipeline_parallel_size=1"
    )

    with pytest.raises(quality.QualityFailure, match="model identity"):
        quality.validate_models({"data": [{"id": "wrong"}]})
    with pytest.raises(quality.QualityFailure, match="max_seq_len"):
        quality.validate_startup_logs(
            "Initializing a V1 LLM engine with config: "
            "model='/models/deepseek-ai/DeepSeek-V4-Flash-0731', "
            "max_seq_len=411, tensor_parallel_size=2, pipeline_parallel_size=1"
        )


def test_lifecycle_records_release_and_boot_qualified_memory(tmp_path: Path) -> None:
    quality = load_quality()
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemAvailable:       125000000 kB\n", encoding="utf-8")
    baseline = tmp_path / ("a" * 64) / "boot-1" / "worker.baseline.json"
    result = tmp_path / ("a" * 64) / "boot-1" / "worker.release.json"

    quality.record_memory_baseline(
        output_path=baseline,
        meminfo_path=meminfo,
        release_sha256="a" * 64,
        boot_id="boot-1",
        role="worker",
    )
    recorded = json.loads(baseline.read_text(encoding="utf-8"))
    assert recorded["mem_available_bytes"] == 128000000000
    assert recorded["release_sha256"] == "a" * 64
    assert recorded["boot_id"] == "boot-1"

    meminfo.write_text("MemAvailable:       124000000 kB\n", encoding="utf-8")
    assert quality.record_release_memory(
        baseline_path=baseline,
        output_path=result,
        meminfo_path=meminfo,
        release_sha256="a" * 64,
        boot_id="boot-1",
        role="worker",
        tolerance_bytes=1073741824,
    )
    assert json.loads(result.read_text(encoding="utf-8"))["status"] == "recovered"

    meminfo.write_text("MemAvailable:       100000000 kB\n", encoding="utf-8")
    assert not quality.record_release_memory(
        baseline_path=baseline,
        output_path=result,
        meminfo_path=meminfo,
        release_sha256="a" * 64,
        boot_id="boot-1",
        role="worker",
        tolerance_bytes=1073741824,
    )
    assert json.loads(result.read_text(encoding="utf-8"))["status"] == "pending"


def test_lifecycle_without_a_baseline_records_not_started(tmp_path: Path) -> None:
    quality = load_quality()
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemAvailable:       125000000 kB\n", encoding="utf-8")
    result = tmp_path / "worker.release.json"

    assert quality.record_release_memory(
        baseline_path=tmp_path / "missing.json",
        output_path=result,
        meminfo_path=meminfo,
        release_sha256="a" * 64,
        boot_id="boot-1",
        role="worker",
        tolerance_bytes=1073741824,
    )
    assert json.loads(result.read_text(encoding="utf-8"))["status"] == "not-started"


def _add_preflight_commands(environment: dict[str, str], tmp_path: Path) -> Path:
    command_log = tmp_path / "preflight.log"
    docker = fake_command(
        tmp_path,
        "docker-preflight",
        f'''printf "docker %s\\n" "$*" >> {command_log!s}
if [[ $* == *"compose"* && $* == *"--format json"* ]]; then
  printf "%s\\n" "$FAKE_COMPOSE_JSON"
elif [[ ${{1:-}} == image && ${{2:-}} == inspect && $* == *"--format"* ]]; then
  printf "%s\\n" "$FAKE_IMAGE_REFERENCE"
elif [[ ${{1:-}} == ps ]]; then
  printf "%s" "${{FAKE_CONFLICTS:-}}"
fi
''',
    )
    nvidia_smi = fake_command(
        tmp_path,
        "nvidia-smi",
        f'printf "nvidia-smi %s\\n" "$*" >> {command_log!s}\n'
        'printf "%s\\n" "${FAKE_GPU_NAME:-NVIDIA GB10}"\n',
    )
    systemctl = fake_command(
        tmp_path,
        "systemctl",
        f'''printf "systemctl %s\\n" "$*" >> {command_log!s}
if [[ ${{FAKE_SYSTEMCTL_MODE:-}} == error ]]; then
  printf "Failed to connect to bus\\n" >&2
  exit 1
fi
if [[ ${{1:-}} == is-enabled ]]; then
  if [[ ${{FAKE_SYSTEMCTL_MODE:-}} == enabled ]]; then
    printf "enabled\\n"
    exit 0
  fi
  printf "not-found\\n"
  exit 4
fi
if [[ ${{FAKE_SYSTEMCTL_MODE:-}} == user-enabled && $* == *"--user"* ]]; then
  printf "vllm-user.service enabled enabled\\n"
fi
exit 0
''',
    )
    df = fake_command(
        tmp_path,
        "df-preflight",
        f'printf "df %s\\n" "$*" >> {command_log!s}\n'
        'printf "Filesystem 1-blocks Used Available Capacity Mounted on\\n"\n'
        'printf "fake 1000000000000 1 %s 1%%%% /srv/models\\n" '
        '"${FAKE_DISK_BYTES:-999999999999}"\n',
    )
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemAvailable:       125000000 kB\n", encoding="utf-8")
    environment.update(
        {
            "MIA_DOCKER_BIN": str(docker),
            "MIA_NVIDIA_SMI_BIN": str(nvidia_smi),
            "MIA_SYSTEMCTL_BIN": str(systemctl),
            "MIA_DF_BIN": str(df),
            "MIA_MEMINFO_PATH": str(meminfo),
            "MIA_RELEASE_SHA256": "a" * 64,
            "FAKE_COMPOSE_JSON": json.dumps(_valid_compose_config("worker")),
            "FAKE_IMAGE_REFERENCE": IMAGE,
        }
    )
    return command_log


def test_verify_runs_complete_offline_node_preflight(tmp_path: Path) -> None:
    environment = _verification_fixture(tmp_path)
    command_log = _add_preflight_commands(environment, tmp_path)

    completed = subprocess.run(
        [str(ADAPTER), "verify", "worker"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.stdout == "verified role=worker\n"
    calls = command_log.read_text(encoding="utf-8")
    assert "config --format json" in calls
    assert "image inspect --format" in calls
    assert "nvidia-smi --query-gpu=name --format=csv,noheader" in calls
    assert "systemctl is-enabled mia-deepseek-dual-worker.service" in calls
    assert "docker ps --format" in calls
    assert "df -PB1" in calls


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"FAKE_GPU_NAME": "NVIDIA H100"}, "GB10"),
        ({"FAKE_DISK_BYTES": "1000"}, "free disk"),
        ({"FAKE_CONFLICTS": "ollama\n"}, "conflicting runtime"),
    ],
)
def test_verify_rejects_failed_node_preflight(
    tmp_path: Path, override: dict[str, str], expected: str
) -> None:
    environment = _verification_fixture(tmp_path)
    _add_preflight_commands(environment, tmp_path)
    environment.update(override)

    completed = subprocess.run(
        [str(ADAPTER), "verify", "worker"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 1
    assert expected in completed.stderr


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("error", "systemd autostart inspection failed"),
        ("enabled", "autostart unit is enabled"),
        ("user-enabled", "matching runtime autostart unit is enabled"),
    ],
)
def test_verify_fails_closed_for_systemd_autostart_inspection(
    tmp_path: Path, mode: str, expected: str
) -> None:
    environment = _verification_fixture(tmp_path)
    _add_preflight_commands(environment, tmp_path)
    environment["FAKE_SYSTEMCTL_MODE"] = mode

    completed = subprocess.run(
        [str(ADAPTER), "verify", "worker"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 1
    assert expected in completed.stderr


def test_health_rejects_a_container_with_the_wrong_local_rank(tmp_path: Path) -> None:
    docker = fake_command(
        tmp_path,
        "docker",
        '''if [[ $* == *"State.Running"* ]]; then printf "true\\n"
elif [[ $* == *"Config.Env"* ]]; then printf "NODE_RANK=0\\n"
elif [[ $* == *"Config.Image"* ]]; then printf "%s\\n" "$FAKE_IMAGE_REFERENCE"
fi
''',
    )

    completed = subprocess.run(
        [str(ADAPTER), "health", "worker"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "MIA_DOCKER_BIN": str(docker),
            "MIA_LOCAL_HOSTNAME": "spark-2297",
            "MIA_RELEASE_SHA256": "a" * 64,
            "FAKE_IMAGE_REFERENCE": IMAGE,
        },
    )

    assert completed.returncode == 1
    assert "container rank mismatch" in completed.stderr


def test_head_health_checks_model_identity_render_and_startup_logs(
    tmp_path: Path,
) -> None:
    call_log = tmp_path / "health.log"
    models_root = tmp_path / "models"
    boot_id = tmp_path / "boot_id"
    boot_id.write_text("boot-1\n", encoding="utf-8")
    docker = fake_command(
        tmp_path,
        "docker",
        f'''printf "docker %s\\n" "$*" >> {call_log!s}
if [[ $* == *"State.Running"* ]]; then printf "true\\n"
elif [[ $* == *"{{{{.Id}}}}"* ]]; then printf "%s\\n" "$FAKE_CONTAINER_ID"
elif [[ $* == *"Config.Env"* ]]; then printf "NODE_RANK=0\\n"
elif [[ $* == *"Config.Image"* ]]; then printf "%s\\n" "$FAKE_IMAGE_REFERENCE"
elif [[ $* == *"compose"* && $* == *"--format json"* ]]; then printf "%s\\n" "$FAKE_COMPOSE_JSON"
elif [[ ${{1:-}} == logs ]]; then
  printf "Initializing a V1 LLM engine with config: model='/models/deepseek-ai/DeepSeek-V4-Flash-0731', max_seq_len=1048576, tensor_parallel_size=2, pipeline_parallel_size=1\\n" >&2
fi
''',
    )
    curl = fake_command(
        tmp_path,
        "curl",
        f'''printf "curl %s\\n" "$*" >> {call_log!s}
if [[ $* == *"/v1/models"* ]]; then printf '{{"data":[{{"id":"deepseek"}}]}}\\n'; fi
''',
    )

    completed = subprocess.run(
        [str(ADAPTER), "health", "head"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "MIA_DOCKER_BIN": str(docker),
            "MIA_CURL_BIN": str(curl),
            "MIA_LOCAL_HOSTNAME": "spark-3542",
            "MIA_MODELS_ROOT": str(models_root),
            "MIA_RELEASE_SHA256": "a" * 64,
            "MIA_BOOT_ID_PATH": str(boot_id),
            "FAKE_IMAGE_REFERENCE": IMAGE,
            "FAKE_CONTAINER_ID": "b" * 64,
            "FAKE_COMPOSE_JSON": json.dumps(_valid_compose_config("head")),
        },
    )

    assert completed.stdout == "healthy role=head\n"
    calls = call_log.read_text(encoding="utf-8")
    assert "/v1/models" in calls
    assert "config --format json" in calls
    assert "docker logs --tail 2000 mia-deepseek-dual-head" in calls

    second = subprocess.run(
        [str(ADAPTER), "health", "head"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "MIA_DOCKER_BIN": str(docker),
            "MIA_CURL_BIN": str(curl),
            "MIA_LOCAL_HOSTNAME": "spark-3542",
            "MIA_MODELS_ROOT": str(models_root),
            "MIA_RELEASE_SHA256": "a" * 64,
            "MIA_BOOT_ID_PATH": str(boot_id),
            "FAKE_IMAGE_REFERENCE": IMAGE,
            "FAKE_CONTAINER_ID": "b" * 64,
            "FAKE_COMPOSE_JSON": json.dumps(_valid_compose_config("head")),
        },
    )
    assert second.stdout == "healthy role=head\n"
    calls = call_log.read_text(encoding="utf-8")
    assert calls.count("docker logs --tail 2000 mia-deepseek-dual-head") == 1


def test_head_infer_writes_release_and_boot_qualified_evidence(tmp_path: Path) -> None:
    models_root = tmp_path / "models"
    boot_id = tmp_path / "boot_id"
    boot_id.write_text("boot-1\n", encoding="utf-8")
    quality_log = tmp_path / "quality.log"
    quality = fake_command(
        tmp_path,
        "quality",
        f'''printf "%s\\n" "$*" > {quality_log!s}
while [[ $# -gt 0 ]]; do
  if [[ $1 == --output ]]; then shift; output=$1; fi
  shift || true
done
mkdir -p -- "$(dirname -- "$output")"
printf '{{"status":"passed"}}\\n' > "$output"
printf "%s\\n" "$output"
''',
    )

    completed = subprocess.run(
        [str(ADAPTER), "infer", "head"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "MIA_LOCAL_HOSTNAME": "spark-3542",
            "MIA_MODELS_ROOT": str(models_root),
            "MIA_RELEASE_SHA256": "a" * 64,
            "MIA_BOOT_ID_PATH": str(boot_id),
            "MIA_QUALITY_BIN": str(quality),
        },
    )

    evidence = list((models_root / "outputs/deepseek-agent-dual").rglob("*.json"))
    assert len(evidence) == 1
    assert ("a" * 64) in str(evidence[0])
    assert "boot-1" in str(evidence[0])
    assert json.loads(evidence[0].read_text(encoding="utf-8"))["status"] == "passed"
    assert "--fixtures" in quality_log.read_text(encoding="utf-8")
    assert completed.stdout == str(evidence[0]) + "\n"


def test_start_records_idle_memory_baseline_before_compose_up(tmp_path: Path) -> None:
    models_root = tmp_path / "models"
    boot_id = tmp_path / "boot_id"
    boot_id.write_text("boot-1\n", encoding="utf-8")
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemAvailable:       125000000 kB\n", encoding="utf-8")
    docker_log = tmp_path / "docker.log"
    docker = fake_command(
        tmp_path,
        "docker",
        f'printf "%s\\n" "$*" >> {docker_log!s}\n',
    )

    subprocess.run(
        [str(ADAPTER), "start", "worker"],
        cwd=ROOT,
        check=True,
        env={
            **os.environ,
            "MIA_DOCKER_BIN": str(docker),
            "MIA_LOCAL_HOSTNAME": "spark-2297",
            "MIA_MODELS_ROOT": str(models_root),
            "MIA_RELEASE_SHA256": "a" * 64,
            "MIA_BOOT_ID_PATH": str(boot_id),
            "MIA_MEMINFO_PATH": str(meminfo),
            "MIA_IDLE_SECONDS": "0",
        },
    )

    baseline = (
        models_root
        / "runtime-cache/deepseek-agent-dual/lifecycle"
        / ("a" * 64)
        / "boot-1/worker.baseline.json"
    )
    assert json.loads(baseline.read_text(encoding="utf-8"))["status"] == "baseline"
    assert "up --detach" in docker_log.read_text(encoding="utf-8")


def test_verify_release_enforces_memory_recovery_tolerance(tmp_path: Path) -> None:
    quality = load_quality()
    models_root = tmp_path / "models"
    boot_id = tmp_path / "boot_id"
    boot_id.write_text("boot-1\n", encoding="utf-8")
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemAvailable:       125000000 kB\n", encoding="utf-8")
    baseline = (
        models_root
        / "runtime-cache/deepseek-agent-dual/lifecycle"
        / ("a" * 64)
        / "boot-1/worker.baseline.json"
    )
    quality.record_memory_baseline(
        output_path=baseline,
        meminfo_path=meminfo,
        release_sha256="a" * 64,
        boot_id="boot-1",
        role="worker",
    )
    meminfo.write_text("MemAvailable:       100000000 kB\n", encoding="utf-8")
    docker = fake_command(tmp_path, "docker", "exit 1\n")
    curl = fake_command(tmp_path, "curl", "exit 1\n")
    ss = fake_command(tmp_path, "ss", ":\n")
    systemctl = fake_command(
        tmp_path,
        "systemctl-release",
        '''if [[ ${1:-} == is-enabled ]]; then
  printf "not-found\\n"
  exit 4
fi
exit 0
''',
    )

    completed = subprocess.run(
        [str(ADAPTER), "verify-release", "worker"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "MIA_DOCKER_BIN": str(docker),
            "MIA_CURL_BIN": str(curl),
            "MIA_SS_BIN": str(ss),
            "MIA_SYSTEMCTL_BIN": str(systemctl),
            "MIA_LOCAL_HOSTNAME": "spark-2297",
            "MIA_MODELS_ROOT": str(models_root),
            "MIA_RELEASE_SHA256": "a" * 64,
            "MIA_BOOT_ID_PATH": str(boot_id),
            "MIA_MEMINFO_PATH": str(meminfo),
            "MIA_RELEASE_WAIT_SECONDS": "0",
        },
    )

    assert completed.returncode == 1
    assert "memory did not recover within 1073741824 bytes" in completed.stderr
