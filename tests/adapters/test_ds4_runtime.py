from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "adapters/deepseek/ds4/Dockerfile"
COMPOSE = ROOT / "adapters/deepseek/ds4/compose.yaml"
RUNTIME_ENV = ROOT / "adapters/deepseek/ds4/config/runtime.env"
PATCH = ROOT / "adapters/deepseek/ds4/patches/served-model-name.patch"
ADAPTER = ROOT / "adapters/deepseek/ds4/bin/ds4-deepseek-single"
CHECKPOINT_MANIFEST = (
    ROOT / "adapters/deepseek/ds4/manifests/deepseek-v4-flash-0731-ds4.json"
)
WORKLOAD = ROOT / "config/workloads/deepseek-agent-single.toml"

IMAGE = (
    "ghcr.io/carstvaartjes/spark-ds4"
    "@sha256:084d9a9ffa47431842c5dec84de97b058034dec0535b2a563bc5db78c9e14615"
)
CHECKPOINT_MANIFEST_SHA256 = (
    "1f6f88d7f968e51e76a118af83f0f7cae7f5df5b915a6cf30db5265228f70c99"
)

SOURCE_COMMIT = "4ad370b4a338efe9723a386673c0e04f6e214108"
SOURCE_ARCHIVE_SHA256 = "7db338d0a441fed36c5e4e7af44ff670e8bfe567e88d482f00ff6a3dc0e5dbe3"
BUILD_BASE = (
    "nvcr.io/nvidia/cuda:13.0.1-devel-ubuntu24.04"
    "@sha256:7d2f6a8c2071d911524f95061a0db363e24d27aa51ec831fcccf9e76eb72bc92"
)
RUNTIME_BASE = (
    "nvcr.io/nvidia/cuda:13.0.1-runtime-ubuntu24.04"
    "@sha256:c3fde347d52d578c84fd644bc177bc7ec333feaf11550d990da4084d7612e4c7"
)


def _runtime_fixture_source() -> str:
    return "\n" * 905 + """\\
#include <ctype.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct { int model_id; } ds4_engine;
typedef struct { int unused; } server_config;

static server_config parse_options(int argc, char **argv) {
    (void)argc;
    (void)argv;
    return (server_config){0};
}

static int ds4_engine_model_id(ds4_engine *engine) {
    return engine->model_id;
}

static bool model_alias_disables_thinking(const char *model) {
    return model && !strcmp(model, \"deepseek-chat\");
}

static bool model_alias_enables_thinking(const char *model) {
    return model && !strcmp(model, \"deepseek-reasoner\");
}

static const char *server_model_id_from_engine(ds4_engine *engine) {
    return ds4_engine_model_id(engine) == 1 ?
           \"deepseek-v4-pro\" : \"deepseek-v4-flash\";
}

static bool server_model_alias_known(const char *id) {
    return id &&
           (!strcmp(id, \"deepseek-v4-flash\") ||
            !strcmp(id, \"deepseek-v4-pro\"));
}

static void emit_models(ds4_engine *engine) {
    printf(\"models=%s default=%s detail=%d wrong_detail=%d\\n\",
           server_model_id_from_engine(engine),
           server_model_id_from_engine(engine),
           server_model_alias_known(engine, server_model_id_from_engine(engine)),
           server_model_alias_known(engine, \"deepseek-v4-pro\"));
}
""" + "\n" * 12726 + """\\
#if 0
static void v053_model_detail_route(http_request hr, server *s) {
    if (!strcmp(hr.method, \"OPTIONS\")) {
        http_response(fd, s->enable_cors, 204, NULL, \"\");
        http_request_free(&hr);
        goto done;
    }

    if (!strcmp(hr.method, \"GET\") && !strcmp(hr.path, \"/metrics\")) {
        send_metrics(s, fd);
        http_request_free(&hr);
        goto done;
    }
    if (!strcmp(hr.method, \"GET\") && !strcmp(hr.path, \"/v1/stats\")) {
        send_stats(s, fd, hr.accept_json);
        http_request_free(&hr);
        goto done;
    }

    if (!strcmp(hr.method, \"GET\") && !strcmp(hr.path, \"/v1/models\")) {
        send_models(s, fd);
        http_request_free(&hr);
        goto done;
    }
    const char *model_path_prefix = \"/v1/models/\";
    const size_t model_path_prefix_len = strlen(model_path_prefix);
    if (!strcmp(hr.method, \"GET\") &&
        !strncmp(hr.path, model_path_prefix, model_path_prefix_len) &&
        server_model_alias_known(hr.path + model_path_prefix_len))
    {
        send_model(s, fd, hr.path + model_path_prefix_len);
    }
}
#endif
""" + "\n" * 878 + """\\
int main(int argc, char **argv) {
    server_config cfg = parse_options(argc, argv);
    ds4_engine engine = {0};
    (void)cfg;
    emit_models(&engine);
    return 0;
}
"""


