"""Persist the independent DocumentIR presentation hash."""

from alembic import context, op
from sqlalchemy import inspect

revision = "0015_document_presentation"
down_revision = "0014_document_deliveries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.get_x_argument(as_dictionary=True).get("kind", "global") == "global":
        return
    connection = op.get_bind()
    tables = set(inspect(connection).get_table_names())
    if "document_revisions" not in tables:
        return
    columns = {item["name"] for item in inspect(connection).get_columns("document_revisions")}
    if "presentation_hash" not in columns:
        op.execute(
            "ALTER TABLE document_revisions ADD COLUMN presentation_hash "
            "VARCHAR(64) NOT NULL DEFAULT ''"
        )
    op.create_index(
        "ix_document_revisions_presentation_hash",
        "document_revisions",
        ["presentation_hash"],
        unique=False,
        if_not_exists=True,
    )


def downgrade() -> None:
    # Presentation lineage is intentionally retained.
    return
