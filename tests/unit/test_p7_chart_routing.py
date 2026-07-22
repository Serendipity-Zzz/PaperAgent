from pathlib import Path
from uuid import uuid4

import pytest

from paperagent.agents.state import RawRequest, RequirementSpec
from paperagent.experiments.runtime import CapabilityReport
from paperagent.visuals.service import ChartRenderer, ChartSpec, VisualAgentRouter


def requirement() -> RequirementSpec:
    return RequirementSpec.model_validate(
        {
            "raw_request": RawRequest(text="实验、数据图、流程图和示意图"),
            "normalized_request": "生成实验报告和视觉材料",
            "research_formulation": {"research_topic": "agent"},
            "document_type": "experiment_report",
            "primary_language": "zh",
            "target_length": {"value": 1000, "unit": "chinese_char"},
            "audience": "researcher",
            "citation_style": "APA",
            "requires_literature_search": False,
            "requires_experiment": True,
            "requires_data_chart": True,
            "requires_generated_image": True,
            "output_formats": ["pdf"],
            "constraints": ["需要流程图"],
        }
    ).confirm()


def test_chart_provenance_units_error_bars_simulation_and_visual_routes(tmp_path: Path) -> None:
    spec = ChartSpec(
        title="Accuracy",
        chart_type="line",
        x=[1, 2, 3],
        y=[0.7, 0.8, 0.9],
        y_error=[0.01, 0.02, 0.01],
        x_label="Epoch",
        y_label="Accuracy",
        unit="%",
        simulated_data=True,
        data_file="metrics.csv",
        run_id=uuid4(),
    )
    artifact = ChartRenderer().render(spec, tmp_path / "chart.png")
    assert Path(artifact.path).is_file()
    assert artifact.provenance.source_type == "simulated_chart"
    assert not artifact.provenance.real_experiment_evidence
    with pytest.raises(ValueError, match="unit"):
        ChartRenderer().render(spec.model_copy(update={"unit": ""}), tmp_path / "bad.png")
    route = VisualAgentRouter().route(requirement())
    assert (
        route.experiment
        and route.data_chart
        and route.deterministic_diagram
        and route.generated_image
    )
    impossible = CapabilityReport(
        verdict="unreasonable",
        reasons=["No GPU"],
        ram_gb=16,
        disk_free_gb=100,
    )
    assert not VisualAgentRouter().route(requirement(), impossible).experiment
    assert not VisualAgentRouter().route(requirement(), user_rejected_experiment=True).experiment
