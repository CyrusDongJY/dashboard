from base64 import b64decode
from html.parser import HTMLParser
from io import BytesIO
import json
from unittest.mock import Mock

import pytest

from liquidity_digest import VERSION
from liquidity_digest.html_report import full_html
from liquidity_digest.job import send, status, verify_bundle, write_bundle
from liquidity_digest.model import assemble, plan
from liquidity_digest.sample import sample_report
from liquidity_digest.store import Store, atomic_json, file_hash


PNG = b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a1ioAAAAASUVORK5CYII=")
MAIL = {"SENDER_EMAIL": "a@example.com", "RECEIVER_EMAIL": "b@example.com", "APP_PASSWORD": "secret"}


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.nodes = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.nodes.append((tag, dict(attrs)))


@pytest.fixture
def report():
    return sample_report(plan("2026-10-20"))


def images():
    return [BytesIO(PNG) for _ in range(3)]


def stub_pdf(report, path, **kwargs):
    path.write_bytes(b"%PDF-test")


@pytest.fixture
def bundle(tmp_path, report, monkeypatch):
    store = Store(tmp_path)
    report["demo"] = False
    monkeypatch.setattr("liquidity_digest.job.chart_images", lambda r: images())
    monkeypatch.setattr("liquidity_digest.job.index_images", lambda r: images()[:2])
    monkeypatch.setattr("liquidity_digest.job.render_pdf", stub_pdf)
    folder = tmp_path / "reports" / report["report_date"]
    write_bundle(report, folder)
    return store, folder


def test_html_self_contained_and_complete(report):
    html = full_html(report, images())
    nodes = Elements(html).nodes
    assert html.startswith("<!doctype html>")
    assert not {"script", "iframe", "object", "embed", "form", "link"} & {tag for tag, _ in nodes}
    imgs = [a for tag, a in nodes if tag == "img"]
    assert len(imgs) == 5
    for img in imgs:
        assert img["src"].startswith("data:image/png;base64,")
        assert b64decode(img["src"].split(",", 1)[1]).startswith(b"\x89PNG")
        assert img["alt"]
    for _, attrs in nodes:
        assert not any(name.startswith("on") for name in attrs)
        if "href" in attrs:
            from liquidity_digest.capital import CFTC_URL, ICI_URL, FINRA_URL
            assert attrs["href"].startswith("#") or attrs["href"] in (CFTC_URL, ICI_URL, FINRA_URL)
    assert any(a.get("http-equiv") == "Content-Security-Policy" for _, a in nodes)
    for metric in report["metrics"].values():
        assert metric["label"] in html
    for label in ("宏观资金与融资", "微观市场承接", "盘后跨资产战术压力观察",
                  "近150个自然日", "采集审计", "模拟样例", "融资业务日历"):
        assert label in html
    assert "charset=\"utf-8\"" in html


def test_html_escapes_all_external_text(report):
    payload = '</td><script>alert("bad")</script><img src="https://invalid.test/tracker">'
    report["metrics"]["hy"].update(label=payload, source=payload)
    report["focus"] = [payload]
    report["source_audit"]["injected"] = {"source": payload, "status": payload}
    html = full_html(report, images())
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert len([1 for tag, _ in Elements(html).nodes if tag == "img"]) == 5


def test_missing_html_keeps_unknown_semantics():
    html = full_html(assemble(plan("2026-10-20"), {}), images())
    assert "证据不足" in html
    assert "待更新" in html
    assert "当前无法定位" in html
    assert "昨日收盘记录缺失" in html


def test_audit_uses_actual_fetch_timestamp(report):
    report["metrics"]["hy"]["fetched_at"] = "2026-10-20T13:10:07+00:00"
    assert "2026-10-20T13:10:07+00:00" in full_html(report, images())


def test_all_chart_panels_required(report):
    with pytest.raises(ValueError):
        full_html(report, images()[:2])


def test_bundle_manifest_contains_full_html(bundle):
    _, folder = bundle
    manifest = json.loads((folder / "manifest.json").read_text())
    assert set(manifest["files"]) == {"report.json", "report.pdf", "report.html", "email.txt", "email.html"}
    assert manifest["files"]["report.html"] == file_hash(folder / "report.html")
    assert verify_bundle(folder)["version"] == VERSION
    assert "HTML附件" in (folder / "email.txt").read_text()


