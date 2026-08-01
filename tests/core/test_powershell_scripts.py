from __future__ import annotations

from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"


def test_powershell_scripts_exist_and_utf8() -> None:
    ps1_files = list(SCRIPTS_DIR.glob("*.ps1"))
    assert len(ps1_files) >= 10

    for script in ps1_files:
        content = script.read_text(encoding="utf-8")
        assert len(content) > 0


def test_installer_script_safety_guards() -> None:
    installer = (SCRIPTS_DIR / "install_vps_continuous_ats.ps1").read_text(encoding="utf-8")
    assert "param" in installer.lower()
    assert "-atsplatform" in installer.lower() or "atsplatform" in installer.lower()
    assert "exit 76" in installer.lower()


def test_service_templates_exist() -> None:
    templates = list((SCRIPTS_DIR / "templates").glob("*.service.template"))
    assert len(templates) >= 3
    for tmpl in templates:
        content = tmpl.read_text(encoding="utf-8")
        assert "[Unit]" in content or "[Service]" in content
