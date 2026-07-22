from paperagent.api.app import CURRENT_INTERACTIVE_TOOL_NAMES
from paperagent.execution.tool_suite import ExecutionToolSuite
from paperagent.orchestration import compile_dynamic_interactive_graph


def test_default_interactive_chain_uses_dynamic_graph_with_current_capabilities() -> None:

    assert set(CURRENT_INTERACTIVE_TOOL_NAMES) == {
        "knowledge.search",
        "file.read",
        "artifact.lookup",
        *ExecutionToolSuite.TOOL_NAMES,
    }
    assert "process.execute" in CURRENT_INTERACTIVE_TOOL_NAMES
    assert "document.render" in CURRENT_INTERACTIVE_TOOL_NAMES
    assert callable(compile_dynamic_interactive_graph)


def test_presentation_resolver_is_allowed_for_requirement_plan_node() -> None:
    resolver = next(
        spec
        for spec in ExecutionToolSuite.specs()
        if spec.name == "document.presentation.resolve"
    )

    assert "requirement_agent" in resolver.allowed_agents
