"""Evolve tasks into durable runs and add run-scoped event sequences."""

import sqlalchemy as sa
from alembic import context, op

from paperagent.db.models import ProjectBase

revision = "0005_durable_runs"
down_revision = "0004_workspace_conversations"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    if context.get_x_argument(as_dictionary=True).get("kind", "global") == "global":
        return

    inspector = sa.inspect(op.get_bind())
    for table_name in ("tasks", "events"):
        if not inspector.has_table(table_name):
            ProjectBase.metadata.tables[table_name].create(bind=op.get_bind(), checkfirst=True)

    task_columns = _columns("tasks")
    task_additions = (
        sa.Column("conversation_id", sa.String(36), nullable=True),
        sa.Column("parent_task_id", sa.String(36), nullable=True),
        sa.Column("current_phase", sa.String(64), nullable=False, server_default="queued"),
        sa.Column("checkpoint_ref", sa.Text(), nullable=True),
        sa.Column("provider_snapshot_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("context_snapshot_ref", sa.Text(), nullable=True),
        sa.Column("tool_policy_snapshot_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("worker_id", sa.String(128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_output_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("recovery_strategy", sa.String(64), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    for column in task_additions:
        if column.name not in task_columns:
            op.add_column("tasks", column)
    op.execute(
        "UPDATE tasks SET conversation_id = json_extract(payload_json, '$.session_id') "
        "WHERE conversation_id IS NULL AND json_valid(payload_json)"
    )
    task_indexes = _indexes("tasks")
    for name, columns in (
        ("ix_tasks_conversation_id", ["conversation_id"]),
        ("ix_tasks_parent_task_id", ["parent_task_id"]),
        ("ix_tasks_worker_id", ["worker_id"]),
        ("ix_tasks_lease_expires_at", ["lease_expires_at"]),
    ):
        if name not in task_indexes:
            op.create_index(name, "tasks", columns)

    event_columns = _columns("events")
    for column in (
        sa.Column("run_sequence", sa.Integer(), nullable=True),
        sa.Column("internal_payload_ref", sa.Text(), nullable=True),
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="1"),
    ):
        if column.name not in event_columns:
            op.add_column("events", column)
    op.execute(
        """
        UPDATE events AS current
        SET run_sequence = (
            SELECT COUNT(*) FROM events AS previous
            WHERE previous.task_id = current.task_id
              AND previous.task_id IS NOT NULL
              AND previous.sequence <= current.sequence
        )
        WHERE task_id IS NOT NULL AND run_sequence IS NULL
        """
    )
    event_indexes = _indexes("events")
    if "uq_events_task_run_sequence" not in event_indexes:
        op.create_index(
            "uq_events_task_run_sequence",
            "events",
            ["task_id", "run_sequence"],
            unique=True,
        )


def downgrade() -> None:
    # Additive compatibility migration; downgrade intentionally preserves durable run data.
    return
