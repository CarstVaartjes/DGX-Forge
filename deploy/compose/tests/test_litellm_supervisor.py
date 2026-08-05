from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SUPERVISOR = ROOT / "deploy/compose/litellm/config_supervisor.py"


def _module():
    spec = importlib.util.spec_from_file_location("litellm_config_supervisor", SUPERVISOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_lease(module, tmp_path, *, issued_at, expires_at, content=b'{"model_list":[]}\n'):
    generated = tmp_path / "config.yaml"
    lease = tmp_path / "lease.json"
    bootstrap = tmp_path / "bootstrap.yaml"
    generated.write_bytes(content)
    bootstrap.write_bytes(b'{"model_list":[]}\n')
    lease.write_text(json.dumps({
        "config_sha256": hashlib.sha256(content).hexdigest(),
        "issued_at": issued_at.isoformat(),
        "expires_at": expires_at.isoformat(),
    }))
    module.GENERATED = generated
    module.LEASE = lease
    module.BOOTSTRAP = bootstrap
    return generated, bootstrap


def test_supervisor_selects_only_a_fresh_hash_bound_lease(tmp_path) -> None:
    module = _module()
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    generated, _bootstrap = _write_lease(
        module,
        tmp_path,
        issued_at=now,
        expires_at=now + timedelta(seconds=150),
    )
    module.STARTED_AT = now - timedelta(seconds=1)

    assert module._selected(now=now) == generated


def test_supervisor_falls_back_after_lease_expiry(tmp_path) -> None:
    module = _module()
    now = datetime(2026, 8, 5, 12, 3, tzinfo=UTC)
    _generated, bootstrap = _write_lease(
        module,
        tmp_path,
        issued_at=now - timedelta(seconds=180),
        expires_at=now - timedelta(seconds=30),
    )
    module.STARTED_AT = now - timedelta(seconds=300)

    assert module._selected(now=now) == bootstrap


def test_supervisor_rejects_a_persisted_lease_from_before_startup(tmp_path) -> None:
    module = _module()
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    _generated, bootstrap = _write_lease(
        module,
        tmp_path,
        issued_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(seconds=149),
    )
    module.STARTED_AT = now

    assert module._selected(now=now) == bootstrap


def test_supervisor_rejects_a_lease_for_different_config_bytes(tmp_path) -> None:
    module = _module()
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    _generated, bootstrap = _write_lease(
        module,
        tmp_path,
        issued_at=now,
        expires_at=now + timedelta(seconds=150),
    )
    module.STARTED_AT = now
    module.GENERATED.write_bytes(b'{"model_list":[{"unsafe":true}]}\n')

    assert module._selected(now=now) == bootstrap
