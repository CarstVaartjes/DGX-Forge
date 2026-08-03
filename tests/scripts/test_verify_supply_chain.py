import hashlib
import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/verify-supply-chain"


def _copy(tmp_path: Path) -> Path:
    target = tmp_path / "repo"
    for path in (
        "agent/pyproject.toml", "agent/uv.lock", "agent_protocol/pyproject.toml",
        ".dockerignore", "agent_protocol/uv.lock", "control/pyproject.toml", "control/uv.lock",
        "control/web/package-lock.json", "control/Dockerfile",
        "deploy/compose/compose.yaml", "deploy/compose/images.lock.json",
        "deploy/compose/trust/litellm-cosign.pub",
    ):
        destination = target / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / path, destination)
    shutil.copytree(
        ROOT / "agent_protocol/src",
        target / "agent_protocol/src",
    )
    subprocess.run([SCRIPT, "--root", target, "--generate", "--json"], check=True, capture_output=True, text=True)
    return target


def test_verifier_accepts_locked_offline_evidence(tmp_path: Path) -> None:
    repository = _copy(tmp_path)
    result = subprocess.run([SCRIPT, "--root", repository, "--json"], capture_output=True, text=True)
    assert result.returncode == 0
    assert '"ok":true' in result.stdout
    assert "inventory/sbom/agent-protocol.spdx.json" in result.stdout


def test_verifier_rejects_floating_image(tmp_path: Path) -> None:
    repository = _copy(tmp_path)
    compose = repository / "deploy/compose/compose.yaml"
    text = compose.read_text()
    locked = "caddy:2.10.2@sha256:c3d7ee5d2b11f9dc54f947f68a734c84e9c9666c92c88a7f30b9cba5da182adb"
    compose.write_text(text.replace(locked, "caddy:latest"))
    result = subprocess.run([SCRIPT, "--root", repository], capture_output=True, text=True)
    assert result.returncode != 0
    assert "digest" in result.stderr or "floating" in result.stderr


def test_verifier_rejects_stale_sbom_after_lock_change(tmp_path: Path) -> None:
    repository = _copy(tmp_path)
    lock = repository / "control/web/package-lock.json"
    lock.write_text(lock.read_text() + "\n")
    result = subprocess.run([SCRIPT, "--root", repository], capture_output=True, text=True)
    assert result.returncode != 0
    assert "SBOM" in result.stderr or "manifest" in result.stderr


def test_verifier_rejects_protocol_wheel_or_lock_drift(tmp_path: Path) -> None:
    repository = _copy(tmp_path)
    source = repository / "agent_protocol/src/dgx_agent_protocol/contracts.py"
    source.write_text(source.read_text() + "\n# package drift\n")

    result = subprocess.run([SCRIPT, "--root", repository], capture_output=True, text=True)

    assert result.returncode != 0
    assert "wheel" in result.stderr


def test_verifier_rejects_a_missing_protocol_wheel_artifact(tmp_path: Path) -> None:
    repository = _copy(tmp_path)
    wheel = repository / "inventory/wheels/dgx_agent_protocol-1.0.0-py3-none-any.whl"
    assert wheel.is_file()
    wheel.unlink()

    result = subprocess.run([SCRIPT, "--root", repository], capture_output=True, text=True)

    assert result.returncode != 0
    assert "wheel" in result.stderr


def test_verifier_rejects_a_byte_different_protocol_wheel_with_the_same_name_and_version(tmp_path: Path) -> None:
    repository = _copy(tmp_path)
    wheel = repository / "inventory/wheels/dgx_agent_protocol-1.0.0-py3-none-any.whl"
    wheel.write_bytes(wheel.read_bytes() + b"different bytes")

    result = subprocess.run([SCRIPT, "--root", repository], capture_output=True, text=True)

    assert result.returncode != 0
    assert "wheel" in result.stderr


def test_protocol_spdx_records_the_verified_wheel_checksum(tmp_path: Path) -> None:
    repository = _copy(tmp_path)
    wheel = repository / "inventory/wheels/dgx_agent_protocol-1.0.0-py3-none-any.whl"
    document = json.loads((repository / "inventory/sbom/agent-protocol.spdx.json").read_text())
    protocol = next(package for package in document["packages"] if package["name"] == "dgx-agent-protocol")

    checksum = hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert protocol["checksums"] == [{"algorithm": "SHA256", "checksumValue": checksum}]
    wheel_file = next(file for file in document["files"] if file["fileName"] == "inventory/wheels/dgx_agent_protocol-1.0.0-py3-none-any.whl")
    assert wheel_file["checksums"] == [{"algorithm": "SHA256", "checksumValue": checksum}]
    assert {
        "spdxElementId": protocol["SPDXID"],
        "relationshipType": "GENERATED_FROM",
        "relatedSpdxElement": wheel_file["SPDXID"],
    } in document["relationships"]


def test_verifier_rejects_a_root_dockerignore_change(tmp_path: Path) -> None:
    repository = _copy(tmp_path)
    dockerignore = repository / ".dockerignore"
    dockerignore.write_text(dockerignore.read_text() + "\n!control/src/.env\n")

    result = subprocess.run([SCRIPT, "--root", repository], capture_output=True, text=True)

    assert result.returncode != 0
    assert "manifest" in result.stderr


def test_verifier_rejects_a_protocol_lock_hash_that_does_not_match_the_wheel(tmp_path: Path) -> None:
    repository = _copy(tmp_path)
    lock = repository / "agent/uv.lock"
    lock.write_text(lock.read_text().replace("10906428efdc60b9f55e9e78ef876e72310353207068cead4383e5a7250c5513", "0" * 64))

    result = subprocess.run([SCRIPT, "--root", repository, "--generate"], capture_output=True, text=True)

    assert result.returncode != 0
    assert "wheel" in result.stderr


def test_verifier_rejects_a_dockerfile_that_copies_but_does_not_install_the_protocol_wheel(tmp_path: Path) -> None:
    repository = _copy(tmp_path)
    dockerfile = repository / "control/Dockerfile"
    wheel = "/wheels/dgx_agent_protocol-1.0.0-py3-none-any.whl"
    dockerfile.write_text(dockerfile.read_text().replace(f"    {wheel} .", "    ."))

    result = subprocess.run([SCRIPT, "--root", repository, "--generate"], capture_output=True, text=True)

    assert result.returncode != 0
    assert "install" in result.stderr
