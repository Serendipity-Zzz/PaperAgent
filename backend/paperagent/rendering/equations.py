from __future__ import annotations

import re

from pydantic import BaseModel

from paperagent.agents.document_ir import EquationSpec


class ResolvedEquation(BaseModel):
    latex: str
    mathml: str | None
    display: bool
    number: int | None
    label: str | None


class EquationService:
    UNSAFE = re.compile(
        r"\\(?:input|include|write|openout|read|usepackage|documentclass|csname)\b",
        re.I,
    )

    def resolve(self, equations: list[EquationSpec]) -> list[ResolvedEquation]:
        sequence = 0
        resolved: list[ResolvedEquation] = []
        labels: set[str] = set()
        for equation in equations:
            latex = self.normalize(equation.latex)
            if equation.label:
                if equation.label in labels:
                    raise ValueError(f"duplicate equation label: {equation.label}")
                labels.add(equation.label)
            number = None
            if equation.number:
                sequence += 1
                number = sequence
            resolved.append(
                ResolvedEquation(
                    latex=latex,
                    mathml=equation.mathml,
                    display=equation.display,
                    number=number,
                    label=equation.label,
                )
            )
        return resolved

    def normalize(self, latex: str) -> str:
        value = latex.strip()
        if value.startswith("$$") and value.endswith("$$"):
            value = value[2:-2].strip()
        elif value.startswith("$") and value.endswith("$"):
            value = value[1:-1].strip()
        if not value:
            raise ValueError("equation source is empty")
        if self.UNSAFE.search(value):
            raise ValueError("equation contains an unsafe TeX command")
        return re.sub(r"[ \t]+", " ", value)
