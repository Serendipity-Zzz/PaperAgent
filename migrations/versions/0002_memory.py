"""Add memory storage."""

from alembic import context, op

from paperagent.db.models import GlobalBase

revision = "0002_memory"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    kind = context.get_x_argument(as_dictionary=True).get("kind", "global")
    if kind == "global":
        GlobalBase.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    if context.get_x_argument(as_dictionary=True).get("kind", "global") == "global":
        GlobalBase.metadata.tables["memories"].drop(bind=op.get_bind(), checkfirst=True)
