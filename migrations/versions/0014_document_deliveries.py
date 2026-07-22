"""Add canonical document delivery lifecycle and artifact publication metadata."""

from alembic import context, op
from sqlalchemy import inspect

from paperagent.db.models import ProjectBase

revision = "0014_document_deliveries"
down_revision = "0013_asset_manifests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.get_x_argument(as_dictionary=True).get("kind", "global") == "global":
        return
    connection = op.get_bind()
    tables = set(inspect(connection).get_table_names())
    if "document_deliveries" not in tables:
        ProjectBase.metadata.tables["document_deliveries"].create(
            bind=connection, checkfirst=True
        )
    if "artifacts" in tables:
        artifact_columns = {
            item["name"] for item in inspect(connection).get_columns("artifacts")
        }
        for name, declaration in (
            ("delivery_status", "VARCHAR(32) NOT NULL DEFAULT 'not_applicable'"),
            ("renderer_version", "VARCHAR(64)"),
            ("lineage_json", "TEXT NOT NULL DEFAULT '{}'"),
        ):
            if name not in artifact_columns:
                op.execute(f"ALTER TABLE artifacts ADD COLUMN {name} {declaration}")
        op.create_index(
            "ix_artifacts_delivery_status",
            "artifacts",
            ["delivery_status"],
            unique=False,
            if_not_exists=True,
        )


def downgrade() -> None:
    # Delivery audit history is intentionally retained.
    return
