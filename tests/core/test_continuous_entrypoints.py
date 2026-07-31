from __future__ import annotations

from unittest.mock import patch

from job_application_automation.core.continuous_ashby import main as ashby_main
from job_application_automation.core.continuous_greenhouse import main as greenhouse_main
from job_application_automation.core.continuous_lever import main as lever_main


def test_continuous_ashby_entrypoint() -> None:
    with patch(
        "job_application_automation.core.continuous_ashby._continuous_main", return_value=0
    ) as mock_main:
        res = ashby_main(["--interval-seconds", "60"])
        assert res == 0
        mock_main.assert_called_once_with(["--interval-seconds", "60"], ats_platform="ashby")


def test_continuous_greenhouse_entrypoint() -> None:
    with patch(
        "job_application_automation.core.continuous_greenhouse._continuous_main", return_value=0
    ) as mock_main:
        res = greenhouse_main(["--interval-seconds", "60"])
        assert res == 0
        mock_main.assert_called_once_with(["--interval-seconds", "60"], ats_platform="greenhouse")


def test_continuous_lever_entrypoint() -> None:
    with patch(
        "job_application_automation.core.continuous_lever.run_continuous_worker", return_value=0
    ) as mock_main:
        res = lever_main(["--interval-seconds", "60"])
        assert res == 0
        mock_main.assert_called_once_with(["--interval-seconds", "60"], ats_platform="lever")
