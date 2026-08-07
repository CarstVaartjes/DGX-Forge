"""Git-authored workload package family and deployment contracts."""

from .contracts import (
    PackageFamily,
    PromotionPolicy,
    ReleaseIndexEntry,
    WorkloadDeployment,
    WorkloadPackageError,
    validate_deployment,
)
from .legacy import LegacyWorkloadReader

__all__ = [
    "LegacyWorkloadReader",
    "PackageFamily",
    "PromotionPolicy",
    "ReleaseIndexEntry",
    "WorkloadDeployment",
    "WorkloadPackageError",
    "validate_deployment",
]
