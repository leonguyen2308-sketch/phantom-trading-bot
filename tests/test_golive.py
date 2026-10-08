from phantom.golive import go_live_report


def test_fresh_install_is_refused(tmp_path):
    report = go_live_report(state_dir=tmp_path, reports_dir=tmp_path, risk_path="config/risk.yaml")
    assert not report.ok
    assert len(report.items) >= 6
    assert all(not i.ok for i in report.items)


def test_report_names_the_blow_up_section(tmp_path):
    report = go_live_report(state_dir=tmp_path, reports_dir=tmp_path, risk_path="config/risk.yaml")
    assert any("WHAT COULD BLOW UP THIS ACCOUNT?" in i.question for i in report.items)


def test_rules_only_gate_does_not_count(tmp_path):
    import json
    (tmp_path / "gate.json").write_text(json.dumps({"passed": True, "strategy_version": "v0-unvalidated", "jev": False, "holdout_runs": 1, "jev_cutoff": "2026-01-01"}))
    report = go_live_report(state_dir=tmp_path, reports_dir=tmp_path, risk_path="config/risk.yaml")
    assert not report.items[0].ok
