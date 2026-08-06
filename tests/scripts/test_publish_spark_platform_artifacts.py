from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/publish-spark-platform-artifacts"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
SOURCE_SHA256 = "a" * 64


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _payloads(tmp_path: Path) -> Path:
    root = tmp_path / "payloads"
    root.mkdir()
    payloads = {
        "agent": ("dgx-agent", b"agent" * 20),
        "supervisor": ("dgx-agent-supervisor", b"supervisor" * 10),
        "tooling": ("dgx-forge-tooling", b"tooling" * 20),
    }
    receipt: dict[str, object] = {
        "architecture": "linux-arm64",
        "payloads": {},
        "schema_version": 1,
    }
    for key, (name, raw) in payloads.items():
        (root / name).write_bytes(raw)
        receipt["payloads"][key] = {  # type: ignore[index]
            "name": name,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
        }
    (root / "spark-platform-payloads.json").write_bytes(_canonical(receipt))
    return root


def _fake_oras(tmp_path: Path) -> tuple[Path, Path]:
    script = tmp_path / "oras"
    log = tmp_path / "oras.jsonl"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import hashlib,json,sys\n"
        "from pathlib import Path\n"
        f"log=Path({str(log)!r})\n"
        "with log.open('a') as stream: stream.write(json.dumps(sys.argv[1:])+\"\\n\")\n"
        "if sys.argv[1] == 'push':\n"
        "  manifest=Path(sys.argv[sys.argv.index('--export-manifest')+1])\n"
        "  raw=b'{\"schemaVersion\":2}'\n"
        "  manifest.write_bytes(raw)\n"
        "  repository=sys.argv[-2].split(':',1)[0]\n"
        "  digest=hashlib.sha256(raw).hexdigest()\n"
        "  print(json.dumps({'reference':repository+'@sha256:'+digest}))\n"
    )
    script.chmod(0o755)
    return script, log


def _run(
    payloads: Path, output: Path, oras: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            SCRIPT,
            "--payload-root",
            payloads,
            "--output-root",
            output,
            "--version",
            "0.1.0",
            "--commit",
            COMMIT,
            "--source-sha256",
            SOURCE_SHA256,
            "--created",
            "2026-08-06T12:00:00+00:00",
            "--builder-id",
            "https://github.com/example/repo/actions/runs/123",
            "--source-url",
            "https://github.com/example/repo",
            "--agent-repository",
            "ghcr.io/example/dgx-forge-agent",
            "--supervisor-repository",
            "ghcr.io/example/dgx-forge-agent-supervisor",
            "--tooling-repository",
            "ghcr.io/example/dgx-forge-tooling",
        ],
        cwd=ROOT,
        env={**os.environ, "DGX_PLATFORM_ORAS_BIN": str(oras)},
        capture_output=True,
        text=True,
        check=False,
    )


def test_publisher_pushes_attaches_and_binds_all_exact_payloads(tmp_path: Path) -> None:
    payloads = _payloads(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    oras, log = _fake_oras(tmp_path)

    result = _run(payloads, output, oras)

    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["schema_version"] == 1
    assert set(receipt["artifacts"]) == {"agent", "supervisor", "tooling"}
    manifest_digest = hashlib.sha256(b'{"schemaVersion":2}').hexdigest()
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert [call[0] for call in calls] == [
        "push",
        "attach",
        "attach",
        "push",
        "attach",
        "attach",
        "push",
        "attach",
        "attach",
    ]
    for key, repository, filename, locator in (
        ("agent", "ghcr.io/example/dgx-forge-agent", "dgx-agent", "agents.linux-arm64"),
        (
            "supervisor",
            "ghcr.io/example/dgx-forge-agent-supervisor",
            "dgx-agent-supervisor",
            "supervisors.linux-arm64",
        ),
        (
            "tooling",
            "ghcr.io/example/dgx-forge-tooling",
            "dgx-forge-tooling",
            "tooling.linux-arm64",
        ),
    ):
        evidence_raw = (output / f"{key}-evidence.json").read_bytes()
        evidence = json.loads(evidence_raw)
        assert evidence_raw == _canonical(evidence)
        assert evidence["locator"] == locator
        assert evidence["artifact"]["reference"] == (
            f"{repository}@sha256:{manifest_digest}"
        )
        assert receipt["artifacts"][key] == evidence["artifact"]["reference"]
        sbom = json.loads((output / f"{key}-sbom.json").read_bytes())
        provenance = json.loads((output / f"{key}-provenance.json").read_bytes())
        payload_sha256 = hashlib.sha256((payloads / filename).read_bytes()).hexdigest()
        assert sbom["files"][0]["checksums"][0]["checksumValue"] == payload_sha256
        assert provenance["subject"] == [
            {"digest": {"sha256": payload_sha256}, "name": filename}
        ]
        assert provenance["predicate"]["buildDefinition"]["internalParameters"] == {
            "source_archive_sha256": SOURCE_SHA256
        }


def test_publisher_rejects_payload_mismatch_before_registry_mutation(
    tmp_path: Path,
) -> None:
    payloads = _payloads(tmp_path)
    (payloads / "dgx-agent").write_bytes(b"substituted")
    output = tmp_path / "output"
    output.mkdir()
    oras, log = _fake_oras(tmp_path)

    result = _run(payloads, output, oras)

    assert result.returncode == 2
    assert not log.exists()
    assert list(output.iterdir()) == []
