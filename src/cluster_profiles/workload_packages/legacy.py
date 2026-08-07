"""Read-only projection from legacy workload definitions to package deployments."""

from __future__ import annotations

from ..contracts import WorkloadDefinition
from .contracts import WorkloadDeployment, WorkloadPackageError


class LegacyWorkloadReader:
    """Validate that a generic deployment preserves a legacy public workload."""

    @staticmethod
    def read(
        old_definition: WorkloadDefinition,
        deployment: WorkloadDeployment,
    ) -> WorkloadDeployment:
        """Return *deployment* when it is an exact read-only legacy projection.

        The routing alias is the compatibility join key.  It keeps the old public
        workload identifier out of the package engine while allowing package
        family, release, and deployment IDs to evolve independently.
        """
        if not isinstance(old_definition, WorkloadDefinition):
            raise TypeError("legacy workload definition is invalid")
        if not isinstance(deployment, WorkloadDeployment):
            raise TypeError("workload deployment is invalid")
        if deployment.routing["alias"] != old_definition.id:
            raise WorkloadPackageError(
                "legacy deployment alias does not match workload"
            )
        if deployment.selector["node_count"] != len(old_definition.nodes):
            raise WorkloadPackageError(
                "legacy deployment node count does not match workload"
            )
        if deployment.ports[deployment.routing["port"]] != old_definition.endpoint.port:
            raise WorkloadPackageError("legacy deployment port does not match workload")
        if (
            deployment.resources["memory_bytes"]
            != old_definition.resources.minimum_free_memory_bytes
        ):
            raise WorkloadPackageError(
                "legacy deployment memory does not match workload"
            )
        if (
            deployment.resources["storage_bytes"]
            != old_definition.resources.minimum_free_disk_bytes
        ):
            raise WorkloadPackageError(
                "legacy deployment storage does not match workload"
            )
        return deployment
