"""PyInstaller entry point for the complete DGX Forge agent slot closure."""

from __future__ import annotations

import sys
from importlib import resources


def _module_smoke() -> int:
    from importlib import import_module

    for name in (
        "dgx_agent.client",
        "dgx_agent.config",
        "dgx_agent.deadlines",
        "dgx_agent.main",
        "dgx_agent.nvidia_tools",
        "dgx_agent.oci",
        "dgx_agent.operations",
        "dgx_agent.package_helper",
        "dgx_agent.package_helper_protocol",
        "dgx_agent.probe",
        "dgx_agent.readiness",
        "dgx_agent.releases",
        "dgx_agent.runtime_policy",
        "dgx_agent.state",
        "dgx_agent.update_trust",
        "dgx_agent.workloads",
        "spark_profiles.platform_release",
        "spark_profiles.update_trust",
    ):
        import_module(name)

    platform_release = import_module("spark_profiles.platform_release")
    platform_update_trust = import_module("spark_profiles.update_trust")

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
    from importlib import import_module

    if sys.argv[1:] == ["--packaged-module-smoke"]:
        return _module_smoke()
    if sys.argv[1:2] == ["--package-helper"]:
        # Keep the helper out of the normal module-smoke import graph; the
        # packaging builder adds it as an explicit hidden import below.
        package_helper_main = import_module("dgx_agent.package_helper").main

        return package_helper_main(sys.argv[2:])
    main = import_module("dgx_agent.main").main

    return main()


if __name__ == "__main__":
    raise SystemExit(entry())
