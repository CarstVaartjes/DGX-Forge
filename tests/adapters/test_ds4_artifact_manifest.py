import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from adapters.deepseek.ds4.tools import artifact_manifest
from adapters.deepseek.ds4.tools.artifact_manifest import verify_manifest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "adapters/deepseek/ds4/tools/artifact_manifest.py"
)
BASE_PATH = "base.gguf"
DRAFTER_PATH = "drafter.gguf"
PRODUCTION_MANIFEST = (
    Path(__file__).resolve().parents[2]
    / "adapters/deepseek/ds4/manifests/deepseek-v4-flash-0731-ds4.json"
)


def _entry(name: str, path: str, content: bytes) -> dict[str, object]:
    return {
        "name": name,
        "repository": f"example/{name}",
        "revision": "a" * 40,
        "path": path,
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _manifest(base: bytes, drafter: bytes) -> dict[str, object]:
    return {
        "schema_version": 1,
        "artifacts": [
            _entry("base", BASE_PATH, base),
            _entry("drafter", DRAFTER_PATH, drafter),
        ],
        "total_bytes": len(base) + len(drafter),
    }


def _write_manifest(root: Path, manifest: dict[str, object]) -> Path:
    path = root / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _run_verify(manifest: Path, root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "verify",
            "--manifest",
            str(manifest),
            "--root",
            str(root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_verifies_a_valid_two_artifact_root(tmp_path: Path) -> None:
    base = b"base artifact"
    drafter = b"drafter artifact"
    (tmp_path / BASE_PATH).write_bytes(base)
    (tmp_path / DRAFTER_PATH).write_bytes(drafter)
    manifest = _write_manifest(tmp_path, _manifest(base, drafter))

    report = verify_manifest(manifest, tmp_path)
    completed = _run_verify(manifest, tmp_path)

    assert report == {
        "artifacts": [
            {"path": BASE_PATH, "status": "verified"},
            {"path": DRAFTER_PATH, "status": "verified"},
        ],
        "ok": True,
        "total_bytes": len(base) + len(drafter),
    }
    assert completed.returncode == 0
    assert json.loads(completed.stdout) == report
    assert completed.stderr == ""


def test_reports_a_missing_drafter_and_still_checks_the_base(tmp_path: Path) -> None:
    base = b"base artifact"
    drafter = b"drafter artifact"
    (tmp_path / BASE_PATH).write_bytes(base)
    manifest = _write_manifest(tmp_path, _manifest(base, drafter))

    report = verify_manifest(manifest, tmp_path)
    completed = _run_verify(manifest, tmp_path)

    assert report == {
        "artifacts": [
            {"path": BASE_PATH, "status": "verified"},
            {"path": DRAFTER_PATH, "status": "missing"},
        ],
        "ok": False,
        "total_bytes": len(base) + len(drafter),
    }
    assert completed.returncode == 1
    assert json.loads(completed.stdout) == report


def test_reports_a_wrong_base_size_without_hashing_it(tmp_path: Path) -> None:
    base = b"base artifact"
    drafter = b"drafter artifact"
    (tmp_path / BASE_PATH).write_bytes(base + b" changed")
    (tmp_path / DRAFTER_PATH).write_bytes(drafter)
    manifest = _write_manifest(tmp_path, _manifest(base, drafter))

    report = verify_manifest(manifest, tmp_path)
    completed = _run_verify(manifest, tmp_path)

    assert report == {
        "artifacts": [
            {"path": BASE_PATH, "status": "size_mismatch"},
            {"path": DRAFTER_PATH, "status": "verified"},
        ],
        "ok": False,
        "total_bytes": len(base) + len(drafter),
    }
    assert completed.returncode == 1
    assert json.loads(completed.stdout) == report


def test_reports_a_changed_base_digest_after_the_size_matches(tmp_path: Path) -> None:
    base = b"base artifact"
    changed_base = b"base artifacT"
    drafter = b"drafter artifact"
    (tmp_path / BASE_PATH).write_bytes(changed_base)
    (tmp_path / DRAFTER_PATH).write_bytes(drafter)
    manifest = _write_manifest(tmp_path, _manifest(base, drafter))

    report = verify_manifest(manifest, tmp_path)
    completed = _run_verify(manifest, tmp_path)

    assert report == {
        "artifacts": [
            {"path": BASE_PATH, "status": "sha256_mismatch"},
            {"path": DRAFTER_PATH, "status": "verified"},
        ],
        "ok": False,
        "total_bytes": len(base) + len(drafter),
    }
    assert completed.returncode == 1
    assert json.loads(completed.stdout) == report


@pytest.mark.parametrize("unsafe_path", ["/absolute.gguf", "../outside.gguf"])
def test_rejects_unsafe_artifact_paths(
    tmp_path: Path, unsafe_path: str
) -> None:
    base = b"base artifact"
    drafter = b"drafter artifact"
    manifest_data = _manifest(base, drafter)
    manifest_data["artifacts"][0]["path"] = unsafe_path  # type: ignore[index]
    manifest = _write_manifest(tmp_path, manifest_data)

    with pytest.raises(ValueError, match="unsafe manifest path"):
        verify_manifest(manifest, tmp_path)


def test_rejects_duplicate_artifact_paths(tmp_path: Path) -> None:
    base = b"base artifact"
    drafter = b"drafter artifact"
    manifest_data = _manifest(base, drafter)
    manifest_data["artifacts"][1]["path"] = BASE_PATH  # type: ignore[index]
    manifest = _write_manifest(tmp_path, manifest_data)

    with pytest.raises(ValueError, match="duplicate manifest path"):
        verify_manifest(manifest, tmp_path)


def test_rejects_unknown_json_keys(tmp_path: Path) -> None:
    manifest_data = _manifest(b"base artifact", b"drafter artifact")
    manifest_data["surprise"] = True
    manifest = _write_manifest(tmp_path, manifest_data)

    with pytest.raises(ValueError, match="unknown manifest keys"):
        verify_manifest(manifest, tmp_path)


@pytest.mark.parametrize("artifact_path", [BASE_PATH, DRAFTER_PATH])
def test_rejects_a_symlink_in_place_of_an_artifact(
    tmp_path: Path, artifact_path: str
) -> None:
    base = b"base artifact"
    drafter = b"drafter artifact"
    (tmp_path / BASE_PATH).write_bytes(base)
    (tmp_path / DRAFTER_PATH).write_bytes(drafter)
    replacement = tmp_path / "replacement.gguf"
    replacement.write_bytes(base if artifact_path == BASE_PATH else drafter)
    (tmp_path / artifact_path).unlink()
    os.symlink(replacement, tmp_path / artifact_path)
    manifest = _write_manifest(tmp_path, _manifest(base, drafter))

    report = verify_manifest(manifest, tmp_path)

    assert report["ok"] is False
    assert report["artifacts"] == [
        {
            "path": BASE_PATH,
            "status": "unsafe",
        }
        if artifact_path == BASE_PATH
        else {"path": BASE_PATH, "status": "verified"},
        {
            "path": DRAFTER_PATH,
            "status": "unsafe",
        }
        if artifact_path == DRAFTER_PATH
        else {"path": DRAFTER_PATH, "status": "verified"},
    ]


def test_rejects_a_directory_in_place_of_an_artifact(tmp_path: Path) -> None:
    base = b"base artifact"
    drafter = b"drafter artifact"
    (tmp_path / BASE_PATH).mkdir()
    (tmp_path / DRAFTER_PATH).write_bytes(drafter)
    manifest = _write_manifest(tmp_path, _manifest(base, drafter))

    report = verify_manifest(manifest, tmp_path)

    assert report == {
        "artifacts": [
            {"path": BASE_PATH, "status": "unsafe"},
            {"path": DRAFTER_PATH, "status": "verified"},
        ],
        "ok": False,
        "total_bytes": len(base) + len(drafter),
    }


def test_fails_closed_when_no_follow_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = b"base artifact"
    drafter = b"drafter artifact"
    (tmp_path / BASE_PATH).write_bytes(base)
    (tmp_path / DRAFTER_PATH).write_bytes(drafter)
    manifest = _write_manifest(tmp_path, _manifest(base, drafter))
    monkeypatch.delattr(artifact_manifest.os, "O_NOFOLLOW", raising=False)

    def forbid_open(*args: object, **kwargs: object) -> int:
        raise AssertionError("artifacts must not be opened without O_NOFOLLOW")

    monkeypatch.setattr(artifact_manifest.os, "open", forbid_open)

    report = verify_manifest(manifest, tmp_path)

    assert report == {
        "artifacts": [
            {"path": BASE_PATH, "status": "unsafe"},
            {"path": DRAFTER_PATH, "status": "unsafe"},
        ],
        "ok": False,
        "total_bytes": len(base) + len(drafter),
    }


def test_checked_production_manifest_has_every_audited_pin(tmp_path: Path) -> None:
    with PRODUCTION_MANIFEST.open(encoding="utf-8") as manifest_file:
        manifest = json.load(manifest_file)

    report = verify_manifest(PRODUCTION_MANIFEST, tmp_path)

    assert manifest == {
        "schema_version": 1,
        "artifacts": [
            {
                "name": "base",
                "repository": "antirez/deepseek-v4-gguf",
                "revision": "1cd7b564460821938add0475a60b942c409295e0",
                "path": "DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix-0731.gguf",
                "size": 86720111488,
                "sha256": "ca22ae2f838e14077c22bc1c1417b71b45b5e5a3687bd96c2ac6e17fdb6261c0",
            },
            {
                "name": "drafter",
                "repository": "bleysg/DeepSeek-V4-Flash-DSpark-drafter-GGUF",
                "revision": "81c6fdd38f9582da45ba27f0ed7b63bcd3ea3b62",
                "path": "DSpark-drafter-Q2K-Q8-0731.gguf",
                "size": 6971241504,
                "sha256": "8fa269560dc76fd73e4233ad9b1938b5f65dd363381fd9b1a5c6183f7d12d686",
            },
        ],
        "total_bytes": 93691352992,
    }
    assert report == {
        "artifacts": [
            {
                "path": "DSpark-drafter-Q2K-Q8-0731.gguf",
                "status": "missing",
            },
            {
                "path": "DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix-0731.gguf",
                "status": "missing",
            },
        ],
        "ok": False,
        "total_bytes": 93691352992,
    }
