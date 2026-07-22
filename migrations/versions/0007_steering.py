"""Add durable steering decision audit records."""

from alembic import context, op

from paperagent.db.models import ProjectBase

revision = "0007_steering"
down_revision = "0006_parallel_resources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.get_x_argument(as_dictionary=True).get("kind", "global") == "project":
        ProjectBase.metadata.tables["steering_decisions"].create(
            bind=op.get_bind(), checkfirst=True
        )


def downgrade() -> None:
    return
