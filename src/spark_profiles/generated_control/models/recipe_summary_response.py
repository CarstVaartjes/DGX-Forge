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
            activation_memory_bytes_per_node (int):
            artifact_count (int):
            expected_download_bytes (int):
            installed_bytes_per_node (int):
            lifecycle (RecipeSummaryResponseLifecycle):
            max_nodes (int):
            min_nodes (int):
            origin (RecipeSummaryResponseOrigin):
            recipe_id (str):
            resident_memory_bytes_per_node (int):
            revision_number (int):
            runtime_family (str):
            runtime_image (str):
            slug (str):
            title (str):
            content_sha256 (Union[None, Unset, str]):
     """

    activation_memory_bytes_per_node: int
    artifact_count: int
    expected_download_bytes: int
    installed_bytes_per_node: int
    lifecycle: RecipeSummaryResponseLifecycle
    max_nodes: int
    min_nodes: int
    origin: RecipeSummaryResponseOrigin
    recipe_id: str
    resident_memory_bytes_per_node: int
    revision_number: int
    runtime_family: str
    runtime_image: str
    slug: str
    title: str
    content_sha256: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        activation_memory_bytes_per_node = self.activation_memory_bytes_per_node

        artifact_count = self.artifact_count

        expected_download_bytes = self.expected_download_bytes

        installed_bytes_per_node = self.installed_bytes_per_node

        lifecycle: str = self.lifecycle

        max_nodes = self.max_nodes

        min_nodes = self.min_nodes

        origin: str = self.origin

        recipe_id = self.recipe_id

        resident_memory_bytes_per_node = self.resident_memory_bytes_per_node

        revision_number = self.revision_number

        runtime_family = self.runtime_family

        runtime_image = self.runtime_image

        slug = self.slug

        title = self.title

        content_sha256: Union[None, Unset, str]
        if isinstance(self.content_sha256, Unset):
            content_sha256 = UNSET
        else:
            content_sha256 = self.content_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "activation_memory_bytes_per_node": activation_memory_bytes_per_node,
            "artifact_count": artifact_count,
            "expected_download_bytes": expected_download_bytes,
            "installed_bytes_per_node": installed_bytes_per_node,
            "lifecycle": lifecycle,
            "max_nodes": max_nodes,
            "min_nodes": min_nodes,
            "origin": origin,
            "recipe_id": recipe_id,
            "resident_memory_bytes_per_node": resident_memory_bytes_per_node,
            "revision_number": revision_number,
            "runtime_family": runtime_family,
            "runtime_image": runtime_image,
            "slug": slug,
            "title": title,
        })
        if content_sha256 is not UNSET:
            field_dict["content_sha256"] = content_sha256

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        activation_memory_bytes_per_node = d.pop("activation_memory_bytes_per_node")

        artifact_count = d.pop("artifact_count")

        expected_download_bytes = d.pop("expected_download_bytes")

        installed_bytes_per_node = d.pop("installed_bytes_per_node")

        lifecycle = check_recipe_summary_response_lifecycle(d.pop("lifecycle"))




        max_nodes = d.pop("max_nodes")

        min_nodes = d.pop("min_nodes")

        origin = check_recipe_summary_response_origin(d.pop("origin"))




        recipe_id = d.pop("recipe_id")

        resident_memory_bytes_per_node = d.pop("resident_memory_bytes_per_node")

        revision_number = d.pop("revision_number")

        runtime_family = d.pop("runtime_family")

        runtime_image = d.pop("runtime_image")

        slug = d.pop("slug")

        title = d.pop("title")

        def _parse_content_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        content_sha256 = _parse_content_sha256(d.pop("content_sha256", UNSET))


        recipe_summary_response = cls(
            activation_memory_bytes_per_node=activation_memory_bytes_per_node,
            artifact_count=artifact_count,
            expected_download_bytes=expected_download_bytes,
            installed_bytes_per_node=installed_bytes_per_node,
            lifecycle=lifecycle,
            max_nodes=max_nodes,
            min_nodes=min_nodes,
            origin=origin,
            recipe_id=recipe_id,
            resident_memory_bytes_per_node=resident_memory_bytes_per_node,
            revision_number=revision_number,
            runtime_family=runtime_family,
            runtime_image=runtime_image,
            slug=slug,
            title=title,
            content_sha256=content_sha256,
        )

        return recipe_summary_response
