"""Internal, side-effect-free implementation package for job automation.

Public compatibility entrypoints remain in ``src/*.py`` so existing commands
and imports continue to work while reusable workflows live behind typed
contracts here.
"""

from .contracts import EngineMode, EngineRequest, EngineResult, EngineStatus

__all__ = ("EngineMode", "EngineRequest", "EngineResult", "EngineStatus")
