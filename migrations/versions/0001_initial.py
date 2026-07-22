"""Initial global and project schemas."""

from alembic import context, op

from paperagent.db.models import GlobalBase, ProjectBase

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def selected_metadata():
    kind = context.get_x_argument(as_dictionary=True).get("kind", "global")
    return ProjectBase.metadata if kind == "project" else GlobalBase.metadata


def upgrade() -> None:
    selected_metadata().create_all(bind=op.get_bind())


def downgrade() -> None:
    selected_metadata().drop_all(bind=op.get_bind())
