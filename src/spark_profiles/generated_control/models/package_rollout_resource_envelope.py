from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.package_resource_values import PackageResourceValues
  from ..models.package_rollout_resource_envelope_evidence_item import PackageRolloutResourceEnvelopeEvidenceItem





T = TypeVar("T", bound="PackageRolloutResourceEnvelope")



@_attrs_define
class PackageRolloutResourceEnvelope:
    """ Signed release sizing for one-node and aggregate placement views.

        Attributes:
            aggregate (PackageResourceValues):
            evidence (list['PackageRolloutResourceEnvelopeEvidenceItem']):
            measurement (str):
            per_node (PackageResourceValues):
            required_sparks (int):
            schema_version (int):
            topology (str):
     """

    aggregate: 'PackageResourceValues'
    evidence: list['PackageRolloutResourceEnvelopeEvidenceItem']
    measurement: str
    per_node: 'PackageResourceValues'
    required_sparks: int
    schema_version: int
    topology: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.package_resource_values import PackageResourceValues
        from ..models.package_rollout_resource_envelope_evidence_item import PackageRolloutResourceEnvelopeEvidenceItem
        aggregate = self.aggregate.to_dict()

        evidence = []
        for evidence_item_data in self.evidence:
            evidence_item = evidence_item_data.to_dict()
            evidence.append(evidence_item)



        measurement = self.measurement

        per_node = self.per_node.to_dict()

        required_sparks = self.required_sparks

        schema_version = self.schema_version

        topology = self.topology


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "aggregate": aggregate,
            "evidence": evidence,
            "measurement": measurement,
            "per_node": per_node,
            "required_sparks": required_sparks,
            "schema_version": schema_version,
            "topology": topology,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.package_resource_values import PackageResourceValues
        from ..models.package_rollout_resource_envelope_evidence_item import PackageRolloutResourceEnvelopeEvidenceItem
        d = dict(src_dict)
        aggregate = PackageResourceValues.from_dict(d.pop("aggregate"))




        evidence = []
        _evidence = d.pop("evidence")
        for evidence_item_data in (_evidence):
            evidence_item = PackageRolloutResourceEnvelopeEvidenceItem.from_dict(evidence_item_data)



            evidence.append(evidence_item)


        measurement = d.pop("measurement")

        per_node = PackageResourceValues.from_dict(d.pop("per_node"))




        required_sparks = d.pop("required_sparks")

        schema_version = d.pop("schema_version")

        topology = d.pop("topology")

        package_rollout_resource_envelope = cls(
            aggregate=aggregate,
            evidence=evidence,
            measurement=measurement,
            per_node=per_node,
            required_sparks=required_sparks,
            schema_version=schema_version,
            topology=topology,
        )

        return package_rollout_resource_envelope
