"""Backfill global provider bindings for configurations created before modality bindings."""

import sqlalchemy as sa
from alembic import context, op

revision = "0009_provider_binding_backfill"
down_revision = "0008_provider_domains"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.get_x_argument(as_dictionary=True).get("kind", "global") != "global":
        return
    bind = op.get_bind()
    metadata = sa.MetaData()
    providers = sa.Table("providers", metadata, autoload_with=bind)
    bindings = sa.Table("active_provider_bindings", metadata, autoload_with=bind)
    existing_modalities = set(bind.execute(sa.select(bindings.c.modality)).scalars())
    rows = bind.execute(
        sa.select(providers.c.id, providers.c.modality)
        .where(providers.c.enabled.is_(True))
        .order_by(providers.c.id)
    )
    for provider_id, modality in rows:
        if modality in existing_modalities:
            continue
        bind.execute(
            bindings.insert().values(
                id=f"global:*:{modality}",
                scope="global",
                scope_id=None,
                modality=modality,
                provider_id=provider_id,
                version=1,
            )
        )
        existing_modalities.add(modality)


def downgrade() -> None:
    return
