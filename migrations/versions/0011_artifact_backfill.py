"""Backfill legacy file metadata into the artifact catalog."""

from alembic import context, op
from sqlalchemy import inspect, text

revision = "0011_artifact_backfill"
down_revision = "0010_execution_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.get_x_argument(as_dictionary=True).get("kind", "global") == "global":
        return
    connection = op.get_bind()
    if inspect(connection).has_table("files"):
        artifact_columns = {
            item["name"] for item in inspect(connection).get_columns("artifacts")
        }
        future_columns: list[str] = []
        future_values: list[str] = []
        for name, value in (
            ("delivery_status", "'not_applicable'"),
            ("renderer_version", "NULL"),
            ("lineage_json", "'{}'"),
        ):
            if name in artifact_columns:
                future_columns.append(name)
                future_values.append(value)
        extra_columns = ", " + ", ".join(future_columns) if future_columns else ""
        extra_values = ", " + ", ".join(future_values) if future_values else ""
        connection.execute(
            text(
                f"""
                INSERT OR IGNORE INTO artifacts(
                    id, kind, mime_type, original_name, relative_path, sha256, size_bytes,
                    producer_tool, producer_version, run_id, source_artifact_ids_json,
                    environment_ref, preview_status, validation_status, created_at
                    {extra_columns}
                )
                SELECT id, category,
                    CASE
                        WHEN lower(original_name) LIKE '%.pdf' THEN 'application/pdf'
                        WHEN lower(original_name) LIKE '%.docx' THEN
                            'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
                        WHEN lower(original_name) LIKE '%.png' THEN 'image/png'
                        WHEN lower(original_name) LIKE '%.svg' THEN 'image/svg+xml'
                        WHEN lower(original_name) LIKE '%.csv' THEN 'text/csv'
                        WHEN lower(original_name) LIKE '%.md' THEN 'text/markdown'
                        ELSE 'application/octet-stream'
                    END,
                    original_name, relative_path, sha256, size_bytes,
                    'legacy.file', '1.0.0', NULL, '[]', NULL, 'pending', 'valid', created_at
                    {extra_values}
                FROM files
                """
            )
        )
    op.create_index(
        "ix_artifact_links_message_relation",
        "artifact_links",
        ["message_id", "relation"],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        "ix_artifacts_run_kind",
        "artifacts",
        ["run_id", "kind"],
        unique=False,
        if_not_exists=True,
    )


def downgrade() -> None:
    # Non-destructive by design: user artifact metadata is retained.
    return
