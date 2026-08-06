"""Fresh-evidence memory, port, capability, and topology admission."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .install_admission import AdmissionReason
from .inventory_repository import InventoryRepository
from .models import AgentNode, InstallationNode, LocalRecipeRevision, RecipeInstallation, RecipeRun, ResourceReservation, RunNode
from .topology import Placement, TopologyError, validate_topology


class RunPlanConflict(RuntimeError): pass


@dataclass(frozen=True, slots=True)
class RunNodePlan:
    node_id: str; rank: int; role: str; port: int; allowed: bool; inventory_observed_at: datetime | None
    required_memory_bytes: int; host_free_bytes: int | None; gpu_free_bytes: int | None; active_host_reserved_bytes: int; active_gpu_reserved_bytes: int; free_after_bytes: int | None; memory_floor_bytes: int
    blockers: tuple[AdmissionReason,...]; warnings: tuple[AdmissionReason,...]


@dataclass(frozen=True, slots=True)
class RunPlan:
    installation_id: str; recipe_revision_id: str; allowed: bool; nodes: tuple[RunNodePlan,...]; plan_digest: str


class RunAdmissionService:
    def __init__(self,sessions:sessionmaker[Session],*,inventory_max_age:int=300,memory_floor_bytes:int=4_000_000_000)->None:
        self._sessions=sessions;self._inventory=InventoryRepository(sessions);self._max_age=inventory_max_age;self._floor=memory_floor_bytes

    def plan_run(self,installation_id:str,placements:tuple[Placement,...],*,now:datetime)->RunPlan:
        with self._sessions() as session:
            installation=session.get(RecipeInstallation,installation_id)
            if installation is None: raise KeyError(installation_id)
            if installation.state!="installed": raise ValueError("recipe installation is not complete")
            revision=session.get(LocalRecipeRevision,installation.recipe_revision_id)
            if revision is None or revision.lifecycle!="resolved": raise ValueError("recipe revision is unavailable")
            installed_nodes={row.node_id for row in session.scalars(select(InstallationNode).where(InstallationNode.installation_id==installation_id,InstallationNode.state=="installed"))}
        capabilities:dict[str,tuple[str,...]]={};snapshots={}
        for placement in placements:
            try: snapshot=self._inventory.latest(placement.node_id,now=now,maximum_age=self._max_age);snapshots[placement.node_id]=snapshot;capabilities[placement.node_id]=snapshot.capabilities
            except KeyError: pass
        topology_reason:AdmissionReason|None=None
        try: ordered=validate_topology(revision.document,placements,capabilities)
        except TopologyError as error: ordered=tuple(sorted(placements,key=lambda item:item.rank));topology_reason=AdmissionReason(error.code,str(error))
        resources=revision.document["resources"];endpoint=revision.document["endpoint"]
        assert isinstance(resources,dict) and isinstance(resources["per_node"],dict) and isinstance(endpoint,dict)
        required=int(resources["per_node"]["resident_memory_bytes"])+int(resources["per_node"]["activation_memory_bytes"]);port=int(endpoint["port"])
        plans=[]
        for placement in ordered:
            blockers=[] if topology_reason is None else [topology_reason];warnings=[];snapshot=snapshots.get(placement.node_id)
            if placement.node_id not in installed_nodes: blockers.append(AdmissionReason("run.not_installed","Recipe artifacts are not installed on this Spark."))
            if snapshot is None: blockers.append(AdmissionReason("run.inventory_missing","No authenticated memory inventory is available."))
            elif snapshot.stale: blockers.append(AdmissionReason("run.stale_inventory","Spark memory inventory is stale."))
            with self._sessions() as session:
                host_reserved=int(session.scalar(select(func.coalesce(func.sum(ResourceReservation.amount_bytes),0)).where(ResourceReservation.node_id==placement.node_id,ResourceReservation.kind=="host-memory",ResourceReservation.state=="active")) or 0)
                gpu_reserved=int(session.scalar(select(func.coalesce(func.sum(ResourceReservation.amount_bytes),0)).where(ResourceReservation.node_id==placement.node_id,ResourceReservation.kind=="gpu-memory",ResourceReservation.state=="active")) or 0)
                occupied=session.scalar(select(ResourceReservation.id).where(ResourceReservation.node_id==placement.node_id,ResourceReservation.kind=="port",ResourceReservation.resource_key==str(port),ResourceReservation.state=="active"))
            if occupied is not None: blockers.append(AdmissionReason("run.port_occupied",f"Port {port} is already reserved on this Spark."))
            host_free=snapshot.host_memory_free_bytes if snapshot else None;gpu_free=snapshot.gpu_memory_free_bytes if snapshot else None
            free_after=None if snapshot is None else min(host_free-host_reserved-required,gpu_free-gpu_reserved-required)
            if free_after is not None and free_after<self._floor: blockers.append(AdmissionReason("run.insufficient_memory",f"Run would leave {free_after} bytes, below the {self._floor}-byte memory floor."))
            plans.append(RunNodePlan(placement.node_id,placement.rank,placement.role,port,not blockers,snapshot.observed_at if snapshot else None,required,host_free,gpu_free,host_reserved,gpu_reserved,free_after,self._floor,tuple(blockers),tuple(warnings)))
        identity={"schema_version":1,"installation_id":installation_id,"recipe_revision_id":revision.id,"nodes":[{**asdict(node),"inventory_observed_at":node.inventory_observed_at.isoformat() if node.inventory_observed_at else None} for node in plans]}
        digest=hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(",",":"),default=str).encode()).hexdigest()
        return RunPlan(installation_id,revision.id,all(node.allowed for node in plans),tuple(plans),digest)

    def accept_run(self,plan:RunPlan,*,alias:str,actor:str,now:datetime)->str:
        fresh=self.plan_run(plan.installation_id,tuple(Placement(node.node_id,node.rank,node.role) for node in plan.nodes),now=now)
        if not fresh.allowed or fresh.plan_digest!=plan.plan_digest: raise RunPlanConflict("run plan is stale or blocked")
        document={"schema_version":1,"installation_id":plan.installation_id,"recipe_revision_id":plan.recipe_revision_id,"plan_digest":plan.plan_digest,"nodes":[{**asdict(node),"inventory_observed_at":node.inventory_observed_at.isoformat() if node.inventory_observed_at else None} for node in plan.nodes]}
        run=RecipeRun(installation_id=plan.installation_id,alias=alias,plan_digest=plan.plan_digest,plan=document,state="planned",actor=actor,created_at=now,updated_at=now)
        with self._sessions.begin() as session:
            for node in plan.nodes:
                if session.scalar(select(AgentNode).where(AgentNode.node_id==node.node_id).with_for_update()) is None: raise RunPlanConflict("run node disappeared")
                if session.scalar(select(ResourceReservation.id).where(ResourceReservation.node_id==node.node_id,ResourceReservation.kind=="port",ResourceReservation.resource_key==str(node.port),ResourceReservation.state=="active")) is not None: raise RunPlanConflict("run port changed while reserving")
            session.add(run);session.flush()
            for node in plan.nodes:
                session.add(RunNode(run_id=run.id,node_id=node.node_id,rank=node.rank,role=node.role,state="planned",port=node.port,reserved_memory_bytes=node.required_memory_bytes,updated_at=now))
                for kind,amount,key in (("host-memory",node.required_memory_bytes,plan.plan_digest),("gpu-memory",node.required_memory_bytes,plan.plan_digest),("port",0,str(node.port))): session.add(ResourceReservation(node_id=node.node_id,kind=kind,resource_key=key,amount_bytes=amount,owner_kind="run",owner_id=run.id,state="active",plan_digest=plan.plan_digest,created_at=now))
        return run.id
