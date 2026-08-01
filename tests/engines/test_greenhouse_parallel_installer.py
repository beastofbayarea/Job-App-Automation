from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_parallel_greenhouse_installer_has_transactional_service_order() -> None:
    script = (ROOT / "scripts" / "install_vps_greenhouse_excel_parallel.ps1").read_text(
        encoding="utf-8"
    )

    assert "data/greenhouse_roles.xlsx" in script
    assert "continuous_source_ats" in script
    assert "job-app-greenhouse-excel.service" in script
    assert "systemctl is-active --quiet job-app-greenhouse-excel.service" in script
    assert script.index("systemctl is-active --quiet job-app-greenhouse-excel.service") < (
        script.index("systemctl disable --now job-app-ashby.service")
    )
    assert 'test "`$(systemctl is-active job-app-ashby.service || true)" = inactive' in script


def test_greenhouse_source_template_has_independent_supervision_and_limits() -> None:
    template = (
        ROOT / "scripts" / "templates" / "job-app-greenhouse-source.service.template"
    ).read_text(encoding="utf-8")

    assert "Restart=always" in template
    assert "continuous_source_ats" in template
    assert "MemoryMax=1050M" in template
    assert "__SOURCE_ARGS__" in template
