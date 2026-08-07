"""PyInstaller entry point for the complete DGX Forge agent slot closure."""

from __future__ import annotations

import sys
from importlib import resources


def _module_smoke() -> int:
    from importlib import import_module

    for name in (
        "vonk_agent.client",
        "vonk_agent.config",
        "vonk_agent.deadlines",
        "vonk_agent.main",
        "vonk_agent.nvidia_tools",
        "vonk_agent.oci",
        "vonk_agent.operations",
        "vonk_agent.package_helper",
        "vonk_agent.package_helper_protocol",
        "vonk_agent.probe",
        "vonk_agent.readiness",
        "vonk_agent.releases",
        "vonk_agent.runtime_policy",
        "vonk_agent.state",
        "vonk_agent.update_trust",
        "vonk_agent.workloads",
        "cluster_profiles.platform_release",
        "cluster_profiles.update_trust",
    ):
        import_module(name)

    platform_release = import_module("cluster_profiles.platform_release")
    platform_update_trust = import_module("cluster_profiles.update_trust")

    if not platform_release.PlatformRelease or not platform_update_trust.UpdateTrust:
        raise RuntimeError("packaged platform release trust is unavailable")

    schema = resources.files("cluster_profiles").joinpath(
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
        package_helper_main = import_module("vonk_agent.package_helper").main

        return package_helper_main(sys.argv[2:])
    main = import_module("vonk_agent.main").main

    return main()


if __name__ == "__main__":
    raise SystemExit(entry())
