"""Side-effect-free implementation package for job automation workflows.

The source tree exposes one launcher, ``src/job_automation.py``. Reusable
workflows and typed contracts live in this package.
"""

from .contracts import EngineMode, EngineRequest, EngineResult, EngineStatus

__all__ = ("EngineMode", "EngineRequest", "EngineResult", "EngineStatus")