def _apply_served_name_patch(tmp_path: Path) -> Path:
    source = tmp_path / "ds4_server.c"
    source.write_text(_runtime_fixture_source(), encoding="utf-8")

    # GNU patch, used by the pinned Ubuntu image build, places the second of these
    # adjacent zero-context insertion hunks at new line 952.  BSD patch rejects the
    # adjacency, while git apply requires that resolved new-line coordinate explicitly.
    patch_text = PATCH.read_text(encoding="utf-8")
    adjacent_hunk = "@@ -934,0 +951,6 @@"
    assert patch_text.count(adjacent_hunk) == 1
    portable_patch = tmp_path / "served-model-name-portable.patch"
    portable_patch.write_text(
        patch_text.replace(adjacent_hunk, "@@ -934,0 +952,6 @@"),
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "apply", "--unsafe-paths", "--unidiff-zero", str(portable_patch)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    return source


def _compile_runtime_fixture(tmp_path: Path) -> Path:
    compiler = os.environ.get("CC", "cc")
    if shutil.which(compiler) is None:
        if sys.platform == "darwin":
            pytest.skip("native C compiler unavailable on macOS; Spark ARM64 harness is run separately")
        pytest.fail(f"C compiler required for behavioral harness is unavailable: {compiler}")

    source = _apply_served_name_patch(tmp_path)
    binary = tmp_path / "ds4-runtime-fixture"
    completed = subprocess.run(
        [compiler, "-std=c99", "-Werror", "-o", str(binary), str(source)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        if sys.platform == "darwin":
            pytest.skip(
                "native C compiler unavailable on macOS; Spark ARM64 harness is run separately: "
                + completed.stderr.strip()
            )
        pytest.fail(f"behavioral harness compilation failed:\\n{completed.stderr}")
    return binary


def _run_fixture(binary: Path, served_model_name: str | None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if served_model_name is None:
        env.pop("DS4_SERVED_MODEL_NAME", None)
    else:
        env["DS4_SERVED_MODEL_NAME"] = served_model_name
    return subprocess.run([str(binary)], check=False, capture_output=True, text=True, env=env)


def test_runtime_recipe_uses_the_pinned_cuda_sources_and_safe_runtime_contract() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    compose = COMPOSE.read_text(encoding="utf-8")
    runtime_env = RUNTIME_ENV.read_text(encoding="utf-8")
    patch_sha256 = hashlib.sha256(PATCH.read_bytes()).hexdigest()

    assert f"FROM {BUILD_BASE}" in dockerfile
    assert f"FROM {RUNTIME_BASE}" in dockerfile
    assert f"https://github.com/Entrpi/ds4/archive/{SOURCE_COMMIT}.tar.gz" in dockerfile
    assert SOURCE_ARCHIVE_SHA256 in dockerfile
    assert f"ARG DS4_PATCH_SHA256={patch_sha256}" in dockerfile
    assert dockerfile.count(f"ARG DS4_PATCH_SHA256={patch_sha256}") == 2
    assert dockerfile.index("sha256sum -c") < dockerfile.index("tar -xzf")
    assert dockerfile.index('echo "${DS4_PATCH_SHA256}  /tmp/served-model-name.patch"') < dockerfile.index(
        "patch -p1 --input /tmp/served-model-name.patch"
    )
    assert "make cuda-spark" in dockerfile
    assert "DS4_CUDA_SPARK_HBM_CACHE=1" in dockerfile
    assert "compute_121a" in dockerfile
    assert "sm_121a" in dockerfile
    assert "DS4_NO_UPDATE_CHECK=1" in dockerfile
    assert "DS4_CUDA_COPY_MODEL" not in dockerfile
    assert "DS4_MODEL_ANON_HUGE=1" not in dockerfile
    assert "DS4_SERVED_MODEL_NAME=deepseek" in compose
    assert "DS4_NO_UPDATE_CHECK=1" in compose
    assert "DS4_CONT_MTP_MODE=2" in compose
    assert "DS4_CONT_DSPARK=1" in compose
    assert "DSpark-drafter-Q2K-Q8-0731.gguf" in compose
    assert "-c" in compose and "32768" in compose
    assert "--host" in compose and "127.0.0.1" in compose
    assert "--port" in compose and "8888" in compose
    assert "--kv-disk-dir" in compose and "--kv-disk-space-mb" in compose
    assert 'restart: "no"' in compose
    assert "network_mode: host" in compose
    assert "read_only: true" in compose
    assert "ports:" not in compose
    assert "DS4_IMAGE=ghcr.io/carstvaartjes/spark-ds4:ds4-v0.5.3-q2-0731-health" in runtime_env


def test_compose_renders_a_loopback_only_nonrestarting_service() -> None:
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE),
            "--env-file",
            str(RUNTIME_ENV),
            "config",
        ],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "DS4_MODELS_GID": str(os.getgid())},
    )

    assert 'restart: "no"' in completed.stdout
    assert "network_mode: host" in completed.stdout
    assert "published:" not in completed.stdout
    assert "127.0.0.1" in completed.stdout


