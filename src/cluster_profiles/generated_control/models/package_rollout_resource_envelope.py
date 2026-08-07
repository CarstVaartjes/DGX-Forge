from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.package_resource_values import PackageResourceValues
  from ..models.package_rollout_resource_envelope_evidence_item import PackageRolloutResourceEnvelopeEvidenceItem
  from ..models.package_rank import PackageRank
  from ..models.package_fabric import PackageFabric





T = TypeVar("T", bound="PackageRolloutResourceEnvelope")



@_attrs_define
class PackageRolloutResourceEnvelope:
    """ Signed release sizing for one-node and aggregate placement views.

        Attributes:
            aggregate (PackageResourceValues):
            evidence (list['PackageRolloutResourceEnvelopeEvidenceItem']):
            fabric (PackageFabric):
            measurement (str):
            per_node (PackageResourceValues):
            ranks (list['PackageRank']):
            required_sparks (int):
            schema_version (int):
            topology (str):
            world_size (int):
     """

    aggregate: 'PackageResourceValues'
    evidence: list['PackageRolloutResourceEnvelopeEvidenceItem']
    fabric: 'PackageFabric'
    measurement: str
    per_node: 'PackageResourceValues'
    ranks: list['PackageRank']
    required_sparks: int
    schema_version: int
    topology: str
    world_size: int





    def to_dict(self) -> dict[str, Any]:
        from ..models.package_resource_values import PackageResourceValues
        from ..models.package_rollout_resource_envelope_evidence_item import PackageRolloutResourceEnvelopeEvidenceItem
        from ..models.package_rank import PackageRank
        from ..models.package_fabric import PackageFabric
        aggregate = self.aggregate.to_dict()

        evidence = []
        for evidence_item_data in self.evidence:
            evidence_item = evidence_item_data.to_dict()
            evidence.append(evidence_item)



        fabric = self.fabric.to_dict()

        measurement = self.measurement

        per_node = self.per_node.to_dict()

        ranks = []
        for ranks_item_data in self.ranks:
            ranks_item = ranks_item_data.to_dict()
            ranks.append(ranks_item)



        required_sparks = self.required_sparks

        schema_version = self.schema_version

        topology = self.topology

        world_size = self.world_size


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "aggregate": aggregate,
            "evidence": evidence,
            "fabric": fabric,
            "measurement": measurement,
            "per_node": per_node,
            "ranks": ranks,
            "required_sparks": required_sparks,
            "schema_version": schema_version,
            "topology": topology,
            "world_size": world_size,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.package_resource_values import PackageResourceValues
        from ..models.package_rollout_resource_envelope_evidence_item import PackageRolloutResourceEnvelopeEvidenceItem
        from ..models.package_rank import PackageRank
        from ..models.package_fabric import PackageFabric
        d = dict(src_dict)
        aggregate = PackageResourceValues.from_dict(d.pop("aggregate"))




        evidence = []
        _evidence = d.pop("evidence")
        for evidence_item_data in (_evidence):
            evidence_item = PackageRolloutResourceEnvelopeEvidenceItem.from_dict(evidence_item_data)



            evidence.append(evidence_item)


        fabric = PackageFabric.from_dict(d.pop("fabric"))




        measurement = d.pop("measurement")

        per_node = PackageResourceValues.from_dict(d.pop("per_node"))




        ranks = []
        _ranks = d.pop("ranks")
        for ranks_item_data in (_ranks):
            ranks_item = PackageRank.from_dict(ranks_item_data)



            ranks.append(ranks_item)


        required_sparks = d.pop("required_sparks")

        schema_version = d.pop("schema_version")

        topology = d.pop("topology")

        world_size = d.pop("world_size")

        package_rollout_resource_envelope = cls(
            aggregate=aggregate,
            evidence=evidence,
            fabric=fabric,
            measurement=measurement,
            per_node=per_node,
            ranks=ranks,
            required_sparks=required_sparks,
            schema_version=schema_version,
            topology=topology,
            world_size=world_size,
        )

        return package_rollout_resource_envelope
