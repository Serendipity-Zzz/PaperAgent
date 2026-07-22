from __future__ import annotations

import json
from collections.abc import Mapping
from uuid import uuid4

from sqlalchemy import select

from paperagent.db.manager import DatabaseManager
from paperagent.db.models import DocumentDeliveryRecord
from paperagent.rendering.delivery import DeliveryStatus


class DeliveryTransitionError(RuntimeError):
    pass


_ALLOWED_TRANSITIONS: Mapping[DeliveryStatus, frozenset[DeliveryStatus]] = {
    DeliveryStatus.PLANNED: frozenset(
        {DeliveryStatus.RENDERING, DeliveryStatus.REPAIR_REQUIRED, DeliveryStatus.REJECTED}
    ),
    DeliveryStatus.RENDERING: frozenset(
        {DeliveryStatus.VALIDATING, DeliveryStatus.REPAIR_REQUIRED, DeliveryStatus.REJECTED}
    ),
    DeliveryStatus.VALIDATING: frozenset(
        {DeliveryStatus.DELIVERED, DeliveryStatus.REPAIR_REQUIRED, DeliveryStatus.REJECTED}
    ),
    DeliveryStatus.REPAIR_REQUIRED: frozenset(
        {DeliveryStatus.RENDERING, DeliveryStatus.REJECTED}
    ),
    DeliveryStatus.DELIVERED: frozenset(),
    DeliveryStatus.REJECTED: frozenset(),
}


class DocumentDeliveryStore:
    def __init__(self, databases: DatabaseManager, project_id: str) -> None:
        self.databases = databases
        self.project_id = project_id

    def create(
        self,
        *,
        revision_id: str,
        format_name: str,
        renderer: str,
        renderer_version: str,
        options_hash: str,
        figure_artifact_ids: list[str],
        source_run_id: str | None,
        source_message_id: str | None = None,
    ) -> DocumentDeliveryRecord:
        with self.databases.project_session(self.project_id) as session:
            existing = session.scalar(
                select(DocumentDeliveryRecord).where(
                    DocumentDeliveryRecord.revision_id == revision_id,
                    DocumentDeliveryRecord.format == format_name,
                    DocumentDeliveryRecord.options_hash == options_hash,
                )
            )
            if existing is not None:
                session.expunge(existing)
                return existing
            record = DocumentDeliveryRecord(
                id=str(uuid4()),
                revision_id=revision_id,
                format=format_name,
                renderer=renderer,
                renderer_version=renderer_version,
                options_hash=options_hash,
                status=DeliveryStatus.PLANNED.value,
                figure_artifact_ids_json=json.dumps(figure_artifact_ids),
                source_run_id=source_run_id,
                source_message_id=source_message_id,
            )
            session.add(record)
            session.commit()
            session.refresh(record)
            session.expunge(record)
            return record

    def transition(
        self,
        delivery_id: str,
        target: DeliveryStatus,
        *,
        expected_version: int,
        artifact_id: str | None = None,
        validation_report: dict[str, object] | None = None,
    ) -> DocumentDeliveryRecord:
        with self.databases.project_session(self.project_id) as session:
            record = session.get(DocumentDeliveryRecord, delivery_id)
            if record is None:
                raise KeyError(delivery_id)
            current = DeliveryStatus(record.status)
            if record.version != expected_version:
                raise DeliveryTransitionError("delivery optimistic lock conflict")
            if target not in _ALLOWED_TRANSITIONS[current]:
                raise DeliveryTransitionError(
                    f"illegal delivery transition: {current.value} -> {target.value}"
                )
            record.status = target.value
            record.version += 1
            if artifact_id is not None:
                record.artifact_id = artifact_id
            if validation_report is not None:
                record.validation_report_json = json.dumps(
                    validation_report, ensure_ascii=False, sort_keys=True
                )
            session.commit()
            session.refresh(record)
            session.expunge(record)
            return record