def test_compose_adds_the_host_model_group_for_nonroot_mount_access() -> None:
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE),
            "--env-file",
            str(RUNTIME_ENV),
            "config",
        ],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "DS4_MODELS_GID": "4321"},
    )

    assert "group_add:" in completed.stdout
    assert '- "4321"' in completed.stdout


def test_served_model_patch_behaves_as_a_single_runtime_identity(tmp_path: Path) -> None:
    binary = _compile_runtime_fixture(tmp_path)

    upstream = _run_fixture(binary, None)
    assert upstream.returncode == 0
    assert upstream.stdout == (
        "models=deepseek-v4-flash default=deepseek-v4-flash detail=1 wrong_detail=0\n"
    )

    configured = _run_fixture(binary, "deepseek")
    assert configured.returncode == 0
    assert configured.stdout == "models=deepseek default=deepseek detail=1 wrong_detail=0\n"

    for invalid_name in ("", "deepseek\ninternal"):
        invalid = _run_fixture(binary, invalid_name)
        assert invalid.returncode == 2
        assert invalid.stdout == ""
        assert "DS4_SERVED_MODEL_NAME must be non-empty and contain no control characters" in invalid.stderr


def test_served_model_patch_adds_a_deterministic_health_route(tmp_path: Path) -> None:
    patched = _apply_served_name_patch(tmp_path).read_text(encoding="utf-8")

    assert '!strcmp(hr.method, "GET") && !strcmp(hr.path, "/health")' in patched
    assert (
        'http_response(fd, s->enable_cors, 200, "application/json", '
        '"{\\"status\\":\\"ok\\"}\\n")'
    ) in patched


def test_single_adapter_is_pinned_to_spark1_and_exposes_only_controller_operations(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [str(ADAPTER), "verify"],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "DS4_LOCAL_HOSTNAME": "spark-9999",
            "DS4_MODELS_ROOT": str(tmp_path / "models"),
        },
    )

    assert completed.returncode == 2
    assert "spark1/spark-3542" in completed.stderr
    assert "role" not in completed.stderr.lower()

    extra = subprocess.run(
        [str(ADAPTER), "verify", "head"],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "DS4_LOCAL_HOSTNAME": "spark-3542"},
    )
    assert extra.returncode == 2
    assert "exactly one operation" in extra.stderr


def test_single_adapter_contract_pins_resumable_parallel_streamed_preparation() -> None:
    source = ADAPTER.read_text(encoding="utf-8")

    assert IMAGE in source
    assert CHECKPOINT_MANIFEST_SHA256 in source
    assert "--continue-at -" in source
    assert ".partial" in source
    assert "wait \"$base_pid\"" in source
    assert "wait \"$drafter_pid\"" in source
    assert "sha256sum" in source
    assert "rm " not in source
    assert "DS4_CUDA_COPY_MODEL" in source
    assert "DS4_MODEL_ANON_HUGE" in source
    assert "--pull never" in source
    assert "restart" not in source
    assert 'DS4_MODELS_GID=$(stat -c %g "$models_root")' in source


def test_single_adapter_covers_exact_openai_identity_and_lifecycle_evidence() -> None:
    source = ADAPTER.read_text(encoding="utf-8")

    for operation in (
        "prepare)",
        "verify)",
        "start)",
        "health)",
        "infer)",
        "stop)",
        "verify-release)",
    ):
        assert operation in source
    for contract in (
        '"deepseek"',
        '"reasoning_effort":"low"',
        '"reasoning_effort":"high"',
        '"reasoning_effort":"max"',
        '"thinking":false',
        '"type":"function"',
        "mapped-no-copy",
        "boot_id",
        "container_id",
        "release_sha256",
        "1073741824",
    ):
        assert contract in source
    assert '"chat_template_kwargs"' not in source


def test_single_workload_values_match_the_adapter_and_checkpoint_contract() -> None:
    with WORKLOAD.open("rb") as source:
        workload = tomllib.load(source)
    manifest = json.loads(CHECKPOINT_MANIFEST.read_text(encoding="utf-8"))

    assert workload["id"] == "deepseek-agent-single"
    assert workload["topology"] == "single"
    assert workload["nodes"] == ["spark1"]
    assert workload["image"]["reference"] == IMAGE
    assert workload["checkpoint"]["manifest_sha256"] == CHECKPOINT_MANIFEST_SHA256
    assert manifest["total_bytes"] == 93_691_352_992
    assert workload["endpoint"] == {"host": "127.0.0.1", "port": 8888}
    for command in workload["commands"].values():
        assert len(command) == 2
        assert command[1] in {
            "prepare",
            "verify",
            "start",
            "health",
            "infer",
            "stop",
            "verify-release",
        }
