from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.enrollment_grant_response_purpose import check_enrollment_grant_response_purpose
from ..models.enrollment_grant_response_purpose import EnrollmentGrantResponsePurpose
from typing import cast






T = TypeVar("T", bound="EnrollmentGrantResponse")



@_attrs_define
class EnrollmentGrantResponse:
    """
        Attributes:
            expires_at (str):
            id (str):
            node_id (str):
            purpose (EnrollmentGrantResponsePurpose):
            token (str):
     """

    expires_at: str
    id: str
    node_id: str
    purpose: EnrollmentGrantResponsePurpose
    token: str





    def to_dict(self) -> dict[str, Any]:
        expires_at = self.expires_at

        id = self.id

        node_id = self.node_id

        purpose: str = self.purpose

        token = self.token


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "expires_at": expires_at,
            "id": id,
            "node_id": node_id,
            "purpose": purpose,
            "token": token,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        expires_at = d.pop("expires_at")

        id = d.pop("id")

        node_id = d.pop("node_id")

        purpose = check_enrollment_grant_response_purpose(d.pop("purpose"))




        token = d.pop("token")

        enrollment_grant_response = cls(
            expires_at=expires_at,
            id=id,
            node_id=node_id,
            purpose=purpose,
            token=token,
        )

        return enrollment_grant_response
