from __future__ import annotations

from dgx_control.package_compatibility import CompatibilityEvaluator

LOCK = {
    "digest": "a" * 64,
    "family_id": "future-stack",
    "compatibility": {
        "architectures": ["linux-arm64"],
        "operating_systems": ["ubuntu-24.04"],
        "required_capabilities": ["package-abi-v1"],
        "minimum_storage_bytes": 100,
        "minimum_memory_bytes": 200,
        "minimum_cuda": "12.4",
        "minimum_driver": "550.54",
        "backends": ["oci"],
    },
    "adapter_abi": 1,
}


def test_evaluator_returns_only_authenticated_compatible_nodes() -> None:
    fleet = {
        "spk_" + "1" * 32: {
            "architecture": "linux-arm64",
            "operating_system": "ubuntu-24.04",
            "memory_bytes": 400,
            "storage_available_bytes": 500,
            "cuda": "12.6",
            "driver": "550.76",
            "capabilities": ["package-abi-v1"],
            "backends": ["oci"],
            "adapter_abis": [1],
            "authenticated": True,
            "online": True,
            "healthy": True,
        },
        "spk_" + "2" * 32: {
            "architecture": "linux-x86_64",
            "operating_system": "ubuntu-24.04",
            "authenticated": True,
            "online": True,
            "healthy": True,
        },
    }

    report = CompatibilityEvaluator().evaluate(LOCK, fleet)

    assert report.release_digest == "a" * 64
    assert report.compatible_node_ids == ("spk_" + "1" * 32,)
    assert "architecture-incompatible" in report.incompatible["spk_" + "2" * 32]
    assert report.required_platform_capabilities == ("package-abi-v1",)
    assert len(report.digest) == 64


def test_evaluator_rejects_missing_fleet_and_reports_all_reasons() -> None:
    report = CompatibilityEvaluator().evaluate(
        LOCK,
        {
            "spk_" + "3" * 32: {
                "architecture": "linux-arm64",
                "operating_system": "ubuntu-24.04",
                "memory_bytes": 1,
                "storage_available_bytes": 1,
                "capabilities": [],
                "authenticated": False,
                "online": False,
            }
        },
    )
    reasons = report.incompatible["spk_" + "3" * 32]
    assert set(reasons) >= {
        "authentication-missing",
        "offline",
        "memory-insufficient",
        "storage-insufficient",
        "capability-missing",
    }
    assert report.compatible_node_ids == ()


def test_evaluator_derives_backend_and_abi_from_authenticated_agent_capabilities() -> None:
    lock = {
        **LOCK,
        "compatibility": {
            **LOCK["compatibility"],
            "required_capabilities": [
                "package-abi-v1",
                "package-backend-native-v1",
            ],
            "backends": ["native"],
        },
    }
    fleet = {
        "spk_" + "4" * 32: {
            "architecture": "linux-arm64",
            "operating_system": "ubuntu-24.04",
            "memory_bytes": 400,
            "storage_available_bytes": 500,
            "cuda": "12.6",
            "driver": "550.76",
            "capabilities": ["package-abi-v1", "package-backend-native-v1"],
            "authenticated": True,
            "online": True,
            "healthy": True,
        }
    }

    report = CompatibilityEvaluator().evaluate(lock, fleet)

    assert report.compatible_node_ids == ("spk_" + "4" * 32,)
