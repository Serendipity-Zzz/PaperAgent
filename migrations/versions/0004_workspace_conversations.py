"""Add durable workspace conversation and message metadata."""

import sqlalchemy as sa
from alembic import context, op

revision = "0004_workspace_conversations"
down_revision = "0003_provider_settings"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    kind = context.get_x_argument(as_dictionary=True).get("kind", "global")
    if kind == "global":
        columns = _columns("projects")
        for column in (
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.String(32), nullable=False, server_default="active"),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        ):
            if column.name not in columns:
                op.add_column("projects", column)
        if "ix_projects_status" not in _indexes("projects"):
            op.create_index("ix_projects_status", "projects", ["status"])
        return

    session_columns = _columns("sessions")
    for column in (
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("last_read_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    ):
        if column.name not in session_columns:
            op.add_column("sessions", column)
    if "ix_sessions_status" not in _indexes("sessions"):
        op.create_index("ix_sessions_status", "sessions", ["status"])

    message_columns = _columns("messages")
    additions = (
        sa.Column("sequence", sa.Integer(), nullable=True),
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("parent_message_id", sa.String(36), nullable=True),
        sa.Column("branch_id", sa.String(36), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="final"),
        sa.Column("superseded_by_message_id", sa.String(36), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    for column in additions:
        if column.name not in message_columns:
            op.add_column("messages", column)
    op.execute(
        """
        UPDATE messages AS current
        SET sequence = (
            SELECT COUNT(*) FROM messages AS previous
            WHERE previous.session_id = current.session_id
              AND (previous.created_at < current.created_at
                   OR (previous.created_at = current.created_at AND previous.id <= current.id))
        )
        WHERE sequence IS NULL
        """
    )
    op.execute("UPDATE messages SET updated_at = created_at WHERE updated_at IS NULL")
    indexes = _indexes("messages")
    for name, columns in (
        ("ix_messages_run_id", ["run_id"]),
        ("ix_messages_branch_id", ["branch_id"]),
        ("ix_messages_status", ["status"]),
        ("uq_messages_session_sequence", ["session_id", "sequence"]),
    ):
        if name not in indexes:
            op.create_index(name, "messages", columns, unique=name.startswith("uq_"))


def downgrade() -> None:
    # The project is file-first and rollback uses the mandatory pre-migration backup.
    # SQLite column removal would rebuild user tables and is intentionally not automatic.
    pass
