from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SUPERVISOR = ROOT / "deploy/compose/litellm/config_supervisor.py"


def _module():
    spec = importlib.util.spec_from_file_location(
        "litellm_config_supervisor", SUPERVISOR
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bundle(module, tmp_path: Path, *, now: datetime, expires_at: datetime):
    root = tmp_path / "routes"
    config = b'{"model_list":[{"model_name":"chat"}]}\n'
    routes = b'{"routes":{"chat":{}},"state":"published"}\n'
    manifest = {
        "schema_version": 1,
        "generation": 1,
        "state": "published",
        "reconciliation_id": "bb7aac18-edbf-4cc1-bafd-15e282557c53",
        "plan_digest": "a" * 64,
        "evidence_set_digest": "b" * 64,
        "routes_sha256": hashlib.sha256(routes).hexdigest(),
        "litellm_sha256": hashlib.sha256(config).hexdigest(),
        "issued_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    manifest_bytes = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    directory_name = "00000001-" + manifest_digest
    directory = root / "generations" / directory_name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "litellm.json").write_bytes(config)
    (directory / "routes.json").write_bytes(routes)
    (directory / "manifest.json").write_bytes(manifest_bytes)
    activation = {
        **manifest,
        "directory": directory_name,
        "manifest_sha256": manifest_digest,
    }
    (root / "activation.json").write_text(
        json.dumps(activation, sort_keys=True, separators=(",", ":")) + "\n"
    )
    bootstrap = tmp_path / "bootstrap.json"
    bootstrap.write_bytes(b'{"model_list":[]}\n')
    module.ROOT = root
    module.ACTIVATION = root / "activation.json"
    module.GENERATIONS = root / "generations"
    module.BOOTSTRAP = bootstrap
    return directory / "litellm.json", bootstrap, directory


def test_supervisor_selects_only_an_exact_fresh_activation_bundle(
    tmp_path: Path,
) -> None:
    module = _module()
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    generated, _bootstrap, _directory = _bundle(
        module,
        tmp_path,
        now=now - timedelta(seconds=30),
        expires_at=now + timedelta(seconds=120),
    )

    assert module._selected(now=now) == generated


def test_supervisor_falls_back_for_expired_or_hash_mismatched_bundle(
    tmp_path: Path,
) -> None:
    module = _module()
    now = datetime(2026, 8, 5, 12, 3, tzinfo=UTC)
    generated, bootstrap, _directory = _bundle(
        module,
        tmp_path,
        now=now - timedelta(seconds=180),
        expires_at=now - timedelta(seconds=30),
    )
    assert module._selected(now=now) == bootstrap

    generated, bootstrap, _directory = _bundle(
        module,
        tmp_path,
        now=now,
        expires_at=now + timedelta(seconds=150),
    )
    generated.write_bytes(b'{"model_list":[{"unsafe":true}]}\n')
    assert module._selected(now=now) == bootstrap


def test_supervisor_rejects_a_lease_beyond_the_production_bound(
    tmp_path: Path,
) -> None:
    module = _module()
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    _generated, bootstrap, _directory = _bundle(
        module,
        tmp_path,
        now=now,
        expires_at=now + timedelta(seconds=301),
    )

    assert module._selected(now=now) == bootstrap


def test_supervisor_falls_back_when_manifest_or_marker_is_not_exact(
    tmp_path: Path,
) -> None:
    module = _module()
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    _generated, bootstrap, directory = _bundle(
        module,
        tmp_path,
        now=now,
        expires_at=now + timedelta(seconds=150),
    )
    manifest = json.loads((directory / "manifest.json").read_bytes())
    manifest["plan_digest"] = "f" * 64
    (directory / "manifest.json").write_text(json.dumps(manifest))
    assert module._selected(now=now) == bootstrap

    _generated, bootstrap, _directory = _bundle(
        module,
        tmp_path,
        now=now,
        expires_at=now + timedelta(seconds=150),
    )
    activation = json.loads(module.ACTIVATION.read_bytes())
    module.ACTIVATION.write_text(json.dumps(activation, indent=2))
    assert module._selected(now=now) == bootstrap

    _generated, bootstrap, _directory = _bundle(
        module,
        tmp_path,
        now=now,
        expires_at=now + timedelta(seconds=150),
    )
    activation = json.loads(module.ACTIVATION.read_bytes())
    activation["unknown"] = True
    module.ACTIVATION.write_text(json.dumps(activation))
    assert module._selected(now=now) == bootstrap


def test_compose_mounts_one_read_only_route_volume_and_starts_bounded_supervisor() -> (
    None
):
    compose = (ROOT / "deploy/compose/compose.yaml").read_text()
    entrypoint = (ROOT / "deploy/compose/litellm/entrypoint.sh").read_text()
    source = SUPERVISOR.read_text()

    assert "route-publications:/routes" in compose
    assert "config_supervisor.py:/app/config-supervisor.py:ro" in compose
    assert "bootstrap-config.json:/app/bootstrap-config.json:ro" in compose
    assert "exec python /app/config-supervisor.py" in entrypoint
    assert "POLL_SECONDS = 2" in source
    assert "TERMINATE_SECONDS = 30" in source
    assert "shell=True" not in source


def test_compose_initializes_route_volume_for_unprivileged_control_worker() -> None:
    environment = os.environ.copy()
    for line in (ROOT / "deploy/compose/tests/test.env").read_text().splitlines():
        name, value = line.split("=", 1)
        environment[name] = value
    rendered = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(ROOT / "deploy/compose/compose.yaml"),
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    services = json.loads(rendered.stdout)["services"]
    initializer = services["route-publication-init"]

    assert initializer["network_mode"] == "none"
    assert initializer["user"] == "0:0"
    assert initializer["cap_drop"] == ["ALL"]
    assert set(initializer["cap_add"]) == {"CHOWN", "FOWNER"}
    command = initializer["command"][-1]
    reclaim = "os.chown('/routes', 0, 0)"
    child = "os.chown('/routes/generations', 10001, 10001)"
    root = "os.chown('/routes', 10001, 10001)"
    assert command.index(reclaim) < command.index("os.makedirs")
    assert command.index(child) < command.index(root)
    assert services["control-worker"]["depends_on"]["route-publication-init"] == {
        "condition": "service_completed_successfully",
        "required": True,
    }
    assert services["litellm"]["depends_on"]["route-publication-init"] == {
        "condition": "service_completed_successfully",
        "required": True,
    }
