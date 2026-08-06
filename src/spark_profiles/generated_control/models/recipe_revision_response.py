from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_revision_response_lifecycle import check_recipe_revision_response_lifecycle
from ..models.recipe_revision_response_lifecycle import RecipeRevisionResponseLifecycle
from ..models.recipe_revision_response_origin import check_recipe_revision_response_origin
from ..models.recipe_revision_response_origin import RecipeRevisionResponseOrigin
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.recipe_revision_response_document import RecipeRevisionResponseDocument





T = TypeVar("T", bound="RecipeRevisionResponse")



@_attrs_define
class RecipeRevisionResponse:
    """
        Attributes:
            activation_memory_bytes_per_node (int):
            artifact_count (int):
            created_at (str):
            created_by (str):
            description (str):
            document (RecipeRevisionResponseDocument):
            expected_download_bytes (int):
            id (str):
            installed_bytes_per_node (int):
            lifecycle (RecipeRevisionResponseLifecycle):
            max_nodes (int):
            min_nodes (int):
            origin (RecipeRevisionResponseOrigin):
            recipe_id (str):
            resident_memory_bytes_per_node (int):
            revision_number (int):
            runtime_family (str):
            runtime_image (str):
            schema_version (Literal[1]):
            slug (str):
            title (str):
            content_sha256 (Union[None, Unset, str]):
     """

    activation_memory_bytes_per_node: int
    artifact_count: int
    created_at: str
    created_by: str
    description: str
    document: 'RecipeRevisionResponseDocument'
    expected_download_bytes: int
    id: str
    installed_bytes_per_node: int
    lifecycle: RecipeRevisionResponseLifecycle
    max_nodes: int
    min_nodes: int
    origin: RecipeRevisionResponseOrigin
    recipe_id: str
    resident_memory_bytes_per_node: int
    revision_number: int
    runtime_family: str
    runtime_image: str
    schema_version: Literal[1]
    slug: str
    title: str
    content_sha256: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_revision_response_document import RecipeRevisionResponseDocument
        activation_memory_bytes_per_node = self.activation_memory_bytes_per_node

        artifact_count = self.artifact_count

        created_at = self.created_at

        created_by = self.created_by

        description = self.description

        document = self.document.to_dict()

        expected_download_bytes = self.expected_download_bytes

        id = self.id

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

        schema_version = self.schema_version

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
            "created_at": created_at,
            "created_by": created_by,
            "description": description,
            "document": document,
            "expected_download_bytes": expected_download_bytes,
            "id": id,
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
            "schema_version": schema_version,
            "slug": slug,
            "title": title,
        })
        if content_sha256 is not UNSET:
            field_dict["content_sha256"] = content_sha256

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_revision_response_document import RecipeRevisionResponseDocument
        d = dict(src_dict)
        activation_memory_bytes_per_node = d.pop("activation_memory_bytes_per_node")

        artifact_count = d.pop("artifact_count")

        created_at = d.pop("created_at")

        created_by = d.pop("created_by")

        description = d.pop("description")

        document = RecipeRevisionResponseDocument.from_dict(d.pop("document"))




        expected_download_bytes = d.pop("expected_download_bytes")

        id = d.pop("id")

        installed_bytes_per_node = d.pop("installed_bytes_per_node")

        lifecycle = check_recipe_revision_response_lifecycle(d.pop("lifecycle"))




        max_nodes = d.pop("max_nodes")

        min_nodes = d.pop("min_nodes")

        origin = check_recipe_revision_response_origin(d.pop("origin"))




        recipe_id = d.pop("recipe_id")

        resident_memory_bytes_per_node = d.pop("resident_memory_bytes_per_node")

        revision_number = d.pop("revision_number")

        runtime_family = d.pop("runtime_family")

        runtime_image = d.pop("runtime_image")

        schema_version = cast(Literal[1] , d.pop("schema_version"))
        if schema_version != 1:
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        slug = d.pop("slug")

        title = d.pop("title")

        def _parse_content_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        content_sha256 = _parse_content_sha256(d.pop("content_sha256", UNSET))


        recipe_revision_response = cls(
            activation_memory_bytes_per_node=activation_memory_bytes_per_node,
            artifact_count=artifact_count,
            created_at=created_at,
            created_by=created_by,
            description=description,
            document=document,
            expected_download_bytes=expected_download_bytes,
            id=id,
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
            schema_version=schema_version,
            slug=slug,
            title=title,
            content_sha256=content_sha256,
        )

        return recipe_revision_response
