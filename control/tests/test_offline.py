from pathlib import Path

import pytest
from dgx_control.offline import OfflineConflict, OfflineLock, require_offline


def test_offline_mutation_refuses_healthy_api(tmp_path: Path) -> None:
    with pytest.raises(OfflineConflict, match="control plane is running"):
        require_offline(tmp_path, probe=lambda: True)


def test_offline_lock_is_exclusive(tmp_path: Path) -> None:
    first = OfflineLock(tmp_path / "offline.lock")
    second = OfflineLock(tmp_path / "offline.lock")
    with (
        first,
        pytest.raises(OfflineConflict, match="maintenance operation"),
        second,
    ):
        pass
