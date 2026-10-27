from copy import deepcopy
from datetime import date
import importlib.util
import json
from pathlib import Path
from io import BytesIO
from unittest.mock import Mock

import pandas as pd
import pytest

from liquidity_digest.correction import correction_store, send_correction
from liquidity_digest import correction as correction_module
from liquidity_digest.frontpage import index_observation, index_summary
from liquidity_digest.job import verify_bundle
from liquidity_digest.model import plan
from liquidity_digest.render import email_html, email_text
from liquidity_digest.sample import sample_report
from liquidity_digest.store import Store, atomic_json, file_hash
from test_liquidity_digest_frontpage import capture, P, PNG


def test_window_counts_do_not_fill_missing_values_with_replay():
    data = capture(count=1)
    replay = deepcopy(data['data'][0])
    replay['observation_mode'] = replay['result']['observation_mode'] = 'PIT_REPLAY'
    data['data'].append(replay)
    item = index_observation(P, data, 'core_funding')
    assert item['window_coverage']['5d']['valid'] == 1
    assert item['changes']['5d'] is None
    assert '5日样本 1/6' in index_summary(item)


def test_missing_component_reason_reports_warmup_count():
    data = capture('liquidity')
    data['data'][-1].update(composite=None, coverage=.96)
    data['data'][-1]['details']['components'] = [dict(
        key='vix_futures_curve', score=None, valid_obs_count=10, required_obs_count=60)]
    item = index_observation(P, data, 'liquidity')
    assert 'VX标准月期货曲线' in item['reason'] and '10/60' in item['reason']
    assert item['value'] is None


def bundle(folder, report):
    folder.mkdir(parents=True)
    atomic_json(folder / 'report.json', report)
    (folder / 'report.pdf').write_bytes(b'%PDF-test')
    for name in ('report.html', 'email.html'):
        (folder / name).write_text('<html>correction</html>')
    (folder / 'email.txt').write_text('correction')
    atomic_json(folder / 'manifest.json', {'files': {p.name: file_hash(p) for p in folder.iterdir()}})


@pytest.fixture
def frozen(tmp_path):
    state = Store(tmp_path)
    original = sample_report(plan('2026-10-23'))
    original['demo'] = False
    root = state.root / 'reports/2026-10-23'
    bundle(root, original)
    state.reserve_delivery('2026-10-23', '<original@example.com>')
    state.mark_delivery('2026-10-23', 'SENT')
    report = deepcopy(original)
    report.update(diagnostic=True, correction=dict(revision='review-1', original_date='2026-10-23',
                  original_sha256=file_hash(root / 'report.json'), note='真实补数'))
    correction = correction_store(state, 'review-1')
    bundle(correction.root / 'report', report)
    config = dict(SENDER_EMAIL='sender@example.com', RECEIVER_EMAIL='user@example.com')
    return state, correction, config


def test_correction_send_preserves_original_and_never_duplicates(frozen):
    state, correction, config = frozen
    original = state.delivery('2026-10-23')
    smtp = Mock()
    smtp.send_message.return_value = {}
    smtp.quit.side_effect = OSError('disconnect after accepted')
    connector = Mock(return_value=smtp)
    assert send_correction(state, config, 'review-1', connector)['status'] == 'SENT'
    assert send_correction(state, config, 'review-1', connector)['status'] == 'SKIPPED'
    assert connector.call_count == 1
    assert state.delivery('2026-10-23') == original
    message = smtp.send_message.call_args.args[0]
    assert '核查更新版' in message['Subject']
    assert {p.get_content_type() for p in message.iter_attachments()} == {'application/pdf', 'text/html'}


def test_uncertain_correction_is_not_retried(frozen):
    state, correction, config = frozen
    smtp = Mock()
    smtp.send_message.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        send_correction(state, config, 'review-1', lambda c: smtp)
    assert correction.delivery('review-1')['status'] == 'UNCERTAIN'
    assert send_correction(state, config, 'review-1', lambda c: smtp)['status'] == 'SKIPPED'


