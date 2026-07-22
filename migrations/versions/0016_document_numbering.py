"""Persist the independent DocumentIR numbering hash."""

from alembic import context, op
from sqlalchemy import inspect

revision = "0016_document_numbering"
down_revision = "0015_document_presentation"
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
    if "numbering_hash" not in columns:
        op.execute(
            "ALTER TABLE document_revisions ADD COLUMN numbering_hash "
            "VARCHAR(64) NOT NULL DEFAULT ''"
        )
    op.create_index(
        "ix_document_revisions_numbering_hash",
        "document_revisions",
        ["numbering_hash"],
        unique=False,
        if_not_exists=True,
    )


def downgrade() -> None:
    # Numbering lineage is intentionally retained.
    return
