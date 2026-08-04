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
RUNTIME_SOURCE = PROJECT_ROOT / "src" / "job_application_automation" / "resources" / "runtime"
TRACKED_RUNTIME_SOURCE = PROJECT_ROOT / "config" / "runtime"
WHEEL_RUNTIME_PREFIX = "job_application_automation/resources/runtime/"


def _single_wheel(directory: Path) -> Path:
    wheels = sorted(directory.glob("*.whl"))
    assert len(wheels) == 1, f"expected one wheel in {directory}, found {wheels}"
    return wheels[0]


def _build_wheel(output_dir: Path) -> Path:
    source_dir = output_dir.parent / "source"
    shutil.copytree(PROJECT_ROOT / "src", source_dir / "src")
    shutil.copytree(PROJECT_ROOT / "config" / "runtime", source_dir / "config" / "runtime")
    for filename in ("pyproject.toml", "README.md"):
        shutil.copy2(PROJECT_ROOT / filename, source_dir / filename)

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
            "--no-build-isolation",
            "--wheel-dir",
            str(output_dir),
            str(source_dir),
        ]

    result = subprocess.run(
        command,
        cwd=source_dir,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return _single_wheel(output_dir)


def test_wheel_contains_every_dashboard_and_runtime_asset(tmp_path: Path) -> None:
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
    source_runtime = {path.name: path.read_bytes() for path in RUNTIME_SOURCE.glob("*.json")}
    tracked_runtime = {
        path.name: path.read_bytes() for path in TRACKED_RUNTIME_SOURCE.glob("*.json")
    }
    assert source_runtime
    assert source_runtime == tracked_runtime

    with ZipFile(wheel) as archive:
        packaged_assets = {
            name for name in archive.namelist() if name.startswith(WHEEL_STATIC_PREFIX)
        }
        packaged_runtime = {
            name.removeprefix(WHEEL_RUNTIME_PREFIX): archive.read(name)
            for name in archive.namelist()
            if name.startswith(WHEEL_RUNTIME_PREFIX) and name.endswith(".json")
        }

    assert packaged_assets == source_assets
    assert f"{WHEEL_STATIC_PREFIX}index.html" in packaged_assets
    assert packaged_runtime == source_runtime
    assert {"continuous_worker.json", "observability.json"}.issubset(packaged_runtime)
