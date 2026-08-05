from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/verify-public-image-inputs"


def run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [SCRIPT, str(root)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_repository_public_image_inputs_are_clean() -> None:
    result = run(ROOT)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "public image inputs: PASS\n"


def test_live_token_pattern_is_rejected_without_echoing_value(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    source = repository / "control/src/dgx_control"
    source.mkdir(parents=True)
    value = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    (source / "leak.py").write_text(f'KEY = "{value}"\n')

    result = run(repository)

    assert result.returncode == 1
    assert "control/src/dgx_control/leak.py: github-token" in result.stderr
    assert value not in result.stderr


def test_private_key_header_is_rejected_without_echoing_content(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    source = repository / "control/src/dgx_control"
    source.mkdir(parents=True)
    value = "-----BEGIN OPENSSH PRIVATE KEY-----"
    (source / "leak.py").write_text(f'KEY = "{value}"\n')

    result = run(repository)

    assert result.returncode == 1
    assert "control/src/dgx_control/leak.py: private-key" in result.stderr
    assert value not in result.stderr
