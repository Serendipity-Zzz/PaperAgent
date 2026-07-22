"""Add provider settings."""

from alembic import context, op

from paperagent.db.models import GlobalBase

revision = "0003_provider_settings"
down_revision = "0002_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.get_x_argument(as_dictionary=True).get("kind", "global") == "global":
        GlobalBase.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    if context.get_x_argument(as_dictionary=True).get("kind", "global") == "global":
        for name in ("app_settings", "providers"):
            GlobalBase.metadata.tables[name].drop(bind=op.get_bind(), checkfirst=True)
