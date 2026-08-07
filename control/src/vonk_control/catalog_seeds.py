from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import insert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .models import PackageFamily


@dataclass(frozen=True, slots=True)
class SeedResult:
    created: int
    identifiers: tuple[str, ...]


STANDARD_FAMILIES: tuple[dict[str, object], ...] = (
    {
        "id": "oci",
        "display_name": "OCI image",
        "provider_kind": "artifact",
        "capability": "artifact.oci.v1",
    },
    {
        "id": "huggingface-snapshot",
        "display_name": "Hugging Face snapshot",
        "provider_kind": "artifact",
        "capability": "artifact.huggingface-snapshot.v1",
    },
    {
        "id": "vllm",
        "display_name": "vLLM",
        "provider_kind": "runtime",
        "capability": "runtime.vllm.v1",
    },
    {
        "id": "sglang",
        "display_name": "SGLang",
        "provider_kind": "runtime",
        "capability": "runtime.sglang.v1",
    },
    {
        "id": "llama-cpp",
        "display_name": "llama.cpp",
        "provider_kind": "runtime",
        "capability": "runtime.llama-cpp.v1",
    },
)


def seed_standard_families(session: Session, now: datetime) -> SeedResult:
    if now.tzinfo is None:
        raise ValueError("catalog seed timestamp must be timezone-aware")
    created = 0
    identifiers: list[str] = []
    dialect = session.get_bind().dialect.name
    for seed in STANDARD_FAMILIES:
        identifier = str(seed["id"])
        values = {
            "id": identifier,
            "display_name": seed["display_name"],
            "provider_kind": seed["provider_kind"],
            "schema_version": 1,
            "definition": {
                "schema_version": 1,
                "recipe_schema_versions": [1],
                "architecture": "linux/arm64",
                "capability": seed["capability"],
            },
            "builtin": True,
            "created_at": now,
            "updated_at": now,
        }
        if dialect == "postgresql":
            statement = postgresql_insert(PackageFamily).values(**values)
            statement = statement.on_conflict_do_nothing(index_elements=["id"])
        elif dialect == "sqlite":
            statement = sqlite_insert(PackageFamily).values(**values)
            statement = statement.on_conflict_do_nothing(index_elements=["id"])
        else:
            statement = insert(PackageFamily).values(**values)
        result = session.execute(statement)
        if result.rowcount == 1:
            created += 1
            identifiers.append(identifier)
    return SeedResult(created=created, identifiers=tuple(identifiers))
