
def test_stage4_files_exist():
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    assert (root/"app"/"templates"/"home.html").exists()
    assert (root/"app"/"templates"/"ops.html").exists()
    assert (root/"app"/"templates"/"privacy.html").exists()
    assert (root/"app"/"static"/"app.css").exists()

def test_core_source_contains_stage4():
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    main=(root/"app"/"main.py").read_text(encoding="utf-8")
    models=(root/"app"/"models.py").read_text(encoding="utf-8")
    for token in ["def ops_dashboard","def system_health","def audit","def privacy_page"]:
        assert token in main
    for token in ["class ConsentRecord","class AuditRecord"]:
        assert token in models
