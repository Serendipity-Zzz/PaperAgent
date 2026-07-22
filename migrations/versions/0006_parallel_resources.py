"""Add resource admission and unread activity metadata."""

import sqlalchemy as sa
from alembic import context, op

revision = "0006_parallel_resources"
down_revision = "0005_durable_runs"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if context.get_x_argument(as_dictionary=True).get("kind", "global") == "global":
        return
    columns = _columns("tasks")
    for column in (
        sa.Column("resource_request_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notification_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
    ):
        if column.name not in columns:
            op.add_column("tasks", column)


def downgrade() -> None:
    return
