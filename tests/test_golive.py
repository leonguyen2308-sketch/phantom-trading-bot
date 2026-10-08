from phantom.golive import go_live_report


def test_fresh_install_is_refused(tmp_path):
    report = go_live_report(state_dir=tmp_path, reports_dir=tmp_path, risk_path="config/risk.yaml")
    assert not report.ok
    assert len(report.items) >= 6
    assert all(not i.ok for i in report.items)


def test_report_names_the_blow_up_section(tmp_path):
    report = go_live_report(state_dir=tmp_path, reports_dir=tmp_path, risk_path="config/risk.yaml")
    assert any("WHAT COULD BLOW UP THIS ACCOUNT?" in i.question for i in report.items)
