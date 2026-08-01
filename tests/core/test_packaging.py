from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile


PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATIC_SOURCE = PROJECT_ROOT / "src" / "job_application_automation" / "dashboard" / "static"
WHEEL_STATIC_PREFIX = "job_application_automation/dashboard/static/"


def _single_wheel(directory: Path) -> Path:
    wheels = sorted(directory.glob("*.whl"))
    assert len(wheels) == 1, f"expected one wheel in {directory}, found {wheels}"
    return wheels[0]


def _build_wheel(output_dir: Path) -> Path:
    uv = shutil.which("uv")
    if uv:
        command = [uv, "build", "--wheel", "--out-dir", str(output_dir)]
    else:
        command = [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(output_dir),
            str(PROJECT_ROOT),
        ]

    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return _single_wheel(output_dir)


def test_wheel_contains_every_dashboard_static_asset(tmp_path: Path) -> None:
    configured_dir = os.environ.get("JOB_AUTOMATION_WHEEL_DIR")
    wheel = (
        _single_wheel(Path(configured_dir)) if configured_dir else _build_wheel(tmp_path / "wheel")
    )

    source_assets = {
        f"{WHEEL_STATIC_PREFIX}{path.relative_to(STATIC_SOURCE).as_posix()}"
        for path in STATIC_SOURCE.rglob("*")
        if path.is_file()
    }
    assert source_assets

    with ZipFile(wheel) as archive:
        packaged_assets = {
            name for name in archive.namelist() if name.startswith(WHEEL_STATIC_PREFIX)
        }

    assert packaged_assets == source_assets
    assert f"{WHEEL_STATIC_PREFIX}index.html" in packaged_assets
