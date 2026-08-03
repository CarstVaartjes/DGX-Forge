import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/verify-supply-chain"


def _copy(tmp_path: Path) -> Path:
    target = tmp_path / "repo"
    for path in (
        "control/uv.lock", "control/web/package-lock.json", "control/Dockerfile",
        "deploy/compose/compose.yaml", "deploy/compose/images.lock.json",
        "deploy/compose/trust/litellm-cosign.pub",
    ):
        destination = target / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / path, destination)
    subprocess.run([SCRIPT, "--root", target, "--generate", "--json"], check=True, capture_output=True, text=True)
    return target


def test_verifier_accepts_locked_offline_evidence(tmp_path: Path) -> None:
    repository = _copy(tmp_path)
    result = subprocess.run([SCRIPT, "--root", repository, "--json"], capture_output=True, text=True)
    assert result.returncode == 0
    assert '"ok":true' in result.stdout


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
