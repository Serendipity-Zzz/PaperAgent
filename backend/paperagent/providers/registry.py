from __future__ import annotations

import builtins
import json
from datetime import UTC, datetime

from sqlalchemy import select

from paperagent.db.manager import DatabaseManager
from paperagent.db.models import ActiveProviderBindingRecord, AppSetting, ProviderRecord
from paperagent.providers.base import ProviderConfig, ProviderHealth, ProviderModality


class ProviderRegistry:
    def __init__(self, databases: DatabaseManager) -> None:
        self.databases = databases

    @staticmethod
    def _config(row: ProviderRecord) -> ProviderConfig:
        return ProviderConfig.model_validate(
            {
                "id": row.id,
                "display_name": row.display_name or row.id,
                "modality": row.modality,
                "protocol": row.protocol,
                "provider_type": row.provider_type,
                "base_url": row.base_url,
                "model": row.model,
                "credential_ref": row.credential_ref,
                "capabilities": json.loads(row.capabilities_json),
                "extra": json.loads(row.extra_json),
                "enabled": row.enabled,
                "health_status": row.health_status,
                "health_detail": row.health_detail,
                "version": row.version,
                "secret_version": row.secret_version,
            }
        )

    def save(
        self, config: ProviderConfig, *, expected_version: int | None = None
    ) -> ProviderConfig:
        with self.databases.global_session() as session:
            row = session.get(ProviderRecord, config.id)
            if row is not None and expected_version is not None and row.version != expected_version:
                raise ValueError(
                    f"provider version changed: expected {expected_version}, found {row.version}"
                )
            creating = row is None
            row = row or ProviderRecord(id=config.id)
            row.display_name = config.display_name or config.id
            row.modality = config.modality.value
            row.protocol = config.protocol
            row.provider_type = config.provider_type
            row.base_url = str(config.base_url)
            row.model = config.model
            row.credential_ref = config.credential_ref
            row.capabilities_json = json.dumps(sorted(item.value for item in config.capabilities))
            row.extra_json = json.dumps(config.extra)
            row.enabled = config.enabled
            row.health_status = config.health_status.value
            row.health_detail = config.health_detail
            row.secret_version = config.secret_version
            row.version = 1 if creating else row.version + 1
            row.updated_at = datetime.now(UTC)
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._config(row)

    def list(
        self,
        *,
        include_disabled: bool = False,
        modality: ProviderModality | str | None = None,
    ) -> list[ProviderConfig]:
        with self.databases.global_session() as session:
            statement = select(ProviderRecord)
            if not include_disabled:
                statement = statement.where(ProviderRecord.enabled.is_(True))
            if modality is not None:
                value = modality.value if isinstance(modality, ProviderModality) else modality
                statement = statement.where(ProviderRecord.modality == value)
            rows = session.scalars(statement.order_by(ProviderRecord.created_at, ProviderRecord.id))
            return [self._config(row) for row in rows]

    def get(self, provider_id: str, *, include_disabled: bool = False) -> ProviderConfig | None:
        with self.databases.global_session() as session:
            row = session.get(ProviderRecord, provider_id)
            if row is None or (not include_disabled and not row.enabled):
                return None
            return self._config(row)

    def deactivate(
        self, provider_id: str, *, expected_version: int | None = None
    ) -> ProviderConfig:
        with self.databases.global_session() as session:
            row = session.get(ProviderRecord, provider_id)
            if row is None:
                raise KeyError(provider_id)
            if expected_version is not None and row.version != expected_version:
                raise ValueError("provider version changed")
            active = session.scalar(
                select(ActiveProviderBindingRecord).where(
                    ActiveProviderBindingRecord.provider_id == provider_id
                )
            )
            if active is not None:
                raise ValueError("active provider must be switched before it can be disabled")
            row.enabled = False
            row.version += 1
            row.updated_at = datetime.now(UTC)
            session.commit()
            session.refresh(row)
            return self._config(row)

    def bind(
        self,
        provider_id: str,
        *,
        scope: str = "global",
        scope_id: str | None = None,
        expected_version: int | None = None,
    ) -> ActiveProviderBindingRecord:
        config = self.get(provider_id)
        if config is None:
            raise KeyError(provider_id)
        if scope not in {"global", "project"} or (scope == "project") != bool(scope_id):
            raise ValueError("invalid provider binding scope")
        binding_id = f"{scope}:{scope_id or '*'}:{config.modality.value}"
        with self.databases.global_session() as session:
            row = session.get(ActiveProviderBindingRecord, binding_id)
            if row is not None and expected_version is not None and row.version != expected_version:
                raise ValueError("provider binding version changed")
            row = row or ActiveProviderBindingRecord(
                id=binding_id,
                scope=scope,
                scope_id=scope_id,
                modality=config.modality.value,
                provider_id=provider_id,
            )
            row.provider_id = provider_id
            row.version = row.version + 1 if row.id == binding_id and row.version else 1
            row.updated_at = datetime.now(UTC)
            session.add(row)
            session.commit()
            session.refresh(row)
            session.expunge(row)
            return row

    def bindings(self) -> builtins.list[ActiveProviderBindingRecord]:
        with self.databases.global_session() as session:
            rows = list(session.scalars(select(ActiveProviderBindingRecord)))
            for row in rows:
                session.expunge(row)
            return rows

    def active(
        self, modality: ProviderModality, *, project_id: str | None = None
    ) -> ProviderConfig | None:
        bindings = self.bindings()
        selected = next(
            (
                row
                for row in bindings
                if project_id
                and row.scope == "project"
                and row.scope_id == project_id
                and row.modality == modality.value
            ),
            None,
        ) or next(
            (row for row in bindings if row.scope == "global" and row.modality == modality.value),
            None,
        )
        if selected is not None:
            return self.get(selected.provider_id)
        available = self.list(modality=modality)
        return available[0] if available else None

    def record_health(
        self, provider_id: str, health: ProviderHealth, detail: str = ""
    ) -> ProviderConfig:
        with self.databases.global_session() as session:
            row = session.get(ProviderRecord, provider_id)
            if row is None:
                raise KeyError(provider_id)
            row.health_status = health.value
            row.health_detail = detail[:2000]
            row.updated_at = datetime.now(UTC)
            session.commit()
            session.refresh(row)
            return self._config(row)

    def set_setting(self, key: str, value: object) -> None:
        with self.databases.global_session() as session:
            row = session.get(AppSetting, key) or AppSetting(key=key, value_json="null")
            row.value_json = json.dumps(value, ensure_ascii=False)
            session.add(row)
            session.commit()

    def get_setting(self, key: str, default: object = None) -> object:
        with self.databases.global_session() as session:
            row = session.get(AppSetting, key)
            return json.loads(row.value_json) if row else default
