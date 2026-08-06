from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.deployment_response import DeploymentResponse





T = TypeVar("T", bound="DeploymentsResponse")



@_attrs_define
class DeploymentsResponse:
    """
        Attributes:
            deployments (list['DeploymentResponse']):
            total (int):
            next_cursor (Union[None, Unset, str]):
     """

    deployments: list['DeploymentResponse']
    total: int
    next_cursor: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.deployment_response import DeploymentResponse
        deployments = []
        for deployments_item_data in self.deployments:
            deployments_item = deployments_item_data.to_dict()
            deployments.append(deployments_item)



        total = self.total

        next_cursor: Union[None, Unset, str]
        if isinstance(self.next_cursor, Unset):
            next_cursor = UNSET
        else:
            next_cursor = self.next_cursor


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "deployments": deployments,
            "total": total,
        })
        if next_cursor is not UNSET:
            field_dict["next_cursor"] = next_cursor

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.deployment_response import DeploymentResponse
        d = dict(src_dict)
        deployments = []
        _deployments = d.pop("deployments")
        for deployments_item_data in (_deployments):
            deployments_item = DeploymentResponse.from_dict(deployments_item_data)



            deployments.append(deployments_item)


        total = d.pop("total")

        def _parse_next_cursor(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        next_cursor = _parse_next_cursor(d.pop("next_cursor", UNSET))


        deployments_response = cls(
            deployments=deployments,
            total=total,
            next_cursor=next_cursor,
        )

        return deployments_response
