from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="InstallRequest")



@_attrs_define
class InstallRequest:
    """
        Attributes:
            node_ids (list[str]):
            plan_digest (str):
            recipe_revision_id (str):
            request_key (str):
     """

    node_ids: list[str]
    plan_digest: str
    recipe_revision_id: str
    request_key: str





    def to_dict(self) -> dict[str, Any]:
        node_ids = self.node_ids



        plan_digest = self.plan_digest

        recipe_revision_id = self.recipe_revision_id

        request_key = self.request_key


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "node_ids": node_ids,
            "plan_digest": plan_digest,
            "recipe_revision_id": recipe_revision_id,
            "request_key": request_key,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        node_ids = cast(list[str], d.pop("node_ids"))


        plan_digest = d.pop("plan_digest")

        recipe_revision_id = d.pop("recipe_revision_id")

        request_key = d.pop("request_key")

        install_request = cls(
            node_ids=node_ids,
            plan_digest=plan_digest,
            recipe_revision_id=recipe_revision_id,
            request_key=request_key,
        )

        return install_request
