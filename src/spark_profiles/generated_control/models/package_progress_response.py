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
  from ..models.package_node_progress import PackageNodeProgress
  from ..models.package_progress import PackageProgress





T = TypeVar("T", bound="PackageProgressResponse")



@_attrs_define
class PackageProgressResponse:
    """
        Attributes:
            id (str):
            plan_digest (str):
            progress (PackageProgress):
            state (str):
            audit_request_id (Union[None, Unset, str]):
            failure (Union[None, Unset, str]):
            job_id (Union[None, Unset, str]):
            nodes (Union[Unset, list['PackageNodeProgress']]):
            rollback_rollout_id (Union[None, Unset, str]):
            rollback_selector (Union[None, Unset, str]):
     """

    id: str
    plan_digest: str
    progress: 'PackageProgress'
    state: str
    audit_request_id: Union[None, Unset, str] = UNSET
    failure: Union[None, Unset, str] = UNSET
    job_id: Union[None, Unset, str] = UNSET
    nodes: Union[Unset, list['PackageNodeProgress']] = UNSET
    rollback_rollout_id: Union[None, Unset, str] = UNSET
    rollback_selector: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.package_node_progress import PackageNodeProgress
        from ..models.package_progress import PackageProgress
        id = self.id

        plan_digest = self.plan_digest

        progress = self.progress.to_dict()

        state = self.state

        audit_request_id: Union[None, Unset, str]
        if isinstance(self.audit_request_id, Unset):
            audit_request_id = UNSET
        else:
            audit_request_id = self.audit_request_id

        failure: Union[None, Unset, str]
        if isinstance(self.failure, Unset):
            failure = UNSET
        else:
            failure = self.failure

        job_id: Union[None, Unset, str]
        if isinstance(self.job_id, Unset):
            job_id = UNSET
        else:
            job_id = self.job_id

        nodes: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.nodes, Unset):
            nodes = []
            for nodes_item_data in self.nodes:
                nodes_item = nodes_item_data.to_dict()
                nodes.append(nodes_item)



        rollback_rollout_id: Union[None, Unset, str]
        if isinstance(self.rollback_rollout_id, Unset):
            rollback_rollout_id = UNSET
        else:
            rollback_rollout_id = self.rollback_rollout_id

        rollback_selector: Union[None, Unset, str]
        if isinstance(self.rollback_selector, Unset):
            rollback_selector = UNSET
        else:
            rollback_selector = self.rollback_selector


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "id": id,
            "plan_digest": plan_digest,
            "progress": progress,
            "state": state,
        })
        if audit_request_id is not UNSET:
            field_dict["audit_request_id"] = audit_request_id
        if failure is not UNSET:
            field_dict["failure"] = failure
        if job_id is not UNSET:
            field_dict["job_id"] = job_id
        if nodes is not UNSET:
            field_dict["nodes"] = nodes
        if rollback_rollout_id is not UNSET:
            field_dict["rollback_rollout_id"] = rollback_rollout_id
        if rollback_selector is not UNSET:
            field_dict["rollback_selector"] = rollback_selector

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.package_node_progress import PackageNodeProgress
        from ..models.package_progress import PackageProgress
        d = dict(src_dict)
        id = d.pop("id")

        plan_digest = d.pop("plan_digest")

        progress = PackageProgress.from_dict(d.pop("progress"))




        state = d.pop("state")

        def _parse_audit_request_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        audit_request_id = _parse_audit_request_id(d.pop("audit_request_id", UNSET))


        def _parse_failure(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        failure = _parse_failure(d.pop("failure", UNSET))


        def _parse_job_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        job_id = _parse_job_id(d.pop("job_id", UNSET))


        nodes = []
        _nodes = d.pop("nodes", UNSET)
        for nodes_item_data in (_nodes or []):
            nodes_item = PackageNodeProgress.from_dict(nodes_item_data)



            nodes.append(nodes_item)


        def _parse_rollback_rollout_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        rollback_rollout_id = _parse_rollback_rollout_id(d.pop("rollback_rollout_id", UNSET))


        def _parse_rollback_selector(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        rollback_selector = _parse_rollback_selector(d.pop("rollback_selector", UNSET))


        package_progress_response = cls(
            id=id,
            plan_digest=plan_digest,
            progress=progress,
            state=state,
            audit_request_id=audit_request_id,
            failure=failure,
            job_id=job_id,
            nodes=nodes,
            rollback_rollout_id=rollback_rollout_id,
            rollback_selector=rollback_selector,
        )

        return package_progress_response
