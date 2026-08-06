from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="DeploymentResponse")



@_attrs_define
class DeploymentResponse:
    """
        Attributes:
            id (str):
            release_digest (str):
            state (str):
            family_id (Union[None, Unset, str]):
            previous_release_digest (Union[None, Unset, str]):
     """

    id: str
    release_digest: str
    state: str
    family_id: Union[None, Unset, str] = UNSET
    previous_release_digest: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        id = self.id

        release_digest = self.release_digest

        state = self.state

        family_id: Union[None, Unset, str]
        if isinstance(self.family_id, Unset):
            family_id = UNSET
        else:
            family_id = self.family_id

        previous_release_digest: Union[None, Unset, str]
        if isinstance(self.previous_release_digest, Unset):
            previous_release_digest = UNSET
        else:
            previous_release_digest = self.previous_release_digest


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "id": id,
            "release_digest": release_digest,
            "state": state,
        })
        if family_id is not UNSET:
            field_dict["family_id"] = family_id
        if previous_release_digest is not UNSET:
            field_dict["previous_release_digest"] = previous_release_digest

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        release_digest = d.pop("release_digest")

        state = d.pop("state")

        def _parse_family_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        family_id = _parse_family_id(d.pop("family_id", UNSET))


        def _parse_previous_release_digest(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        previous_release_digest = _parse_previous_release_digest(d.pop("previous_release_digest", UNSET))


        deployment_response = cls(
            id=id,
            release_digest=release_digest,
            state=state,
            family_id=family_id,
            previous_release_digest=previous_release_digest,
        )

        return deployment_response
