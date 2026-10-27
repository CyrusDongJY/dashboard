from copy import deepcopy
from io import BytesIO
from base64 import b64decode

from liquidity_digest.model import plan, assemble
from liquidity_digest.sample import sample_report
from liquidity_digest.reading import tactical_view, pressure_identity, alert_groups
from liquidity_digest.frontpage import front_html
from liquidity_digest.html_report import full_html
from liquidity_digest.render import render_pdf

PNG = b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a1ioAAAAASUVORK5CYII=")


def test_tactical_net_change_never_claims_continuity_or_mutates_scores():
    report = sample_report(plan("2026-10-23"))
    report["panels"]["tactical"]["stress_components"]["vix"]["source_date"] = report["market_date"]
    report["tactical_history"] = {"changes": {"1d": 7, "5d": 20, "21d": 42},
                                   "direction_cn": "近5日净压力增加",
                                   "windows": {"5d": {"available": 5, "required": 6, "missing_dates": ["2026-10-21"]}}}
    before = deepcopy(report)
    view = tactical_view(report)
    assert view["changes"]["5d"] == 20
    assert "待确认" in view["continuity"] and "2026-10-21" in view["continuity"]
    assert view["components"][0]["date"] == report["panels"]["tactical"]["stress_components"]["vix"]["source_date"]
    assert report == before


def test_missing_score_has_no_healthy_default():
    view = tactical_view(assemble(plan("2026-10-23"), {}))
    assert view["score"] is None
    assert all(v is None for v in view["changes"].values())
    assert "不可用" in view["continuity"]


def test_pressure_identity_tracks_fallback_instead_of_mixing_scores():
    report = sample_report(plan("2026-10-23"))
    report["states"]["display_pressure"] = {**report["states"]["pressure"], "panel": "environment"}
    assert pressure_identity(report)[1] == "本图采用：综合环境压力"
    report["states"]["display_pressure"]["panel"] = "tactical_stress"
    assert pressure_identity(report)[1] == "本图采用：跨资产战术压力代理"


def test_every_severe_warning_and_gap_remains_visible_and_escaped():
    report = sample_report(plan("2026-10-23"))
    front = report["frontpage"]
    front["alerts"] = [{"kind": "raw_guard", "text": f"critical-{i}"} for i in range(12)]
    front["alerts"] += [{"kind": "watch", "text": "secondary"}]
    front["gaps"] = ["<bad>core-missing"]
    primary, secondary = alert_groups(front)
    assert len(primary) == 12 and len(secondary) == 1
    html = front_html(report, [BytesIO(PNG), BytesIO(PNG)])
    alerts = html.split('id="alerts"')[1].split('id="priority"')[0]
    for i in range(12):
        assert alerts.index(f"critical-{i}</li>") < alerts.index("<details>")
    assert "&lt;bad&gt;core-missing" in alerts
    email = front_html(report, [BytesIO(PNG), BytesIO(PNG)], compact=True)
    assert "<details>" not in email and "secondary" in email


def test_rendering_is_read_only_and_mobile_charts_are_not_forced_wide():
    report = sample_report(plan("2026-10-23"))
    before = deepcopy(report)
    html = full_html(report, [BytesIO(PNG) for _ in range(8)], [BytesIO(PNG), BytesIO(PNG)])
    assert report == before
    assert "min-width:680px" not in html
    assert 'class="chart-grid"' in html and "本图采用：" in html
    assert html.count('alt="综合环境压力"') == 1


def test_pdf_flows_all_warnings_without_silent_truncation(tmp_path):
    from pypdf import PdfReader
    report = sample_report(plan("2026-10-23"))
    report["frontpage"]["alerts"] = [{"kind": "shock", "text": f"ALERT-{i:02d} " + "需核查" * 12} for i in range(45)]
    path = tmp_path / "overflow.pdf"
    render_pdf(report, path, [BytesIO(PNG) for _ in range(8)], [BytesIO(PNG), BytesIO(PNG)])
    pages = PdfReader(path).pages
    text = "".join(p.extract_text() for p in pages)
    assert len(pages) > 6
    assert all(f"ALERT-{i:02d}" in text for i in range(45))
