from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "scripts/generate-control-clients"
OPENAPI = ROOT / "control/openapi.json"
PYTHON_CLIENT = ROOT / "src/spark_profiles/generated_control"
TYPESCRIPT_CLIENT = ROOT / "control/web/src/api/generated.d.ts"


def _generate() -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "PYTHONHASHSEED": "0", "SOURCE_DATE_EPOCH": "0"}
    return subprocess.run(
        [os.fspath(GENERATOR)],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _digests() -> dict[str, str]:
    artifacts = [OPENAPI, TYPESCRIPT_CLIENT]
    artifacts.extend(
        path
        for path in PYTHON_CLIENT.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )
    return {
        path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in artifacts
    }


def test_generator_is_idempotent_and_admin_schema_is_secret_free() -> None:
    tracked_digests = _digests()
    first = _generate()
    assert first.returncode == 0, first.stderr
    first_digests = _digests()
    assert first_digests == tracked_digests, "generated clients or OpenAPI drifted"
    second = _generate()
    assert second.returncode == 0, second.stderr
    assert _digests() == first_digests

    schema = json.loads(OPENAPI.read_text())
    assert set(schema["paths"]) >= {
        "/api/v1/agents",
        "/api/v1/endpoints/{alias}",
        "/api/v1/fleet",
        "/api/v1/jobs/{job_id}",
        "/api/v1/jobs/{job_id}/logs",
        "/api/v1/jobs/{job_id}/resume",
        "/api/v1/nodes/status",
        "/api/v1/profiles/{profile_id}/plan",
        "/api/v1/reconciliations",
        "/api/v1/reconciliations/plan",
    }
    assert all(path.startswith("/api/v1/") for path in schema["paths"])
    operations = [
        operation
        for path in schema["paths"].values()
        for method, operation in path.items()
        if method in {"delete", "get", "patch", "post", "put"}
    ]
    operation_ids = [operation["operationId"] for operation in operations]
    assert len(operation_ids) == len(set(operation_ids))
    assert all("_api_v1_" not in operation_id for operation_id in operation_ids)
    assert schema["components"]["securitySchemes"] == {
        "BearerAuth": {"scheme": "bearer", "type": "http"}
    }
    assert all(operation["security"] == [{"BearerAuth": []}] for operation in operations)

    by_id = {operation["operationId"]: operation for operation in operations}
    for operation_id in (
        "applyReconciliation",
        "getFleetStatus",
        "getJob",
        "getNodeStatuses",
        "getPublishedEndpoint",
        "listAgents",
        "listJobLogs",
        "listJobs",
        "planProfileReconciliation",
        "planReconciliation",
        "resumeJob",
    ):
        response_schema = next(
            response["content"]["application/json"]["schema"]
            for status, response in sorted(by_id[operation_id]["responses"].items())
            if status.startswith("2")
        )
        reference = response_schema["$ref"]
        component = schema["components"]["schemas"][reference.rsplit("/", 1)[-1]]
        assert component["additionalProperties"] is False

    serialized = json.dumps(schema, sort_keys=True).lower()
    for forbidden in (
        "/agent/v1/",
        "certificate_pem",
        "chain_pem",
        "csr_pem",
        "grant_token",
        "management_address",
        "operation_payload",
        "token_digest",
    ):
        assert forbidden not in serialized


def test_generated_python_models_compile() -> None:
    generated = _generate()
    assert generated.returncode == 0, generated.stderr
    result = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            "control",
            "--frozen",
            "python",
            "-c",
            (
                "from pathlib import Path; root=Path('src/spark_profiles/generated_control'); "
                "[(compile(path.read_text(), str(path), 'exec')) for path in root.rglob('*.py')]"
            ),
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not list(PYTHON_CLIENT.rglob("__pycache__"))


def test_generated_python_client_imports_in_the_root_locked_environment() -> None:
    result = subprocess.run(
        [
            "uv",
            "run",
            "--frozen",
            "python",
            "-c",
            "from spark_profiles.generated_control.client import AuthenticatedClient",
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
