from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="PackagePlanResponse")



@_attrs_define
class PackagePlanResponse:
    """
        Attributes:
            digest (str):
            state (str):
            batches (Union[Unset, list[list[str]]]):
            canary_node (Union[None, Unset, str]):
            candidate_id (Union[None, Unset, str]):
            deployment_id (Union[None, Unset, str]):
            download_bytes (Union[None, Unset, int]):
            offline_pending (Union[Unset, list[str]]):
            reclaim_bytes (Union[None, Unset, int]):
            release_digest (Union[None, Unset, str]):
            storage_bytes (Union[None, Unset, int]):
     """

    digest: str
    state: str
    batches: Union[Unset, list[list[str]]] = UNSET
    canary_node: Union[None, Unset, str] = UNSET
    candidate_id: Union[None, Unset, str] = UNSET
    deployment_id: Union[None, Unset, str] = UNSET
    download_bytes: Union[None, Unset, int] = UNSET
    offline_pending: Union[Unset, list[str]] = UNSET
    reclaim_bytes: Union[None, Unset, int] = UNSET
    release_digest: Union[None, Unset, str] = UNSET
    storage_bytes: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        digest = self.digest

        state = self.state

        batches: Union[Unset, list[list[str]]] = UNSET
        if not isinstance(self.batches, Unset):
            batches = []
            for batches_item_data in self.batches:
                batches_item = batches_item_data


                batches.append(batches_item)



        canary_node: Union[None, Unset, str]
        if isinstance(self.canary_node, Unset):
            canary_node = UNSET
        else:
            canary_node = self.canary_node

        candidate_id: Union[None, Unset, str]
        if isinstance(self.candidate_id, Unset):
            candidate_id = UNSET
        else:
            candidate_id = self.candidate_id

        deployment_id: Union[None, Unset, str]
        if isinstance(self.deployment_id, Unset):
            deployment_id = UNSET
        else:
            deployment_id = self.deployment_id

        download_bytes: Union[None, Unset, int]
        if isinstance(self.download_bytes, Unset):
            download_bytes = UNSET
        else:
            download_bytes = self.download_bytes

        offline_pending: Union[Unset, list[str]] = UNSET
        if not isinstance(self.offline_pending, Unset):
            offline_pending = self.offline_pending



        reclaim_bytes: Union[None, Unset, int]
        if isinstance(self.reclaim_bytes, Unset):
            reclaim_bytes = UNSET
        else:
            reclaim_bytes = self.reclaim_bytes

        release_digest: Union[None, Unset, str]
        if isinstance(self.release_digest, Unset):
            release_digest = UNSET
        else:
            release_digest = self.release_digest

        storage_bytes: Union[None, Unset, int]
        if isinstance(self.storage_bytes, Unset):
            storage_bytes = UNSET
        else:
            storage_bytes = self.storage_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "digest": digest,
            "state": state,
        })
        if batches is not UNSET:
            field_dict["batches"] = batches
        if canary_node is not UNSET:
            field_dict["canary_node"] = canary_node
        if candidate_id is not UNSET:
            field_dict["candidate_id"] = candidate_id
        if deployment_id is not UNSET:
            field_dict["deployment_id"] = deployment_id
        if download_bytes is not UNSET:
            field_dict["download_bytes"] = download_bytes
        if offline_pending is not UNSET:
            field_dict["offline_pending"] = offline_pending
        if reclaim_bytes is not UNSET:
            field_dict["reclaim_bytes"] = reclaim_bytes
        if release_digest is not UNSET:
            field_dict["release_digest"] = release_digest
        if storage_bytes is not UNSET:
            field_dict["storage_bytes"] = storage_bytes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        digest = d.pop("digest")

        state = d.pop("state")

        batches = []
        _batches = d.pop("batches", UNSET)
        for batches_item_data in (_batches or []):
            batches_item = cast(list[str], batches_item_data)

            batches.append(batches_item)


        def _parse_canary_node(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        canary_node = _parse_canary_node(d.pop("canary_node", UNSET))


        def _parse_candidate_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        candidate_id = _parse_candidate_id(d.pop("candidate_id", UNSET))


        def _parse_deployment_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        deployment_id = _parse_deployment_id(d.pop("deployment_id", UNSET))


        def _parse_download_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        download_bytes = _parse_download_bytes(d.pop("download_bytes", UNSET))


        offline_pending = cast(list[str], d.pop("offline_pending", UNSET))


        def _parse_reclaim_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        reclaim_bytes = _parse_reclaim_bytes(d.pop("reclaim_bytes", UNSET))


        def _parse_release_digest(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        release_digest = _parse_release_digest(d.pop("release_digest", UNSET))


        def _parse_storage_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        storage_bytes = _parse_storage_bytes(d.pop("storage_bytes", UNSET))


        package_plan_response = cls(
            digest=digest,
            state=state,
            batches=batches,
            canary_node=canary_node,
            candidate_id=candidate_id,
            deployment_id=deployment_id,
            download_bytes=download_bytes,
            offline_pending=offline_pending,
            reclaim_bytes=reclaim_bytes,
            release_digest=release_digest,
            storage_bytes=storage_bytes,
        )

        return package_plan_response
