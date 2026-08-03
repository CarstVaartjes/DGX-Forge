from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "adapters/deepseek/ds4/Dockerfile"
COMPOSE = ROOT / "adapters/deepseek/ds4/compose.yaml"
RUNTIME_ENV = ROOT / "adapters/deepseek/ds4/config/runtime.env"
PATCH = ROOT / "adapters/deepseek/ds4/patches/served-model-name.patch"

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
    return "\n" * 913 + """\
#include <ctype.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct { int model_id; } ds4_engine;

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
    printf(\"models=%s default=%s detail=%d\\n\",
           server_model_id_from_engine(engine),
           server_model_id_from_engine(engine),
           server_model_alias_known(engine, \"deepseek\"));
}
""" + "\n" * 12726 + """\
#if 0
static void v053_model_detail_route(http_request hr, server *s) {
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

int main(void) {
    ds4_engine engine = {0};
    emit_models(&engine);
    return 0;
}
"""


def _apply_served_name_patch(tmp_path: Path) -> str:
    source = tmp_path / "ds4_server.c"
    source.write_text(_runtime_fixture_source(), encoding="utf-8")
    subprocess.run(
        ["patch", "-p1", "-i", str(PATCH)], cwd=tmp_path, check=True, capture_output=True
    )
    return source.read_text(encoding="utf-8")


def test_runtime_recipe_uses_the_pinned_cuda_sources_and_safe_runtime_contract() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    compose = COMPOSE.read_text(encoding="utf-8")
    runtime_env = RUNTIME_ENV.read_text(encoding="utf-8")

    assert f"FROM {BUILD_BASE}" in dockerfile
    assert f"FROM {RUNTIME_BASE}" in dockerfile
    assert f"https://github.com/Entrpi/ds4/archive/{SOURCE_COMMIT}.tar.gz" in dockerfile
    assert SOURCE_ARCHIVE_SHA256 in dockerfile
    assert dockerfile.index("sha256sum -c") < dockerfile.index("tar -xzf")
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
    assert "restart: \"no\"" in compose
    assert "network_mode: host" in compose
    assert "read_only: true" in compose
    assert "ports:" not in compose
    assert "DS4_IMAGE=ghcr.io/carstvaartjes/spark-ds4:ds4-v0.5.3-q2-0731" in runtime_env


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
    )

    assert "restart: \"no\"" in completed.stdout
    assert "network_mode: host" in completed.stdout
    assert "published:" not in completed.stdout
    assert "127.0.0.1" in completed.stdout


def test_served_model_patch_advertises_only_the_configured_name(tmp_path: Path) -> None:
    patched = _apply_served_name_patch(tmp_path)

    assert 'getenv("DS4_SERVED_MODEL_NAME")' in patched
    assert "if (server_model_id_is_valid(configured)) return configured;" in patched
    assert "server_model_alias_known(ds4_engine *engine, const char *id)" in patched
    assert "return id && !strcmp(id, server_model_id_from_engine(engine));" in patched
    assert "server_model_alias_known(s->engine, hr.path + model_path_prefix_len)" in patched
    assert "models=%s default=%s detail=%d\\n" in patched
    assert "server_model_id_from_engine(engine),\n           server_model_id_from_engine(engine)" in patched


def test_served_model_patch_rejects_empty_and_control_character_names(tmp_path: Path) -> None:
    patched = _apply_served_name_patch(tmp_path)

    assert "if (!id || !id[0]) return false;" in patched
    assert "for (const unsigned char *p = (const unsigned char *)id; *p; p++)" in patched
    assert "if (iscntrl(*p)) return false;" in patched
    assert '"deepseek-v4-pro" : "deepseek-v4-flash"' in patched
