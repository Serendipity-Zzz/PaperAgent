from __future__ import annotations

import sqlite3
from threading import RLock
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver

from paperagent.agents.state import (
    AgentState,
    AgentStateCheckpoint,
    GraphInterrupt,
    GraphRunStatus,
)
from paperagent.db.manager import DatabaseManager
from paperagent.services.progress import DurableProgressSink
from paperagent.services.tasks import TaskService


class AgentEventBridge:
    """Write checkpoint lifecycle metadata to the project event stream."""

    def __init__(self, databases: DatabaseManager) -> None:
        self.sink = DurableProgressSink(TaskService(databases))

    def emit(
        self,
        *,
        project_id: str,
        task_id: str,
        thread_id: str,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        safe_payload: dict[str, object] = {
            "thread_id": thread_id,
            "sequence": payload.get("sequence"),
            "checkpoint_id": payload.get("checkpoint_id"),
            "node_id": payload.get("node_id"),
            "status": payload.get("status"),
        }
        self.sink.emit(
            project_id=project_id,
            run_id=task_id,
            event_type=event_type,
            payload=safe_payload,
        )


class AgentCheckpointService:
    """Project-scoped durable state plus the official LangGraph SQLite saver."""

    def __init__(self, databases: DatabaseManager, project_id: str) -> None:
        self.databases = databases
        self.project_id = project_id
        self.project_root = databases.project_root(project_id).resolve()
        self.path = self.project_root / "agent-checkpoints.db"
        self.connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS paperagent_state_checkpoints(
                checkpoint_id TEXT PRIMARY KEY,
                thread_key TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(thread_key, sequence)
            );
            CREATE INDEX IF NOT EXISTS ix_agent_checkpoint_thread
              ON paperagent_state_checkpoints(thread_key, sequence DESC);
            CREATE TABLE IF NOT EXISTS paperagent_interrupt_decisions(
                interrupt_id TEXT PRIMARY KEY,
                thread_key TEXT NOT NULL,
                approved INTEGER NOT NULL,
                checkpoint_id TEXT NOT NULL
            );
            """
        )
        self.saver = SqliteSaver(self.connection)
        self.saver.setup()
        self.lock = RLock()
        self.events = AgentEventBridge(databases)

    def close(self) -> None:
        self.connection.close()

    def _thread_key(self, thread_id: str) -> str:
        if not thread_id or ":" in thread_id:
            raise ValueError("thread id must be non-empty and cannot contain ':'")
        return f"{self.project_id}:{thread_id}"

    def config(self, thread_id: str, checkpoint_id: str | None = None) -> RunnableConfig:
        configurable = {"thread_id": self._thread_key(thread_id)}
        if checkpoint_id is not None:
            configurable["checkpoint_id"] = checkpoint_id
        return {"configurable": configurable}

    def checkpoint(
        self, state: AgentState, *, event_type: str = "agent.checkpoint.saved"
    ) -> AgentStateCheckpoint:
        if state.project_id != self.project_id:
            raise ValueError("agent state belongs to another project")
        envelope = AgentStateCheckpoint(state=state)
        thread_key = self._thread_key(state.thread_id)
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO paperagent_state_checkpoints(
                    checkpoint_id,thread_key,thread_id,task_id,sequence,event_type,
                    payload_json,created_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    str(envelope.checkpoint_id),
                    thread_key,
                    state.thread_id,
                    state.task_id,
                    state.sequence,
                    event_type,
                    envelope.model_dump_json(),
                    envelope.created_at.isoformat(),
                ),
            )
            row = self.connection.execute(
                """
                SELECT payload_json FROM paperagent_state_checkpoints
                WHERE thread_key=? AND sequence=?
                """,
                (thread_key, state.sequence),
            ).fetchone()
        saved = AgentStateCheckpoint.model_validate_json(row[0])
        self.events.emit(
            project_id=self.project_id,
            task_id=state.task_id,
            thread_id=state.thread_id,
            event_type=event_type,
            payload={
                "sequence": saved.state.sequence,
                "checkpoint_id": str(saved.checkpoint_id),
                "node_id": saved.state.active_node,
                "status": saved.state.status,
            },
        )
        return saved

    def latest(self, thread_id: str) -> AgentStateCheckpoint:
        row = self.connection.execute(
            """
            SELECT payload_json FROM paperagent_state_checkpoints
            WHERE thread_key=? ORDER BY sequence DESC LIMIT 1
            """,
            (self._thread_key(thread_id),),
        ).fetchone()
        if row is None:
            raise KeyError("agent checkpoint not found")
        return AgentStateCheckpoint.model_validate_json(row[0])

    def history(self, thread_id: str) -> list[AgentStateCheckpoint]:
        rows = self.connection.execute(
            """
            SELECT payload_json FROM paperagent_state_checkpoints
            WHERE thread_key=? ORDER BY sequence
            """,
            (self._thread_key(thread_id),),
        ).fetchall()
        return [AgentStateCheckpoint.model_validate_json(row[0]) for row in rows]

    def before_node(
        self,
        state: AgentState,
        *,
        node_id: str,
        execution_key: str,
        node_input: dict[str, object],
    ) -> tuple[AgentStateCheckpoint, bool]:
        started = state.begin_node(node_id, execution_key, node_input)
        event_type = "agent.node.started" if started else "agent.node.duplicate_skipped"
        return self.checkpoint(state, event_type=event_type), started

    def after_node(
        self, state: AgentState, *, node_id: str, output: dict[str, object]
    ) -> AgentStateCheckpoint:
        state.complete_node(node_id, output)
        return self.checkpoint(state, event_type="agent.node.completed")

    def pause(self, state: AgentState, interrupt: GraphInterrupt) -> AgentStateCheckpoint:
        state.pause(interrupt)
        return self.checkpoint(state, event_type="agent.interrupted")

    def resume(self, thread_id: str, interrupt_id: UUID, *, approved: bool) -> AgentState:
        thread_key = self._thread_key(thread_id)
        existing = self.connection.execute(
            """
            SELECT approved FROM paperagent_interrupt_decisions
            WHERE interrupt_id=? AND thread_key=?
            """,
            (str(interrupt_id), thread_key),
        ).fetchone()
        if existing is not None:
            if bool(existing[0]) != approved:
                raise ValueError("interrupt already has a different decision")
            return self.latest(thread_id).state
        state = self.latest(thread_id).state
        state.resume(interrupt_id, approved=approved)
        saved = self.checkpoint(state, event_type="agent.resumed" if approved else "agent.rejected")
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO paperagent_interrupt_decisions(
                    interrupt_id,thread_key,approved,checkpoint_id
                ) VALUES (?,?,?,?)
                """,
                (str(interrupt_id), thread_key, int(approved), str(saved.checkpoint_id)),
            )
        return saved.state

    def cancel(self, thread_id: str) -> AgentState:
        state = self.latest(thread_id).state
        if state.status is GraphRunStatus.CANCELLED:
            return state
        state.status = GraphRunStatus.CANCELLED
        state.sequence += 1
        return self.checkpoint(state, event_type="agent.cancelled").state