def test_tampering_blocks_correction_before_authentication(frozen):
    state, correction, config = frozen
    (state.root / 'reports/2026-10-23/report.json').write_text('{}')
    connector = Mock()
    with pytest.raises(ValueError):
        send_correction(state, config, 'review-1', connector)
    connector.assert_not_called()


def test_revision_cannot_escape_state(tmp_path):
    with pytest.raises(ValueError):
        correction_store(Store(tmp_path), '../reports')


def test_build_uses_actual_freeze_time_and_keeps_original(frozen, monkeypatch):
    from datetime import datetime, timezone
    state, _, config = frozen
    original_hash = file_hash(state.root / 'reports/2026-10-23/report.json')
    actual = datetime(2026, 10, 26, 21, 30, tzinfo=timezone.utc)

    class Clock:
        @staticmethod
        def now(tz):
            return actual.astimezone(tz)

    monkeypatch.setattr(correction_module, 'datetime', Clock)
    monkeypatch.setattr(correction_module, 'write_bundle', Mock())
    collector = Mock()
    report = correction_module.build_correction(state, config, 'review-2', '2026-10-23',
                                                '2026-10-26', '原始数据补数', collector)
    assert report['cutoff'] == actual.isoformat()
    assert report['market_date'] == '2026-10-26'
    assert report['send_at'] is None and report['diagnostic']
    assert file_hash(state.root / 'reports/2026-10-23/report.json') == original_hash
    assert collector.call_count == 1
    with pytest.raises(ValueError):
        correction_module.build_correction(state, config, 'review-3', '2026-10-23',
                                            '2026-10-27', '未来不可用', collector)


def test_authentication_failure_does_not_reserve_delivery(frozen):
    state, correction, config = frozen
    with pytest.raises(ConnectionError):
        send_correction(state, config, 'review-1', Mock(side_effect=ConnectionError()))
    assert correction.delivery('review-1') is None


def test_manual_edition_does_not_claim_morning_cutoff():
    report = sample_report(P)
    report.update(diagnostic=True, cutoff='2026-10-23T21:30:00+00:00', correction={'note': '<真实补数>'})
    assert '日报｜核查更新版' in email_text(report)
    html = email_html(report, front_images=[BytesIO(PNG), BytesIO(PNG)])
    assert '2026-10-23 核查更新版' in html
    assert '&lt;真实补数&gt;' in html


def test_single_temporal_observation_has_visible_marker_and_label(monkeypatch):
    from matplotlib.axes import Axes
    from liquidity_digest.render import chart_images
    original = Axes.plot
    calls = []

    def record(self, *args, **kwargs):
        calls.append(kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Axes, 'plot', record)
    report = sample_report(P)
    for key in report['temporal']:
        report['temporal'][key] = report['temporal'][key][-1:]
    images = chart_images(report)
    singles = [c for c in calls if '单点，趋势待积累' in c.get('label', '')]
    assert len(images) == 8 and len(singles) == 4
    assert all(c['marker'] == 'o' and c['markersize'] >= 4 and c['clip_on'] is False for c in singles)


def test_vx_repair_is_narrow_idempotent_and_dst_aware():
    path = Path(__file__).parents[1] / 'deploy/liquidity-digest/repair_vx_request.py'
    spec = importlib.util.spec_from_file_location('vx_repair', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = "unchanged = 42\ndef get_vix_term_structure(ib, as_of):\n    aligned_curve()\n    return ib.reqHistoricalData(endDateTime=f'{as_of:%Y%m%d} 16:00:00 America/New_York')\n"
    fixed = module.repaired_source(source)
    assert fixed.startswith('unchanged = 42\n')
    assert module.repaired_source(fixed) == fixed
    namespace = {'pd': pd, 'aligned_curve': lambda: None}
    exec(fixed, namespace)
    ib = Mock()
    for day, hour in ((date(2026, 10, 26), 20), (date(2026, 11, 2), 21)):
        namespace['get_vix_term_structure'](ib, day)
        cutoff = ib.reqHistoricalData.call_args.kwargs['endDateTime']
        assert cutoff.tzinfo is not None
        assert pd.Timestamp(cutoff).tz_convert('UTC').hour == hour
    with pytest.raises(ValueError):
        module.repaired_source('def get_vix_term_structure(ib):\n    pass\n')
