from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_summary_response_lifecycle import check_recipe_summary_response_lifecycle
from ..models.recipe_summary_response_lifecycle import RecipeSummaryResponseLifecycle
from ..models.recipe_summary_response_origin import check_recipe_summary_response_origin
from ..models.recipe_summary_response_origin import RecipeSummaryResponseOrigin
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="RecipeSummaryResponse")



@_attrs_define
class RecipeSummaryResponse:
    """
        Attributes:
            artifact_count (int):
            expected_download_bytes (int):
            lifecycle (RecipeSummaryResponseLifecycle):
            maximum_installed_bytes_per_node (int):
            maximum_runtime_memory_bytes_per_node (int):
            origin (RecipeSummaryResponseOrigin):
            profile_node_counts (list[int]):
            recipe_id (str):
            revision_number (int):
            runtime_family (str):
            slug (str):
            source_bundle_sha256 (str):
            title (str):
            content_sha256 (Union[None, Unset, str]):
     """

    artifact_count: int
    expected_download_bytes: int
    lifecycle: RecipeSummaryResponseLifecycle
    maximum_installed_bytes_per_node: int
    maximum_runtime_memory_bytes_per_node: int
    origin: RecipeSummaryResponseOrigin
    profile_node_counts: list[int]
    recipe_id: str
    revision_number: int
    runtime_family: str
    slug: str
    source_bundle_sha256: str
    title: str
    content_sha256: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        artifact_count = self.artifact_count

        expected_download_bytes = self.expected_download_bytes

        lifecycle: str = self.lifecycle

        maximum_installed_bytes_per_node = self.maximum_installed_bytes_per_node

        maximum_runtime_memory_bytes_per_node = self.maximum_runtime_memory_bytes_per_node

        origin: str = self.origin

        profile_node_counts = self.profile_node_counts



        recipe_id = self.recipe_id

        revision_number = self.revision_number

        runtime_family = self.runtime_family

        slug = self.slug

        source_bundle_sha256 = self.source_bundle_sha256

        title = self.title

        content_sha256: Union[None, Unset, str]
        if isinstance(self.content_sha256, Unset):
            content_sha256 = UNSET
        else:
            content_sha256 = self.content_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifact_count": artifact_count,
            "expected_download_bytes": expected_download_bytes,
            "lifecycle": lifecycle,
            "maximum_installed_bytes_per_node": maximum_installed_bytes_per_node,
            "maximum_runtime_memory_bytes_per_node": maximum_runtime_memory_bytes_per_node,
            "origin": origin,
            "profile_node_counts": profile_node_counts,
            "recipe_id": recipe_id,
            "revision_number": revision_number,
            "runtime_family": runtime_family,
            "slug": slug,
            "source_bundle_sha256": source_bundle_sha256,
            "title": title,
        })
        if content_sha256 is not UNSET:
            field_dict["content_sha256"] = content_sha256

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        artifact_count = d.pop("artifact_count")

        expected_download_bytes = d.pop("expected_download_bytes")

        lifecycle = check_recipe_summary_response_lifecycle(d.pop("lifecycle"))




        maximum_installed_bytes_per_node = d.pop("maximum_installed_bytes_per_node")

        maximum_runtime_memory_bytes_per_node = d.pop("maximum_runtime_memory_bytes_per_node")

        origin = check_recipe_summary_response_origin(d.pop("origin"))




        profile_node_counts = cast(list[int], d.pop("profile_node_counts"))


        recipe_id = d.pop("recipe_id")

        revision_number = d.pop("revision_number")

        runtime_family = d.pop("runtime_family")

        slug = d.pop("slug")

        source_bundle_sha256 = d.pop("source_bundle_sha256")

        title = d.pop("title")

        def _parse_content_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        content_sha256 = _parse_content_sha256(d.pop("content_sha256", UNSET))


        recipe_summary_response = cls(
            artifact_count=artifact_count,
            expected_download_bytes=expected_download_bytes,
            lifecycle=lifecycle,
            maximum_installed_bytes_per_node=maximum_installed_bytes_per_node,
            maximum_runtime_memory_bytes_per_node=maximum_runtime_memory_bytes_per_node,
            origin=origin,
            profile_node_counts=profile_node_counts,
            recipe_id=recipe_id,
            revision_number=revision_number,
            runtime_family=runtime_family,
            slug=slug,
            source_bundle_sha256=source_bundle_sha256,
            title=title,
            content_sha256=content_sha256,
        )

        return recipe_summary_response
