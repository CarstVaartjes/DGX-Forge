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


def test_encrypted_backup_includes_hermes_state_but_not_cache(tmp_path: Path) -> None:
    dump = tmp_path / "database.dump"
    dump.write_bytes(b"dump")
    hermes = tmp_path / "hermes"
    (hermes / "data/sessions").mkdir(parents=True)
    (hermes / "data/.env").write_text("LOCAL_ONLY=1\n")
    (hermes / "data/sessions/session.json").write_text("{}\n")
    (hermes / "workspaces/repository").mkdir(parents=True)
    (hermes / "workspaces/repository/README.md").write_text("workspace\n")
    (hermes / "cache").mkdir()
    (hermes / "cache/sentinel").write_text("discard me\n")
    output = tmp_path / "backup.enc"
    transform = _transform(tmp_path)

    create_backup(
        dump,
        [hermes / "data", hermes / "workspaces"],
        output,
        encrypt_command=[str(transform)],
    )
    files = inspect_backup(output, decrypt_command=[str(transform)])["files"]
    assert {
        "data/.env",
        "data/sessions/session.json",
        "workspaces/repository/README.md",
    } <= set(files)
    assert not any(name.startswith("cache/") for name in files)


def test_host_backup_and_restore_scripts_handle_hermes_fail_closed() -> None:
    root = Path(__file__).resolve().parents[3]
    backup = (root / "deploy/compose/bin/backup-control-plane").read_text()
    restore = (root / "deploy/compose/bin/restore-control-plane").read_text()

    assert '--config "$hermes_data_root/data"' in backup
    assert '--config "$hermes_data_root/workspaces"' in backup
    assert '--config "$hermes_data_root/cache"' not in backup
    assert "stop control-api control-worker hermes-agent" in restore
    assert 'local source="$temporary/verified/$name"' in restore
    assert "restore_hermes_tree data" in restore
    assert "restore_hermes_tree workspaces" in restore
    assert "HERMES_UID" in restore and "HERMES_GID" in restore
    assert "start hermes-agent" not in restore
