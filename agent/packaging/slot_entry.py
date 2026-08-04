"""PyInstaller entry point for the complete DGX Forge agent slot closure."""

from __future__ import annotations

import sys


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

    print("packaged-agent-modules-ok")
    return 0


def entry() -> int:
    if sys.argv[1:] == ["--packaged-module-smoke"]:
        return _module_smoke()
    from dgx_agent.main import main

    return main()


if __name__ == "__main__":
    raise SystemExit(entry())
