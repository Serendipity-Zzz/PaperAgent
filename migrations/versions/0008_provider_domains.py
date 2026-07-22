"""Separate provider modalities, bindings, health and immutable versions."""

import sqlalchemy as sa
from alembic import context, op

revision = "0008_provider_domains"
down_revision = "0007_steering"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.get_x_argument(as_dictionary=True).get("kind", "global") != "global":
        return
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {column["name"] for column in inspector.get_columns("providers")}
    additions = {
        "display_name": sa.Column(
            "display_name", sa.String(255), nullable=False, server_default=""
        ),
        "modality": sa.Column(
            "modality", sa.String(32), nullable=False, server_default="text"
        ),
        "protocol": sa.Column(
            "protocol", sa.String(64), nullable=False, server_default="openai_compatible"
        ),
        "health_status": sa.Column(
            "health_status", sa.String(32), nullable=False, server_default="unknown"
        ),
        "health_detail": sa.Column(
            "health_detail", sa.Text(), nullable=False, server_default=""
        ),
        "version": sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        "secret_version": sa.Column(
            "secret_version", sa.Integer(), nullable=False, server_default="0"
        ),
        "created_at": sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        "updated_at": sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    }
    missing = [column for name, column in additions.items() if name not in existing_columns]
    if missing:
        with op.batch_alter_table("providers") as batch:
            for column in missing:
                batch.add_column(column)

    inspector = sa.inspect(bind)
    provider_indexes = {index["name"] for index in inspector.get_indexes("providers")}
    if "ix_providers_modality" not in provider_indexes:
        op.create_index("ix_providers_modality", "providers", ["modality"])
    if "ix_providers_health_status" not in provider_indexes:
        op.create_index("ix_providers_health_status", "providers", ["health_status"])

    if "active_provider_bindings" not in inspector.get_table_names():
        op.create_table(
            "active_provider_bindings",
            sa.Column("id", sa.String(160), primary_key=True),
            sa.Column("scope", sa.String(32), nullable=False),
            sa.Column("scope_id", sa.String(64), nullable=True),
            sa.Column("modality", sa.String(32), nullable=False),
            sa.Column(
                "provider_id", sa.String(64), sa.ForeignKey("providers.id"), nullable=False
            ),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )
    binding_indexes = {
        index["name"] for index in sa.inspect(bind).get_indexes("active_provider_bindings")
    }
    for name, columns in (
        ("ix_active_provider_bindings_scope", ["scope"]),
        ("ix_active_provider_bindings_scope_id", ["scope_id"]),
        ("ix_active_provider_bindings_modality", ["modality"]),
        ("ix_active_provider_bindings_provider_id", ["provider_id"]),
    ):
        if name not in binding_indexes:
            op.create_index(name, "active_provider_bindings", columns)


def downgrade() -> None:
    return
