import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.vonk_identity import verify

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts/verify-vonk-identity"


def _legacy_token() -> str:
    return "sp" + "ark"


def test_identity_verifier_rejects_owned_legacy_token(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(f"vonk {_legacy_token()}ctl\n", encoding="utf-8")

    result = verify(tmp_path)

    assert result["status"] == "failed"
    assert f"{_legacy_token()}ctl" in result["owned_matches"][0]["text"]


def test_identity_verifier_scans_paths_and_sorts_matches(tmp_path: Path) -> None:
    legacy = _legacy_token()
    named_directory = tmp_path / "a"
    named_directory.mkdir()
    (named_directory / f"{legacy}-config.yml").write_text("clean\n", encoding="utf-8")
    (tmp_path / f"{legacy}-directory").mkdir()
    (tmp_path / "README.md").write_text(f"z {legacy}\na {legacy}\n", encoding="utf-8")

    result = verify(tmp_path)

    assert result["owned_matches"] == [
        {"line": 1, "path": "README.md", "text": f"z {legacy}"},
        {"line": 2, "path": "README.md", "text": f"a {legacy}"},
        {"line": 0, "path": f"a/{legacy}-config.yml", "text": f"a/{legacy}-config.yml"},
        {"line": 0, "path": f"{legacy}-directory", "text": f"{legacy}-directory"},
    ]


@pytest.mark.parametrize(
    "external_root", ("manifests", "inventory/raw", "tests/fixtures/external")
)
def test_identity_verifier_classifies_each_external_evidence_root(
    tmp_path: Path, external_root: str
) -> None:
    legacy = _legacy_token()
    evidence = tmp_path / external_root / f"{legacy}-evidence.txt"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(legacy, encoding="utf-8")

    result = verify(tmp_path)

    assert result["status"] == "passed"
    assert result["owned_matches"] == []
    assert {match["path"] for match in result["external_matches"]} == {
        f"{external_root}/{legacy}-evidence.txt"
    }


def test_identity_verifier_ignores_cache_build_and_binary_artifacts(tmp_path: Path) -> None:
    legacy = _legacy_token()
    cache = tmp_path / "compiler-cache"
    cache.mkdir()
    (cache / f"{legacy}-ignored.txt").write_text(legacy, encoding="utf-8")
    bytecode_cache = tmp_path / "__pycache__"
    bytecode_cache.mkdir()
    (bytecode_cache / f"{legacy}-ignored.txt").write_text(legacy, encoding="utf-8")
    build = tmp_path / "generated-build-output"
    build.mkdir()
    (build / f"{legacy}-ignored.txt").write_text(legacy, encoding="utf-8")
    (tmp_path / f"{legacy}-image.dat").write_bytes(b"\x89PNG\r\n\x1a\n" + legacy.encode())
    (tmp_path / f"{legacy}-payload.b64").write_bytes(base64.b64encode(legacy.encode()))

    result = verify(tmp_path)

    assert result == {
        "external_matches": [],
        "owned_matches": [],
        "status": "passed",
    }


def test_identity_verifier_uses_git_visibility_for_checkout_roots(tmp_path: Path) -> None:
    legacy = _legacy_token()
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("scratch/\n", encoding="utf-8")

    tracked = tmp_path / "owned" / "tracked.txt"
    tracked.parent.mkdir()
    tracked.write_text(legacy, encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)

    visible = tmp_path / "notes" / f"{legacy}-visible.txt"
    visible.parent.mkdir()
    visible.write_text("clean\n", encoding="utf-8")
    work_record = tmp_path / ".superpowers" / f"{legacy}-visible.txt"
    work_record.parent.mkdir()
    work_record.write_text("clean\n", encoding="utf-8")
    sibling_checkout = tmp_path / ".worktrees" / f"{legacy}-visible.txt"
    sibling_checkout.parent.mkdir()
    sibling_checkout.write_text("clean\n", encoding="utf-8")
    ignored = tmp_path / "scratch" / f"{legacy}-ignored.txt"
    ignored.parent.mkdir()
    ignored.write_text(legacy, encoding="utf-8")

    result = verify(tmp_path)

    paths = {match["path"] for match in result["owned_matches"]}
    assert str(tracked.relative_to(tmp_path)) in paths
    assert str(visible.relative_to(tmp_path)) in paths
    assert str(work_record.relative_to(tmp_path)) in paths
    assert str(sibling_checkout.relative_to(tmp_path)) in paths
    assert not any(path.startswith("scratch/") for path in paths)


def test_identity_cli_emits_sorted_json_and_nonzero_for_owned_matches(tmp_path: Path) -> None:
    legacy = _legacy_token()
    (tmp_path / "README.md").write_text(legacy, encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(CLI), "--json", str(tmp_path)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 1
    report = json.loads(completed.stdout)
    assert report["status"] == "failed"
    assert completed.stdout == json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n"
