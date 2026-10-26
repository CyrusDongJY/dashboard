import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def upgrade():
    path = Path(__file__).resolve().parents[1] / 'deploy/liquidity-digest/upgrade_release.py'
    spec = importlib.util.spec_from_file_location('digest_upgrade', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_rejects_path_escape(upgrade, tmp_path):
    manifest = dict(git_commit='a'*40, version='liquidity_digest_v1.7-shadow',
                    files={'../escape': 'bad'}, producer_baselines={})
    (tmp_path / 'release_manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match='integrity'):
        upgrade.validate(tmp_path)


def test_manifest_rejects_uncommitted_release(upgrade, tmp_path):
    (tmp_path / 'release_manifest.json').write_text(json.dumps({'version': 'v1'}))
    with pytest.raises(RuntimeError, match='committed'):
        upgrade.validate(tmp_path)


def setup_producers(upgrade, monkeypatch, tmp_path):
    home, release, backup = [tmp_path / p for p in ('home', 'release', 'backup')]
    for path in (home / 'market_dashboard', home / '.config/liquidity_digest', release, backup):
        path.mkdir(parents=True)
    (home / '.config/liquidity_digest/config.json').write_text(json.dumps(
        {'SUPABASE_URL': 'https://example.invalid', 'SUPABASE_KEY': 'test-only'}))
    original = {'producers': {}}
    for name in upgrade.PRODUCERS:
        target = home / 'market_dashboard' / name
        target.write_text('original')
        (backup / name).write_text('original')
        (release / name).write_text('new')
        original['producers'][str(target)] = upgrade.sha(target)
    (backup / 'before.json').write_text(json.dumps({'invariants': original}))
    monkeypatch.setattr(upgrade, 'HOME', home)
    monkeypatch.setattr(upgrade, 'guard', lambda: None)
    monkeypatch.setattr(upgrade, 'snapshot', lambda: original)
    monkeypatch.setattr(upgrade, 'validate', lambda p: {'git_commit': 'a'*40})
    return home, release, backup


def test_missing_migration_never_replaces_producers(upgrade, monkeypatch, tmp_path):
    home, release, backup = setup_producers(upgrade, monkeypatch, tmp_path)
    monkeypatch.setattr('requests.get', lambda *a, **k: SimpleNamespace(status_code=400))
    with pytest.raises(RuntimeError, match='migration'):
        upgrade.activate_producers(release, backup)
    assert all((home / 'market_dashboard' / n).read_text() == 'original' for n in upgrade.PRODUCERS)


def test_import_failure_rolls_back_all_replaced_files(upgrade, monkeypatch, tmp_path):
    home, release, backup = setup_producers(upgrade, monkeypatch, tmp_path)
    monkeypatch.setattr('requests.get', lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: []))
    def fail(*args, **kwargs):
        raise RuntimeError('import failed')
    monkeypatch.setattr(upgrade, 'run', fail)
    with pytest.raises(RuntimeError, match='import failed'):
        upgrade.activate_producers(release, backup)
    assert all((home / 'market_dashboard' / n).read_text() == 'original' for n in upgrade.PRODUCERS)
    assert not (backup / 'producer_acceptance.json').exists()
