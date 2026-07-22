from paperagent.presentation.requirements import (
    PresentationResolution,
    PresentationResolver,
    enrich_requirement_presentation,
    extract_explicit_presentation,
    presentation_confirmation_summary,
)

__all__ = [
    "PresentationResolution",
    "PresentationResolver",
    "apply_presentation_patch",
    "default_page_chrome",
    "enrich_requirement_presentation",
    "expectation_from_presentation",
    "extract_explicit_presentation",
    "presentation_confirmation_summary",
    "presentation_from_requirement",
]
from paperagent.presentation.canonical import (
    apply_presentation_patch,
    default_page_chrome,
    expectation_from_presentation,
    presentation_from_requirement,
)
