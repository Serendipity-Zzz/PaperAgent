"""Add traceable artifacts, links and local execution records."""

from alembic import context, op

from paperagent.db.models import ProjectBase

revision = "0010_execution_artifacts"
down_revision = "0009_provider_binding_backfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.get_x_argument(as_dictionary=True).get("kind", "global") == "global":
        return
    for table_name in ("artifacts", "artifact_links", "execution_records"):
        ProjectBase.metadata.tables[table_name].create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    # User-generated artifact metadata is preserved by design.
    return
