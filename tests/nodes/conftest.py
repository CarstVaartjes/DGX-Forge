from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def pytest_addoption(parser):
    parser.addoption(
        "--inventory-dir",
        action="store",
        default=None,
        help="validate captured inventory JSON files in this directory",
    )


@pytest.fixture
def inventory_dir(request):
    configured = request.config.getoption("--inventory-dir")
    if configured is None:
        return None

    path = Path(configured)
    return path if path.is_absolute() else ROOT / path
