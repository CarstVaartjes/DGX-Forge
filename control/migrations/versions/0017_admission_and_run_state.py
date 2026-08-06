"""Persist inventory, artifacts, reservations, installations, and runs."""

import sqlalchemy as sa
from alembic import op

revision = "0017_admission_and_run_state"
down_revision = "0016_recipe_deployment_authority"
branch_labels = None
depends_on = None


def digest(column: str) -> str:
    remainder = column
    for character in "0123456789abcdef": remainder = f"replace({remainder}, '{character}', '')"
    return f"length({column})=64 AND {column}=lower({column}) AND length({remainder})=0"


def upgrade() -> None:
    op.create_table("node_inventory_snapshots",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("node_id", sa.String(36), sa.ForeignKey("agent_nodes.node_id", ondelete="CASCADE"), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False), sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("disk_total_bytes", sa.BigInteger, nullable=False), sa.Column("disk_free_bytes", sa.BigInteger, nullable=False),
        sa.Column("host_memory_total_bytes", sa.BigInteger, nullable=False), sa.Column("host_memory_free_bytes", sa.BigInteger, nullable=False),
        sa.Column("gpu_memory_total_bytes", sa.BigInteger, nullable=False), sa.Column("gpu_memory_free_bytes", sa.BigInteger, nullable=False), sa.Column("gpu_count", sa.Integer, nullable=False),
        sa.Column("artifact_store_read_only", sa.Boolean, nullable=False), sa.Column("capabilities", sa.JSON, nullable=False), sa.Column("evidence_digest", sa.String(64), nullable=False, unique=True),
        sa.CheckConstraint("disk_total_bytes>=0 AND disk_free_bytes>=0 AND disk_free_bytes<=disk_total_bytes", name="ck_inventory_disk"),
        sa.CheckConstraint("host_memory_total_bytes>=0 AND host_memory_free_bytes>=0 AND host_memory_free_bytes<=host_memory_total_bytes", name="ck_inventory_host_memory"),
        sa.CheckConstraint("gpu_memory_total_bytes>=0 AND gpu_memory_free_bytes>=0 AND gpu_memory_free_bytes<=gpu_memory_total_bytes AND gpu_count>=0", name="ck_inventory_gpu_memory"),
        sa.CheckConstraint(digest("evidence_digest"), name="ck_inventory_digest"), sa.UniqueConstraint("node_id", "observed_at", name="uq_inventory_node_observed"))
    op.create_index("ix_inventory_node_observed", "node_inventory_snapshots", ["node_id", "observed_at"])
    op.create_table("node_artifacts",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("node_id", sa.String(36), sa.ForeignKey("agent_nodes.node_id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False), sa.Column("digest", sa.String(64), nullable=False), sa.Column("source", sa.Text, nullable=False), sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("state", sa.String(24), nullable=False), sa.Column("ref_count", sa.Integer, nullable=False, server_default="0"), sa.Column("verified_at", sa.DateTime(timezone=True)), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('image','image-layer','model','auxiliary')", name="ck_node_artifacts_kind"), sa.CheckConstraint("state IN ('partial','verified','missing','corrupt')", name="ck_node_artifacts_state"),
        sa.CheckConstraint("size_bytes>=0 AND ref_count>=0", name="ck_node_artifacts_sizes"), sa.CheckConstraint(digest("digest"), name="ck_node_artifacts_digest"), sa.UniqueConstraint("node_id", "digest", name="uq_node_artifact_digest"))
    op.create_index("ix_node_artifacts_node", "node_artifacts", ["node_id"])
    op.create_table("recipe_installations",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("recipe_revision_id", sa.String(36), sa.ForeignKey("local_recipe_revisions.id", ondelete="RESTRICT"), nullable=False), sa.Column("plan_digest", sa.String(64), nullable=False, unique=True), sa.Column("plan", sa.JSON, nullable=False),
        sa.Column("state", sa.String(24), nullable=False), sa.Column("actor", sa.String(200), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(digest("plan_digest"), name="ck_recipe_installations_digest"), sa.CheckConstraint("state IN ('planned','installing','installed','partial','failed','uninstalled')", name="ck_recipe_installations_state"))
    op.create_table("installation_nodes",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("installation_id", sa.String(36), sa.ForeignKey("recipe_installations.id", ondelete="CASCADE"), nullable=False), sa.Column("node_id", sa.String(36), sa.ForeignKey("agent_nodes.node_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("state", sa.String(24), nullable=False), sa.Column("required_bytes", sa.BigInteger, nullable=False), sa.Column("installed_bytes", sa.BigInteger, nullable=False, server_default="0"), sa.Column("evidence_digest", sa.String(64)), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("required_bytes>=0 AND installed_bytes>=0", name="ck_installation_nodes_bytes"), sa.UniqueConstraint("installation_id", "node_id", name="uq_installation_node"))
    op.create_table("recipe_runs",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("installation_id", sa.String(36), sa.ForeignKey("recipe_installations.id", ondelete="RESTRICT"), nullable=False), sa.Column("alias", sa.String(128), nullable=False), sa.Column("plan_digest", sa.String(64), nullable=False, unique=True), sa.Column("plan", sa.JSON, nullable=False),
        sa.Column("state", sa.String(24), nullable=False), sa.Column("actor", sa.String(200), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), sa.Column("stopped_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(digest("plan_digest"), name="ck_recipe_runs_digest"), sa.CheckConstraint("state IN ('planned','starting','running','stopping','stopped','failed','lost')", name="ck_recipe_runs_state"))
    op.create_table("run_nodes",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("run_id", sa.String(36), sa.ForeignKey("recipe_runs.id", ondelete="CASCADE"), nullable=False), sa.Column("node_id", sa.String(36), sa.ForeignKey("agent_nodes.node_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("rank", sa.Integer, nullable=False), sa.Column("role", sa.String(16), nullable=False), sa.Column("state", sa.String(24), nullable=False), sa.Column("port", sa.Integer, nullable=False),
        sa.Column("reserved_memory_bytes", sa.BigInteger, nullable=False), sa.Column("observed_memory_bytes", sa.BigInteger), sa.Column("endpoint", sa.JSON), sa.Column("evidence_digest", sa.String(64)), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("rank>=0 AND port BETWEEN 1024 AND 65535 AND reserved_memory_bytes>=0 AND (observed_memory_bytes IS NULL OR observed_memory_bytes>=0)", name="ck_run_nodes_resources"),
        sa.CheckConstraint("role IN ('entrypoint','worker')", name="ck_run_nodes_role"), sa.UniqueConstraint("run_id", "node_id", name="uq_run_node"), sa.UniqueConstraint("run_id", "rank", name="uq_run_rank"))
    op.create_table("resource_reservations",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("node_id", sa.String(36), sa.ForeignKey("agent_nodes.node_id", ondelete="RESTRICT"), nullable=False), sa.Column("kind", sa.String(16), nullable=False), sa.Column("resource_key", sa.String(128), nullable=False), sa.Column("amount_bytes", sa.BigInteger, nullable=False),
        sa.Column("owner_kind", sa.String(24), nullable=False), sa.Column("owner_id", sa.String(36), nullable=False), sa.Column("state", sa.String(16), nullable=False), sa.Column("plan_digest", sa.String(64), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("kind IN ('disk','host-memory','gpu-memory','port')", name="ck_reservations_kind"), sa.CheckConstraint("state IN ('active','released','expired') AND amount_bytes>=0", name="ck_reservations_state"), sa.CheckConstraint(digest("plan_digest"), name="ck_reservations_digest"))
    op.create_index("ix_reservations_node_state", "resource_reservations", ["node_id", "state"])
    op.create_index("uq_active_node_resource", "resource_reservations", ["node_id", "kind", "resource_key"], unique=True, postgresql_where=sa.text("state='active'"), sqlite_where=sa.text("state='active'"))


def downgrade() -> None:
    for table in ("resource_reservations", "run_nodes", "recipe_runs", "installation_nodes", "recipe_installations", "node_artifacts", "node_inventory_snapshots"): op.drop_table(table)
