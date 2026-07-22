"""Add DocumentIR revision lineage and artifact derivation metadata."""

from alembic import context, op
from sqlalchemy import inspect

from paperagent.db.models import ProjectBase

revision = "0012_document_revisions"
down_revision = "0011_artifact_backfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.get_x_argument(as_dictionary=True).get("kind", "global") == "global":
        return
    connection = op.get_bind()
    inspector = inspect(connection)
    existing = set(inspector.get_table_names())
    for table_name in ("documents", "document_revisions", "document_revision_assets"):
        if table_name not in existing:
            ProjectBase.metadata.tables[table_name].create(bind=connection, checkfirst=True)

    artifact_columns = {item["name"] for item in inspect(connection).get_columns("artifacts")}
    additions = (
        ("document_id", "VARCHAR(36)"),
        ("revision_id", "VARCHAR(36)"),
        ("derived_from_artifact_id", "VARCHAR(36)"),
    )
    for name, column_type in additions:
        if name not in artifact_columns:
            op.execute(f"ALTER TABLE artifacts ADD COLUMN {name} {column_type}")
        op.create_index(
            f"ix_artifacts_{name}",
            "artifacts",
            [name],
            unique=False,
            if_not_exists=True,
        )


def downgrade() -> None:
    # Document lineage and user artifact metadata are intentionally retained.
    return
