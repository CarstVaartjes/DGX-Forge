"""PyInstaller entry point for the complete DGX Forge agent slot closure."""

from __future__ import annotations

import sys
from importlib import resources


def _module_smoke() -> int:
    from dgx_agent import (  # noqa: F401
        client,
        config,
        deadlines,
        main,
        nvidia_tools,
        oci,
        operations,
        probe,
        readiness,
        releases,
        runtime_policy,
        state,
        update_trust,
        workloads,
    )

    from spark_profiles import platform_release
    from spark_profiles import update_trust as platform_update_trust

    if not platform_release.PlatformRelease or not platform_update_trust.UpdateTrust:
        raise RuntimeError("packaged platform release trust is unavailable")

    schema = resources.files("spark_profiles").joinpath(
        "schemas", "platform-update-manifest.schema.json"
    )
    with schema.open("rb") as stream:
        if not stream.read(1):
            raise RuntimeError("packaged platform release schema is empty")

    print("packaged-agent-modules-ok")
    return 0


def entry() -> int:
    if sys.argv[1:] == ["--packaged-module-smoke"]:
        return _module_smoke()
    from dgx_agent.main import main

    return main()


if __name__ == "__main__":
    raise SystemExit(entry())
