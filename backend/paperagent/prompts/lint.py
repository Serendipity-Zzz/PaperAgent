from __future__ import annotations

import re

from pydantic import Field

from paperagent.prompts.models import PromptModule
from paperagent.schemas.common import StrictModel


class PromptLintIssue(StrictModel):
    code: str
    module_id: str
    message: str
    line: int | None = Field(default=None, ge=1)


def lint_modules(modules: list[PromptModule]) -> list[PromptLintIssue]:
    issues: list[PromptLintIssue] = []
    rule_owners: dict[str, str] = {}
    for module in modules:
        for line_number, raw in enumerate(module.content.splitlines(), start=1):
            line = re.sub(r"\s+", " ", raw.strip()).casefold()
            if not line:
                continue
            owner = rule_owners.get(line)
            if owner and owner != module.module_id:
                issues.append(
                    PromptLintIssue(
                        code="DUPLICATE_RULE",
                        module_id=module.module_id,
                        line=line_number,
                        message=f"same rule already exists in {owner}",
                    )
                )
            rule_owners[line] = module.module_id
            if re.search(r"\b20\d{2}-\d{2}-\d{2}\b", line):
                issues.append(
                    PromptLintIssue(
                        code="FIXED_DATE",
                        module_id=module.module_id,
                        line=line_number,
                        message="runtime dates must not be frozen in a prompt module",
                    )
                )
            if '"properties"' in line or '"input_schema"' in line:
                issues.append(
                    PromptLintIssue(
                        code="TOOL_SCHEMA_LEAK",
                        module_id=module.module_id,
                        line=line_number,
                        message="tool schemas must be supplied through the provider API",
                    )
                )
    return issues
