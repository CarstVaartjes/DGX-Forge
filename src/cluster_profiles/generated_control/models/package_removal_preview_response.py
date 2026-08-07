from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Union

if TYPE_CHECKING:
  from ..models.package_removal_node import PackageRemovalNode





T = TypeVar("T", bound="PackageRemovalPreviewResponse")



@_attrs_define
class PackageRemovalPreviewResponse:
    """
        Attributes:
            deployment_id (str):
            digest (str):
            nodes (list['PackageRemovalNode']):
            reclaimable_bytes (int):
            release_digest (str):
            state (str):
            blocked_nodes (Union[Unset, list[str]]):
     """

    deployment_id: str
    digest: str
    nodes: list['PackageRemovalNode']
    reclaimable_bytes: int
    release_digest: str
    state: str
    blocked_nodes: Union[Unset, list[str]] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.package_removal_node import PackageRemovalNode
        deployment_id = self.deployment_id

        digest = self.digest

        nodes = []
        for nodes_item_data in self.nodes:
            nodes_item = nodes_item_data.to_dict()
            nodes.append(nodes_item)



        reclaimable_bytes = self.reclaimable_bytes

        release_digest = self.release_digest

        state = self.state

        blocked_nodes: Union[Unset, list[str]] = UNSET
        if not isinstance(self.blocked_nodes, Unset):
            blocked_nodes = self.blocked_nodes




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "deployment_id": deployment_id,
            "digest": digest,
            "nodes": nodes,
            "reclaimable_bytes": reclaimable_bytes,
            "release_digest": release_digest,
            "state": state,
        })
        if blocked_nodes is not UNSET:
            field_dict["blocked_nodes"] = blocked_nodes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.package_removal_node import PackageRemovalNode
        d = dict(src_dict)
        deployment_id = d.pop("deployment_id")

        digest = d.pop("digest")

        nodes = []
        _nodes = d.pop("nodes")
        for nodes_item_data in (_nodes):
            nodes_item = PackageRemovalNode.from_dict(nodes_item_data)



            nodes.append(nodes_item)


        reclaimable_bytes = d.pop("reclaimable_bytes")

        release_digest = d.pop("release_digest")

        state = d.pop("state")

        blocked_nodes = cast(list[str], d.pop("blocked_nodes", UNSET))


        package_removal_preview_response = cls(
            deployment_id=deployment_id,
            digest=digest,
            nodes=nodes,
            reclaimable_bytes=reclaimable_bytes,
            release_digest=release_digest,
            state=state,
            blocked_nodes=blocked_nodes,
        )

        return package_removal_preview_response
