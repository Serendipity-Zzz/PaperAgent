from paperagent.prompts import (
    BUILTIN_PROMPT_MODULES,
    PromptModule,
    PromptModuleRegistry,
    PromptSelectionContext,
    default_prompt_compiler,
    lint_modules,
)
from paperagent.providers import ChatMessage


def test_prompt_compiler_selects_versioned_modules_in_stable_order() -> None:
    compiler = default_prompt_compiler()
    context = PromptSelectionContext(
        agent_type="writer_agent",
        task="write_section",
        language="zh",
        features={"typography"},
    )
    compiled = compiler.compile(context, [ChatMessage(role="user", content="section")])
    assert compiled.module_versions == [
        "core/identity@1.0.0",
        "core/truthfulness@1.0.0",
        "writing/section@1.0.0",
        "evidence/citation@1.0.0",
        "rendering/typography@1.0.0",
    ]
    assert compiled.messages[-1].role == "user"
    assert len(compiled.prompt_hash) == 64
    assert compiled == compiler.compile(
        context, [ChatMessage(role="user", content="section")]
    )


def test_registry_selects_latest_applicable_version() -> None:
    registry = PromptModuleRegistry()
    for version in ("1.0.0", "1.1.0"):
        registry.register(
            PromptModule(
                module_id="test/module",
                version=version,
                priority=100,
                content=version,
                agent_types={"writer_agent"},
            )
        )
    selected = registry.select(
        PromptSelectionContext(agent_type="writer_agent", task="test")
    )
    assert [module.version for module in selected] == ["1.1.0"]


def test_prompt_lint_detects_frozen_date_schema_and_duplicates() -> None:
    modules = [
        PromptModule(
            module_id="test/one",
            version="1.0.0",
            priority=1,
            content='Same rule\nToday is 2026-07-17\n{"input_schema": {"properties": {}}}',
        ),
        PromptModule(
            module_id="test/two",
            version="1.0.0",
            priority=2,
            content="Same rule",
        ),
    ]
    assert {issue.code for issue in lint_modules(modules)} == {
        "DUPLICATE_RULE",
        "FIXED_DATE",
        "TOOL_SCHEMA_LEAK",
    }
    assert lint_modules(list(BUILTIN_PROMPT_MODULES)) == []