def test_send_has_pdf_and_utf8_html_attachments(bundle):
    store, _ = bundle
    smtp = Mock()
    smtp.send_message.return_value = {}
    send(store, MAIL, plan("2026-10-20"), "2026-10-20T09:20:00-04:00", Mock(return_value=smtp))
    message = smtp.send_message.call_args.args[0]
    attachments = list(message.iter_attachments())
    assert [a.get_filename() for a in attachments] == [
        "liquidity_digest_2026-10-20.pdf", "liquidity_digest_2026-10-20.html"]
    assert [a.get_content_type() for a in attachments] == ["application/pdf", "text/html"]
    assert attachments[1].get_content_charset() == "utf-8"
    assert "盘后跨资产战术压力观察" in attachments[1].get_payload(decode=True).decode("utf-8")
    assert all(a.get_content_disposition() == "attachment" for a in attachments)


def test_html_tampering_blocks_email(bundle):
    store, folder = bundle
    (folder / "report.html").write_text("changed")
    connector = Mock()
    with pytest.raises(ValueError):
        send(store, MAIL, plan("2026-10-20"), "2026-10-20T09:20:00-04:00", connector)
    connector.assert_not_called()
    assert store.delivery("2026-10-20") is None


def test_current_version_cannot_silently_omit_html(bundle):
    _, folder = bundle
    manifest = json.loads((folder / "manifest.json").read_text())
    del manifest["files"]["report.html"]
    atomic_json(folder / "manifest.json", manifest)
    with pytest.raises(ValueError, match="attachment set"):
        verify_bundle(folder)


def test_legacy_archive_still_readable_without_rewriting(bundle):
    store, folder = bundle
    report = json.loads((folder / "report.json").read_text())
    report["version"] = "liquidity_digest_v1.2-shadow"
    atomic_json(folder / "report.json", report)
    (folder / "report.html").unlink()
    files = {name: file_hash(folder / name)
             for name in ("report.json", "report.pdf", "email.txt", "email.html")}
    atomic_json(folder / "manifest.json", {"files": files})
    before = file_hash(folder / "manifest.json")
    assert status(store, plan("2026-10-20"))["frozen_report_exists"] is True
    assert write_bundle({**report, "version": VERSION}, folder)["version"] == report["version"]
    assert not (folder / "report.html").exists()
    assert file_hash(folder / "manifest.json") == before


@pytest.mark.parametrize("version", ["liquidity_digest_v1.3-shadow", "liquidity_digest_v1.4-shadow", "liquidity_digest_v1.5-shadow", "liquidity_digest_v1.6-shadow"])
def test_prior_archives_preserve_html_attachment_contract(bundle, version):
    _, folder = bundle
    report = json.loads((folder / "report.json").read_text())
    report["version"] = version
    atomic_json(folder / "report.json", report)
    manifest = json.loads((folder / "manifest.json").read_text())
    manifest["files"]["report.json"] = file_hash(folder / "report.json")
    atomic_json(folder / "manifest.json", manifest)
    assert verify_bundle(folder)["version"] == version


def test_unknown_version_rejected(bundle):
    _, folder = bundle
    report = json.loads((folder / "report.json").read_text())
    report["version"] = "unknown"
    atomic_json(folder / "report.json", report)
    manifest = json.loads((folder / "manifest.json").read_text())
    manifest["files"]["report.json"] = file_hash(folder / "report.json")
    atomic_json(folder / "manifest.json", manifest)
    with pytest.raises(ValueError, match="version"):
        verify_bundle(folder)


def test_chart_failure_never_leaves_partial_bundle(tmp_path, report, monkeypatch):
    monkeypatch.setattr("liquidity_digest.job.chart_images", lambda r: images()[:2])
    monkeypatch.setattr("liquidity_digest.job.render_pdf", stub_pdf)
    folder = tmp_path / "reports" / report["report_date"]
    with pytest.raises(ValueError):
        write_bundle(report, folder)
    assert not folder.exists()
    assert not list(folder.parent.glob(".digest-build-*"))
