"""Conversation lifecycle contracts and the reusable agent execution kernel."""

from paperagent.engine.agent_graph import AgentLoopGraphState, compile_agent_loop_graph
from paperagent.engine.agent_loop import (
    AgentLoop,
    AgentLoopLimitError,
    AgentLoopRequest,
    AgentLoopResult,
)
from paperagent.engine.budgets import BudgetDecision, BudgetLimits, BudgetUsage
from paperagent.engine.conversation import ConversationEngine
from paperagent.engine.events import (
    EngineEvent,
    EngineEventKind,
    TurnRequest,
    ensure_event_sequence,
)
from paperagent.engine.lifecycle import (
    CancelRequest,
    EngineSignal,
    GraphLifecycle,
    LangGraphLifecycleAdapter,
    ResumeRequest,
)
from paperagent.engine.persistence import ConversationPersistence, SqliteConversationPersistence

__all__ = [
    "AgentLoop",
    "AgentLoopGraphState",
    "AgentLoopLimitError",
    "AgentLoopRequest",
    "AgentLoopResult",
    "BudgetDecision",
    "BudgetLimits",
    "BudgetUsage",
    "CancelRequest",
    "ConversationEngine",
    "ConversationPersistence",
    "EngineEvent",
    "EngineEventKind",
    "EngineSignal",
    "GraphLifecycle",
    "LangGraphLifecycleAdapter",
    "ResumeRequest",
    "SqliteConversationPersistence",
    "TurnRequest",
    "compile_agent_loop_graph",
    "ensure_event_sequence",
]
