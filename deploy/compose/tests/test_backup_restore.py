import json
from pathlib import Path

import pytest

from dgx_control.offline import BackupError, create_backup, inspect_backup


def _transform(tmp_path: Path) -> Path:
    script = tmp_path / "transform"
    script.write_text("#!/usr/bin/env python3\nimport sys\nsys.stdout.buffer.write(sys.stdin.buffer.read()[::-1])\n")
    script.chmod(0o700)
    return script


def test_encrypted_backup_contains_verified_database_and_config(tmp_path: Path) -> None:
    dump = tmp_path / "database.dump"
    dump.write_bytes(b"postgres-custom-dump")
    config = tmp_path / "config"
    config.mkdir()
    (config / "Caddyfile").write_text("admin off\n")
    output = tmp_path / "backup.enc"
    transform = _transform(tmp_path)

    create_backup(dump, [config], output, encrypt_command=[str(transform)])
    assert not output.read_bytes().startswith(b"postgres-custom-dump")
    manifest = inspect_backup(output, decrypt_command=[str(transform)])
    assert manifest["format"] == "dgx-control-backup-v1"
    assert set(manifest["files"]) == {"database.dump", "config/Caddyfile"}


def test_tampered_backup_is_rejected_before_restore(tmp_path: Path) -> None:
    dump = tmp_path / "database.dump"
    dump.write_bytes(b"dump")
    output = tmp_path / "backup.enc"
    transform = _transform(tmp_path)
    create_backup(dump, [], output, encrypt_command=[str(transform)])
    content = bytearray(output.read_bytes())
    content[len(content) // 2] ^= 1
    output.write_bytes(content)
    with pytest.raises(BackupError):
        inspect_backup(output, decrypt_command=[str(transform)])
