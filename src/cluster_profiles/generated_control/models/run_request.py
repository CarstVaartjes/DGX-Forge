from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="RunRequest")



@_attrs_define
class RunRequest:
    """
        Attributes:
            alias (str):
            installation_id (str):
            plan_digest (str):
            request_key (str):
     """

    alias: str
    installation_id: str
    plan_digest: str
    request_key: str





    def to_dict(self) -> dict[str, Any]:
        alias = self.alias

        installation_id = self.installation_id

        plan_digest = self.plan_digest

        request_key = self.request_key


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "alias": alias,
            "installation_id": installation_id,
            "plan_digest": plan_digest,
            "request_key": request_key,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        alias = d.pop("alias")

        installation_id = d.pop("installation_id")

        plan_digest = d.pop("plan_digest")

        request_key = d.pop("request_key")

        run_request = cls(
            alias=alias,
            installation_id=installation_id,
            plan_digest=plan_digest,
            request_key=request_key,
        )

        return run_request
