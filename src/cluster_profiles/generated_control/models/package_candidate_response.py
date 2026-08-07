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
  from ..models.package_release_metadata import PackageReleaseMetadata
  from ..models.package_candidate_response_metadata import PackageCandidateResponseMetadata





T = TypeVar("T", bound="PackageCandidateResponse")



@_attrs_define
class PackageCandidateResponse:
    """
        Attributes:
            family_id (str):
            id (str):
            release_key (str):
            state (str):
            upstream_version (str):
            metadata (Union[Unset, PackageCandidateResponseMetadata]):
            reason_code (Union[None, Unset, str]):
            release (Union['PackageReleaseMetadata', None, Unset]):
     """

    family_id: str
    id: str
    release_key: str
    state: str
    upstream_version: str
    metadata: Union[Unset, 'PackageCandidateResponseMetadata'] = UNSET
    reason_code: Union[None, Unset, str] = UNSET
    release: Union['PackageReleaseMetadata', None, Unset] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.package_release_metadata import PackageReleaseMetadata
        from ..models.package_candidate_response_metadata import PackageCandidateResponseMetadata
        family_id = self.family_id

        id = self.id

        release_key = self.release_key

        state = self.state

        upstream_version = self.upstream_version

        metadata: Union[Unset, dict[str, Any]] = UNSET
        if not isinstance(self.metadata, Unset):
            metadata = self.metadata.to_dict()

        reason_code: Union[None, Unset, str]
        if isinstance(self.reason_code, Unset):
            reason_code = UNSET
        else:
            reason_code = self.reason_code

        release: Union[None, Unset, dict[str, Any]]
        if isinstance(self.release, Unset):
            release = UNSET
        elif isinstance(self.release, PackageReleaseMetadata):
            release = self.release.to_dict()
        else:
            release = self.release


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "family_id": family_id,
            "id": id,
            "release_key": release_key,
            "state": state,
            "upstream_version": upstream_version,
        })
        if metadata is not UNSET:
            field_dict["metadata"] = metadata
        if reason_code is not UNSET:
            field_dict["reason_code"] = reason_code
        if release is not UNSET:
            field_dict["release"] = release

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.package_release_metadata import PackageReleaseMetadata
        from ..models.package_candidate_response_metadata import PackageCandidateResponseMetadata
        d = dict(src_dict)
        family_id = d.pop("family_id")

        id = d.pop("id")

        release_key = d.pop("release_key")

        state = d.pop("state")

        upstream_version = d.pop("upstream_version")

        _metadata = d.pop("metadata", UNSET)
        metadata: Union[Unset, PackageCandidateResponseMetadata]
        if isinstance(_metadata,  Unset):
            metadata = UNSET
        else:
            metadata = PackageCandidateResponseMetadata.from_dict(_metadata)




        def _parse_reason_code(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        reason_code = _parse_reason_code(d.pop("reason_code", UNSET))


        def _parse_release(data: object) -> Union['PackageReleaseMetadata', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                release_type_0 = PackageReleaseMetadata.from_dict(data)



                return release_type_0
            except: # noqa: E722
                pass
            return cast(Union['PackageReleaseMetadata', None, Unset], data)

        release = _parse_release(d.pop("release", UNSET))


        package_candidate_response = cls(
            family_id=family_id,
            id=id,
            release_key=release_key,
            state=state,
            upstream_version=upstream_version,
            metadata=metadata,
            reason_code=reason_code,
            release=release,
        )

        return package_candidate_response
