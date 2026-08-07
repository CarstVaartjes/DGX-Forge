from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.plan_endpoint_scheme import check_plan_endpoint_scheme
from ..models.plan_endpoint_scheme import PlanEndpointScheme
from typing import cast






T = TypeVar("T", bound="PlanEndpoint")



@_attrs_define
class PlanEndpoint:
    """
        Attributes:
            path (str):
            port (int):
            scheme (PlanEndpointScheme):
     """

    path: str
    port: int
    scheme: PlanEndpointScheme





    def to_dict(self) -> dict[str, Any]:
        path = self.path

        port = self.port

        scheme: str = self.scheme


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "path": path,
            "port": port,
            "scheme": scheme,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        path = d.pop("path")

        port = d.pop("port")

        scheme = check_plan_endpoint_scheme(d.pop("scheme"))




        plan_endpoint = cls(
            path=path,
            port=port,
            scheme=scheme,
        )

        return plan_endpoint
