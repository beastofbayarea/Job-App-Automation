"""Injectable boundary for the PDF rendering step of resume generation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from collections.abc import Callable, Mapping


@dataclass(frozen=True, slots=True)
class ResumeRenderRequest:
    """Inputs required to render one resume artifact."""

    resume: Mapping[str, Any]
    output_path: Path
    bold_keywords: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.resume, Mapping):
            raise ValueError("resume must be a mapping")
        object.__setattr__(self, "output_path", Path(self.output_path))
        if self.bold_keywords is not None:
            object.__setattr__(self, "bold_keywords", frozenset(self.bold_keywords))


@runtime_checkable
class ResumeRenderer(Protocol):
    """Renders an isolated request without depending on orchestration state."""

    def render(self, request: ResumeRenderRequest) -> bool:
        """Render one resume and return whether the artifact was written."""


class CallableResumeRenderer:
    """Adapter for the established ReportLab rendering function."""

    def __init__(
        self,
        callback: Callable[[Mapping[str, Any], Path, set[str] | None], bool],
    ) -> None:
        self._callback = callback

    def render(self, request: ResumeRenderRequest) -> bool:
        keywords = set(request.bold_keywords) if request.bold_keywords is not None else None
        return bool(self._callback(request.resume, request.output_path, keywords))


def render_resume(
    renderer: ResumeRenderer,
    resume: Mapping[str, Any],
    output_path: Path,
    bold_keywords: set[str] | None = None,
) -> bool:
    """Create a validated render request and delegate it to a renderer port."""
    return bool(
        renderer.render(
            ResumeRenderRequest(
                resume=resume,
                output_path=output_path,
                bold_keywords=frozenset(bold_keywords) if bold_keywords is not None else None,
            )
        )
    )
