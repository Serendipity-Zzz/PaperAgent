# Orchestration

This package owns executable workflow concerns only:

- `failure.py`: failure normalization and materially different recovery strategies.
- `runtime.py`: compilation from the serializable `TaskGraph` contract to LangGraph.

Domain agents remain in `paperagent.agents`; deterministic services remain in their capability
packages. Provider HTTP retries are limited to transient failures. Semantic failures are persisted
in graph state and routed through the recovery planner instead of blindly repeating the same call.
