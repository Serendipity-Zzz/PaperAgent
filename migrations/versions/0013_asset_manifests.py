"""Persist document asset manifests and binding evidence additively."""

from alembic import context, op
from sqlalchemy import inspect

revision = "0013_asset_manifests"
down_revision = "0012_document_revisions"
branch_labels = None
depends_on = None


def _add_missing(table: str, additions: tuple[tuple[str, str], ...]) -> None:
    connection = op.get_bind()
    columns = {item["name"] for item in inspect(connection).get_columns(table)}
    for name, declaration in additions:
        if name not in columns:
            op.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def upgrade() -> None:
    if context.get_x_argument(as_dictionary=True).get("kind", "global") == "global":
        return
    connection = op.get_bind()
    tables = set(inspect(connection).get_table_names())
    if "document_revisions" in tables:
        _add_missing(
            "document_revisions",
            (
                ("asset_manifest_json", "TEXT"),
                ("asset_manifest_hash", "VARCHAR(64)"),
                ("image_required", "BOOLEAN NOT NULL DEFAULT 0"),
                ("expected_asset_count", "INTEGER NOT NULL DEFAULT 0"),
            ),
        )
        op.create_index(
            "ix_document_revisions_asset_manifest_hash",
            "document_revisions",
            ["asset_manifest_hash"],
            unique=False,
            if_not_exists=True,
        )
    if "document_revision_assets" in tables:
        _add_missing(
            "document_revision_assets",
            (
                ("logical_id", "VARCHAR(64)"),
                ("binding_evidence", "TEXT"),
                ("status", "VARCHAR(32) NOT NULL DEFAULT 'ready'"),
            ),
        )
        op.create_index(
            "ix_document_revision_assets_logical_id",
            "document_revision_assets",
            ["logical_id"],
            unique=False,
            if_not_exists=True,
        )
        op.create_index(
            "ix_document_revision_assets_status",
            "document_revision_assets",
            ["status"],
            unique=False,
            if_not_exists=True,
        )


def downgrade() -> None:
    # User lineage and binding evidence are retained intentionally.
    return
