"""Bounded acceptance gates for the pinned Mia DeepSeek runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_HTTP_BYTES = 1024 * 1024
DEFAULT_MAX_EVIDENCE_BYTES = 65536
EXPECTED_COMMAND_SHA256 = {
    "head": "84c60fd6931ec972581545587ce09d315d7b0026b8f579871ddb41d8d92ca852",
    "worker": "1736d21e1d09a3a15a5e6d71cd68c6e23077ffb80cdf9dd8c14e9555ac238abc",
}


class QualityFailure(RuntimeError):
    """A runtime response failed an acceptance gate."""


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise QualityFailure("fixture root must be an object")
    return value


def _atomic_json(path: Path, value: Mapping[str, Any], maximum: int) -> None:
    encoded = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(encoded) > maximum:
        raise QualityFailure(f"structured evidence exceeds {maximum} bytes")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _message(response: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        message = response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as error:
        raise QualityFailure("response has no assistant message") from error
    if not isinstance(message, Mapping):
        raise QualityFailure("assistant message is not an object")
    return message


def _content(response: Mapping[str, Any]) -> str:
    content = _message(response).get("content")
    if not isinstance(content, str):
        raise QualityFailure("assistant content is not a string")
    return content.strip()


def _stream_response(chunks: object) -> tuple[dict[str, Any], bool]:
    if not isinstance(chunks, Sequence) or isinstance(chunks, (str, bytes)):
        raise QualityFailure("stream response is not a sequence")
    content: list[str] = []
    done = False
    for chunk in chunks:
        if chunk == "[DONE]":
            done = True
            continue
        if not isinstance(chunk, Mapping):
            raise QualityFailure("stream chunk is not an object")
        try:
            piece = chunk["choices"][0]["delta"].get("content")
        except (KeyError, IndexError, TypeError) as error:
            raise QualityFailure("malformed stream chunk") from error
        if piece is not None:
            if not isinstance(piece, str):
                raise QualityFailure("stream content is not a string")
            content.append(piece)
    return {
        "choices": [{"message": {"role": "assistant", "content": "".join(content)}}]
    }, done


def _has_repetition_loop(content: str) -> bool:
    words = re.findall(r"[A-Za-z0-9']+", content.casefold())
    for width in range(1, min(8, len(words)) + 1):
        for start in range(len(words) - width * 6 + 1):
            phrase = words[start : start + width]
            if all(
                words[start + offset : start + offset + width] == phrase
                for offset in range(width, width * 6, width)
            ):
                return True
    return False


def _evaluate(case: Mapping[str, Any], raw_response: object) -> dict[str, Any]:
    case_id = str(case.get("id", "unknown"))
    checks = case.get("checks")
    if not isinstance(checks, Mapping):
        raise QualityFailure(f"{case_id}: checks must be an object")

    stream_complete = True
    if case.get("request", {}).get("stream") is True:
        response, stream_complete = _stream_response(raw_response)
    elif isinstance(raw_response, Mapping):
        response = raw_response
    else:
        raise QualityFailure(f"{case_id}: response is not an object")

    content_required = any(
        name in checks
        for name in (
            "exact_content",
            "latin_script",
            "no_repetition_loop",
            "no_xml_leakage",
        )
    )
    content = _content(response) if content_required else ""

    expected = checks.get("exact_content")
    if expected is not None and content != expected:
        raise QualityFailure(f"{case_id}: exact content mismatch")
    if checks.get("latin_script") is True:
        letters = [character for character in content if character.isalpha()]
        if not letters or any(ord(character) > 127 for character in letters):
            raise QualityFailure(f"{case_id}: script drift detected")
    if checks.get("no_repetition_loop") is True and _has_repetition_loop(content):
        raise QualityFailure(f"{case_id}: repetition loop detected")
    if checks.get("no_xml_leakage") is True and re.search(r"<[^>]+>", content):
        raise QualityFailure(f"{case_id}: XML leakage detected")
    if checks.get("stream_complete") is True and not stream_complete:
        raise QualityFailure(f"{case_id}: stream terminator missing")

    reasoning = checks.get("reasoning")
    if reasoning is not None:
        message = _message(response)
        present = any(
            isinstance(value, str) and bool(value.strip())
            for value in (message.get("reasoning_content"), message.get("reasoning"))
        )
        if reasoning == "absent" and present:
            raise QualityFailure(f"{case_id}: reasoning off leaked content")
        if reasoning == "present" and not present:
            effort = case_id.removeprefix("reasoning_")
            raise QualityFailure(f"{case_id}: reasoning {effort} content missing")

    expected_tool = checks.get("tool_call")
    if expected_tool is not None:
        tool_calls = _message(response).get("tool_calls")
        if not isinstance(tool_calls, list) or len(tool_calls) != 1:
            raise QualityFailure(f"{case_id}: tool call missing")
        function = tool_calls[0].get("function") if isinstance(tool_calls[0], Mapping) else None
        if not isinstance(function, Mapping) or function.get("name") != expected_tool["name"]:
            raise QualityFailure(f"{case_id}: tool call name mismatch")
        try:
            arguments = json.loads(function.get("arguments", ""))
        except (TypeError, json.JSONDecodeError) as error:
            raise QualityFailure(f"{case_id}: tool call arguments are not JSON") from error
        if arguments != expected_tool["arguments"]:
            raise QualityFailure(f"{case_id}: tool call arguments mismatch")

    minimum_prompt_tokens = checks.get("minimum_prompt_tokens")
    prompt_tokens = response.get("usage", {}).get("prompt_tokens")
    if minimum_prompt_tokens is not None and (
        not isinstance(prompt_tokens, int) or prompt_tokens < minimum_prompt_tokens
    ):
        raise QualityFailure(f"{case_id}: >411 prompt-token regression gate failed")

    return {
        "id": case_id,
        "passed": True,
        "content_characters": len(content),
        "prompt_tokens": prompt_tokens if isinstance(prompt_tokens, int) else None,
    }


def run_quality(
    *,
    fixtures_path: Path,
    output_path: Path,
    request: Callable[[Mapping[str, Any]], object],
    release_sha256: str,
    boot_id: str,
) -> dict[str, Any]:
    if re.fullmatch(r"[0-9a-f]{64}", release_sha256) is None:
        raise QualityFailure("release SHA-256 must contain 64 lowercase hex characters")
    fixtures = _load_json(fixtures_path)
    maximum = fixtures.get("max_evidence_bytes", DEFAULT_MAX_EVIDENCE_BYTES)
    if not isinstance(maximum, int) or maximum <= 0 or maximum > DEFAULT_MAX_EVIDENCE_BYTES:
        raise QualityFailure("invalid evidence-size bound")
    cases = fixtures.get("cases")
    if not isinstance(cases, list) or not cases:
        raise QualityFailure("quality fixtures contain no cases")

    evidence: dict[str, Any] = {
        "schema_version": 1,
        "status": "running",
        "release_sha256": release_sha256,
        "boot_id": boot_id.strip(),
        "model": "deepseek",
        "generated_at": datetime.now(UTC).isoformat(),
        "gates": [],
    }
    try:
        for case in cases:
            if not isinstance(case, Mapping):
                raise QualityFailure("quality case is not an object")
            try:
                response = request(case)
            except Exception as error:
                case_id = str(case.get("id", "unknown"))
                raise QualityFailure(
                    f"{case_id}: request failed: {str(error)[:512]}"
                ) from error
            evidence["gates"].append(_evaluate(case, response))
    except QualityFailure as error:
        evidence["status"] = "failed"
        evidence["error"] = str(error)[:1024]
        _atomic_json(output_path, evidence, maximum)
        raise
    evidence["status"] = "passed"
    _atomic_json(output_path, evidence, maximum)
    return evidence


def _require_equal(actual: object, expected: object, label: str) -> None:
    if actual != expected:
        raise QualityFailure(f"Compose {label} mismatch: {actual!r}")


def validate_compose(config: Mapping[str, Any], role: str) -> None:
    """Validate the security, topology, offline, and immutable runtime pins."""
    try:
        service = config["services"]["runtime"]
    except (KeyError, TypeError) as error:
        raise QualityFailure("Compose runtime service missing") from error
    if not isinstance(service, Mapping):
        raise QualityFailure("Compose runtime service is not an object")
    _require_equal(
        service.get("image"),
        "ghcr.io/anemll/dspark-vllm-gx10@sha256:"
        "a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8",
        "image",
    )
    for name, expected in (
        ("network_mode", "host"),
        ("ipc", "host"),
        ("restart", "no"),
        ("pull_policy", "never"),
    ):
        _require_equal(service.get(name), expected, name)
    _require_equal(str(service.get("shm_size")), "68719476736", "shm_size")
    _require_equal(service.get("gpus"), [{"count": -1}], "all-GPU allocation")

    devices = service.get("devices", [])
    if not any(
        isinstance(device, Mapping)
        and device.get("source") == "/dev/infiniband"
        and device.get("target") == "/dev/infiniband"
        for device in devices
    ):
        raise QualityFailure("Compose InfiniBand device mapping missing")
    ulimits = service.get("ulimits", {})
    memlock = ulimits.get("memlock") if isinstance(ulimits, Mapping) else None
    if memlock not in (-1, {"soft": -1, "hard": -1}):
        raise QualityFailure("Compose memlock mismatch")
    stack = ulimits.get("stack") if isinstance(ulimits, Mapping) else None
    if stack not in (67108864, {"soft": 67108864, "hard": 67108864}):
        raise QualityFailure("Compose stack ulimit mismatch")

    environment = service.get("environment")
    if not isinstance(environment, Mapping):
        raise QualityFailure("Compose environment missing")
    expected_rank = "0" if role == "head" else "1"
    expected_host_ip = "192.168.100.10" if role == "head" else "192.168.100.11"
    for name, expected in (
        ("HF_HUB_OFFLINE", "1"),
        ("TRANSFORMERS_OFFLINE", "1"),
        ("HF_HUB_DISABLE_XET", "1"),
        ("NODE_RANK", expected_rank),
        ("DVONK_MODEL", "/models/deepseek-ai/DeepSeek-V4-Flash-0731"),
        (
            "DVONK_ENCODING_FILE",
            "/models/deepseek-ai/DeepSeek-V4-Flash-0731/encoding/encoding_dsv4.py",
        ),
        ("VLLM_HOST_IP", expected_host_ip),
        ("MASTER_ADDR", "192.168.100.10"),
        ("MASTER_PORT", "25000"),
        ("NCCL_SOCKET_IFNAME", "=enp1s0f1np1,enP2p1s0f1np1"),
        ("NCCL_IB_HCA", "=rocep1s0f1:1,roceP2p1s0f1:1"),
        ("NCCL_IB_GID_INDEX", "3"),
        ("TP_SOCKET_IFNAME", "enp1s0f1np1,enP2p1s0f1np1"),
        ("GLOO_SOCKET_IFNAME", "enp1s0f1np1,enP2p1s0f1np1"),
        ("VLLM_CACHE_ROOT", "/runtime-cache/vllm"),
        ("FLASHINFER_WORKSPACE_BASE", "/runtime-cache/flashinfer"),
        ("MTP_NUM_TOKENS", "5"),
        ("DEFAULT_THINKING", "low"),
        ("VLLM_USE_B12X_MOE", "1"),
    ):
        _require_equal(environment.get(name), expected, name)

    volumes = service.get("volumes", [])
    if not isinstance(volumes, list):
        raise QualityFailure("Compose volume list missing")
    normalized_volumes: list[dict[str, Any]] = []
    for volume in volumes:
        if not isinstance(volume, Mapping):
            raise QualityFailure("Compose volume entry is not an object")
        if volume.get("bind", {}) != {}:
            raise QualityFailure("Compose bind options mismatch")
        normalized_volumes.append(
            {
                "type": volume.get("type"),
                "source": volume.get("source"),
                "target": volume.get("target"),
                "read_only": volume.get("read_only", False),
            }
        )
    expected_volumes = [
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
            "read_only": False,
        },
        {
            "type": "bind",
            "source": "/srv/models/runtime-cache/deepseek-agent-dual/tmp",
            "target": "/tmp",
            "read_only": False,
        },
        {
            "type": "bind",
            "source": "/srv/models/outputs/deepseek-agent-dual",
            "target": "/outputs",
            "read_only": False,
        },
        {
            "type": "bind",
            "source": "/srv/models/logs/deepseek-agent-dual",
            "target": "/logs",
            "read_only": False,
        },
    ]
    _require_equal(normalized_volumes, expected_volumes, "volume list")
    snapshot_mount = next(
        (
            volume
            for volume in volumes
            if isinstance(volume, Mapping)
            and volume.get("target")
            == "/models/deepseek-ai/DeepSeek-V4-Flash-0731"
        ),
        None,
    )
    if not isinstance(snapshot_mount, Mapping):
        raise QualityFailure("Compose checkpoint mount missing")
    _require_equal(
        snapshot_mount.get("source"),
        "/srv/models/snapshots/deepseek-v4-flash-0731",
        "checkpoint source",
    )
    _require_equal(snapshot_mount.get("read_only"), True, "checkpoint read-only")
    writable_mounts = {
        "/runtime-cache": "/srv/models/runtime-cache/deepseek-agent-dual",
        "/tmp": "/srv/models/runtime-cache/deepseek-agent-dual/tmp",
        "/outputs": "/srv/models/outputs/deepseek-agent-dual",
        "/logs": "/srv/models/logs/deepseek-agent-dual",
    }
    for target, source in writable_mounts.items():
        mount = next(
            (
                volume
                for volume in volumes
                if isinstance(volume, Mapping) and volume.get("target") == target
            ),
            None,
        )
        if not isinstance(mount, Mapping):
            raise QualityFailure(f"Compose writable mount missing: {target}")
        _require_equal(mount.get("source"), source, f"{target} source")
        if mount.get("read_only", False) is not False:
            raise QualityFailure(f"Compose writable mount is read-only: {target}")

    command_value = service.get("command", [])
    if not isinstance(command_value, list):
        raise QualityFailure("Compose runtime command is not a list")
    command = " ".join(str(item) for item in command_value)
    required = (
        "--served-model-name deepseek",
        "--host 127.0.0.1",
        "--port 8888",
        "--tensor-parallel-size 2",
        "--pipeline-parallel-size 1",
        "--distributed-executor-backend mp",
        "--max-model-len 1048576",
        "--max-num-seqs 6",
        "--max-num-batched-tokens 8192",
        "--block-size 256",
        "--max-cudagraph-capture-size 36",
        "--gpu-memory-utilization 0.80",
        "--kv-cache-dtype nvfp4_ds_mla",
        "--enable-prefix-caching",
        "--enable-prompt-tokens-details",
        "--async-scheduling",
        "--enable-chunked-prefill",
        "--speculative-config '{\"method\":\"dspark\",\"num_speculative_tokens\":5,\"draft_sample_method\":\"probabilistic\"}'",
        "--tokenizer-mode deepseek_v4",
        "--moe-backend flashinfer_b12x",
        "--tool-call-parser deepseek_v4",
        "--enable-auto-tool-choice",
        "--reasoning-parser deepseek_v4",
        "--reasoning-config '{\"reasoning_parser\":\"deepseek_v4\",\"reasoning_start_str\":\"<think>\",\"reasoning_end_str\":\"</think>\"}'",
        "--default-chat-template-kwargs '{\"thinking\":true,\"reasoning_effort\":\"low\"}'",
        "--generation-config vllm",
        "--enable-flashinfer-autotune",
        "--nnodes 2",
        '--node-rank "$${NODE_RANK}"',
        '--master-addr "$${MASTER_ADDR}"',
        '--master-port "$${MASTER_PORT}"',
    )
    for argument in required:
        if argument not in command:
            raise QualityFailure(f"Compose runtime argument missing: {argument}")
    if role == "worker" and "--headless" not in command:
        raise QualityFailure("Compose worker is not headless")
    if role == "head" and "--headless" in command:
        raise QualityFailure("Compose head is unexpectedly headless")
    command_payload = json.dumps(
        command_value, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if hashlib.sha256(command_payload).hexdigest() != EXPECTED_COMMAND_SHA256[role]:
        raise QualityFailure("Compose runtime command mismatch")


def validate_models(payload: Mapping[str, Any]) -> None:
    data = payload.get("data")
    if not isinstance(data, list) or [item.get("id") for item in data] != ["deepseek"]:
        raise QualityFailure("API model identity mismatch")


def validate_startup_logs(logs: str) -> str:
    engine_lines = [
        line for line in logs.splitlines() if "Initializing a V1 LLM engine" in line
    ]
    if len(engine_lines) != 1:
        raise QualityFailure("startup logs do not contain exactly one engine config")
    engine = engine_lines[0]
    required = (
        "/models/deepseek-ai/DeepSeek-V4-Flash-0731",
        "max_seq_len=1048576",
        "tensor_parallel_size=2",
        "pipeline_parallel_size=1",
    )
    for pin in required:
        if pin not in engine:
            raise QualityFailure(f"startup logs do not prove {pin}")
    return engine


def _startup_identity(
    *, release_sha256: str, boot_id: str, container_id: str
) -> dict[str, str]:
    if re.fullmatch(r"[0-9a-f]{64}", release_sha256) is None:
        raise QualityFailure("release SHA-256 must contain 64 lowercase hex characters")
    if not boot_id.strip() or "/" in boot_id:
        raise QualityFailure("invalid boot ID")
    if re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
        raise QualityFailure("invalid container ID")
    return {
        "release_sha256": release_sha256,
        "boot_id": boot_id.strip(),
        "container_id": container_id,
    }


def record_startup_identity(
    *,
    output_path: Path,
    logs: str,
    release_sha256: str,
    boot_id: str,
    container_id: str,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": 1,
        "status": "validated",
        **_startup_identity(
            release_sha256=release_sha256,
            boot_id=boot_id,
            container_id=container_id,
        ),
        "engine_config": validate_startup_logs(logs),
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    _atomic_json(output_path, record, DEFAULT_MAX_EVIDENCE_BYTES)
    return record


def validate_startup_identity(
    *,
    record_path: Path,
    release_sha256: str,
    boot_id: str,
    container_id: str,
) -> None:
    expected = _startup_identity(
        release_sha256=release_sha256,
        boot_id=boot_id,
        container_id=container_id,
    )
    record = _load_json(record_path)
    if record.get("status") != "validated":
        raise QualityFailure("startup identity status mismatch")
    for name, value in expected.items():
        if record.get(name) != value:
            raise QualityFailure(f"startup identity {name} mismatch")
    engine_config = record.get("engine_config")
    if not isinstance(engine_config, str):
        raise QualityFailure("startup identity engine config is missing")
    validate_startup_logs(engine_config)


def _mem_available_bytes(path: Path) -> int:
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"MemAvailable:\s+([0-9]+)\s+kB", line)
        if match:
            return int(match.group(1)) * 1024
    raise QualityFailure("MemAvailable is missing from meminfo")


def _lifecycle_identity(
    *, release_sha256: str, boot_id: str, role: str
) -> dict[str, str]:
    if re.fullmatch(r"[0-9a-f]{64}", release_sha256) is None:
        raise QualityFailure("release SHA-256 must contain 64 lowercase hex characters")
    if not boot_id.strip() or "/" in boot_id:
        raise QualityFailure("invalid boot ID")
    if role not in ("head", "worker"):
        raise QualityFailure("invalid role")
    return {
        "release_sha256": release_sha256,
        "boot_id": boot_id.strip(),
        "role": role,
    }


def record_memory_baseline(
    *,
    output_path: Path,
    meminfo_path: Path,
    release_sha256: str,
    boot_id: str,
    role: str,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": 1,
        "status": "baseline",
        **_lifecycle_identity(
            release_sha256=release_sha256, boot_id=boot_id, role=role
        ),
        "mem_available_bytes": _mem_available_bytes(meminfo_path),
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    _atomic_json(output_path, record, DEFAULT_MAX_EVIDENCE_BYTES)
    return record


def record_release_memory(
    *,
    baseline_path: Path,
    output_path: Path,
    meminfo_path: Path,
    release_sha256: str,
    boot_id: str,
    role: str,
    tolerance_bytes: int,
) -> bool:
    identity = _lifecycle_identity(
        release_sha256=release_sha256, boot_id=boot_id, role=role
    )
    if tolerance_bytes < 0:
        raise QualityFailure("memory tolerance cannot be negative")
    current = _mem_available_bytes(meminfo_path)
    if not baseline_path.exists():
        record: dict[str, Any] = {
            "schema_version": 1,
            "status": "not-started",
            **identity,
            "mem_available_bytes": current,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        recovered = True
    else:
        baseline = _load_json(baseline_path)
        for name, expected in identity.items():
            if baseline.get(name) != expected:
                raise QualityFailure(f"memory baseline {name} mismatch")
        baseline_bytes = baseline.get("mem_available_bytes")
        if not isinstance(baseline_bytes, int) or baseline_bytes < 0:
            raise QualityFailure("invalid memory baseline")
        recovered = current >= baseline_bytes - tolerance_bytes
        record = {
            "schema_version": 1,
            "status": "recovered" if recovered else "pending",
            **identity,
            "baseline_mem_available_bytes": baseline_bytes,
            "mem_available_bytes": current,
            "tolerance_bytes": tolerance_bytes,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
    _atomic_json(output_path, record, DEFAULT_MAX_EVIDENCE_BYTES)
    return recovered


def _http_request(base_url: str, case: Mapping[str, Any]) -> object:
    request_body = json.dumps(case["request"], separators=(",", ":")).encode()
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v1/chat/completions",
        data=request_body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        if case["request"].get("stream") is True:
            chunks: list[object] = []
            total = 0
            for raw_line in response:
                total += len(raw_line)
                if total > MAX_HTTP_BYTES:
                    raise QualityFailure("stream response exceeds bounded input")
                line = raw_line.decode("utf-8").strip()
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                chunks.append("[DONE]" if payload == "[DONE]" else json.loads(payload))
            return chunks
        payload = response.read(MAX_HTTP_BYTES + 1)
        if len(payload) > MAX_HTTP_BYTES:
            raise QualityFailure("response exceeds bounded input")
        return json.loads(payload)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="operation", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--fixtures", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--base-url", default="http://127.0.0.1:8888")
    run.add_argument("--release-sha256", required=True)
    run.add_argument(
        "--boot-id-file",
        type=Path,
        default=Path("/proc/sys/kernel/random/boot_id"),
    )
    compose = subparsers.add_parser("validate-compose")
    compose.add_argument("--role", choices=("head", "worker"), required=True)
    subparsers.add_parser("validate-models")
    subparsers.add_parser("validate-logs")
    record_startup = subparsers.add_parser("record-startup")
    record_startup.add_argument("--output", type=Path, required=True)
    record_startup.add_argument("--release-sha256", required=True)
    record_startup.add_argument("--boot-id-file", type=Path, required=True)
    record_startup.add_argument("--container-id", required=True)
    validate_startup = subparsers.add_parser("validate-startup-record")
    validate_startup.add_argument("--record", type=Path, required=True)
    validate_startup.add_argument("--release-sha256", required=True)
    validate_startup.add_argument("--boot-id-file", type=Path, required=True)
    validate_startup.add_argument("--container-id", required=True)
    for operation in ("record-baseline", "verify-memory"):
        lifecycle = subparsers.add_parser(operation)
        lifecycle.add_argument("--output", type=Path, required=True)
        lifecycle.add_argument("--meminfo", type=Path, required=True)
        lifecycle.add_argument("--release-sha256", required=True)
        lifecycle.add_argument("--boot-id-file", type=Path, required=True)
        lifecycle.add_argument("--role", choices=("head", "worker"), required=True)
        if operation == "verify-memory":
            lifecycle.add_argument("--baseline", type=Path, required=True)
            lifecycle.add_argument("--tolerance-bytes", type=int, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.operation == "run":
        run_quality(
            fixtures_path=args.fixtures,
            output_path=args.output,
            request=lambda case: _http_request(args.base_url, case),
            release_sha256=args.release_sha256,
            boot_id=args.boot_id_file.read_text(encoding="utf-8").strip(),
        )
        print(args.output)
    elif args.operation == "validate-compose":
        validate_compose(json.load(sys.stdin), args.role)
    elif args.operation == "validate-models":
        validate_models(json.load(sys.stdin))
    elif args.operation == "validate-logs":
        validate_startup_logs(sys.stdin.read())
    elif args.operation == "record-startup":
        record_startup_identity(
            output_path=args.output,
            logs=sys.stdin.read(),
            release_sha256=args.release_sha256,
            boot_id=args.boot_id_file.read_text(encoding="utf-8").strip(),
            container_id=args.container_id,
        )
    elif args.operation == "validate-startup-record":
        validate_startup_identity(
            record_path=args.record,
            release_sha256=args.release_sha256,
            boot_id=args.boot_id_file.read_text(encoding="utf-8").strip(),
            container_id=args.container_id,
        )
    elif args.operation == "record-baseline":
        record_memory_baseline(
            output_path=args.output,
            meminfo_path=args.meminfo,
            release_sha256=args.release_sha256,
            boot_id=args.boot_id_file.read_text(encoding="utf-8").strip(),
            role=args.role,
        )
    elif args.operation == "verify-memory":
        recovered = record_release_memory(
            baseline_path=args.baseline,
            output_path=args.output,
            meminfo_path=args.meminfo,
            release_sha256=args.release_sha256,
            boot_id=args.boot_id_file.read_text(encoding="utf-8").strip(),
            role=args.role,
            tolerance_bytes=args.tolerance_bytes,
        )
        if not recovered:
            return 75
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
