"""Upgrade an existing installation without changing schedules or frozen reports.

Run prepare, activate-digest, then activate-producers after the additive migration.
The archive must contain a committed-file manifest and producer baseline hashes.
"""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
from zoneinfo import ZoneInfo

HOME = Path('/home/winters_dong426')
ROOT = HOME / 'liquidity_digest_app'
STATE = HOME / 'liquidity_digest_state'
PYTHON = HOME / 'liquidity_digest_venv/bin/python'
PRODUCERS = ('environment_indices.py', 'environment_report.py', 'risk_capital_ladder.py',
             'risk_event_pulse.py', 'temporal_monitor.py', 'temporal_report.py')
UNITS = ['liquidity-digest@.service'] + [f'liquidity-digest-{x}.timer' for x in ('collect', 'build', 'send')]


def run(args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, data):
    with path.open('x') as stream:
        json.dump(data, stream, indent=2)


def guard():
    now = datetime.now(ZoneInfo('America/New_York'))
    if now.hour == 9 and now.minute < 26:
        raise RuntimeError('Cannot upgrade during collection/freeze/send')
    for stage in ('collect', 'build', 'send'):
        value = run(['systemctl', 'show', f'liquidity-digest@{stage}.service', '-p',
                     'ActiveState', '--value'], capture_output=True, text=True).stdout.strip()
        if value not in ('inactive', 'failed'):
            raise RuntimeError('Digest job is running')
    commands = run(['ps', '-u', str(os.getuid()), '-o', 'args='], capture_output=True, text=True).stdout
    for command in commands.splitlines():
        if any(name in command for name in ('daily_post_close.py', 'auto_analyst.py', 'market_sentinel.py')):
            raise RuntimeError('A producer job is running')


def snapshot():
    cron = run(['crontab', '-l'], capture_output=True).stdout
    with sqlite3.connect(f'file:{STATE / "captures.sqlite3"}?mode=ro', uri=True) as db:
        deliveries = db.execute('SELECT * FROM deliveries ORDER BY report_date').fetchall()
    producers = {}
    for directory in (HOME / 'market_dashboard', HOME / 'quant_bot', HOME / 'core_funding_monitor/current'):
        for path in sorted(directory.glob('*.py')):
            producers[str(path)] = sha(path)
    return json.loads(json.dumps({
        'cron_sha256': hashlib.sha256(cron).hexdigest(), 'deliveries': deliveries,
        'reports': {str(p.relative_to(STATE)): sha(p) for p in sorted((STATE / 'reports').rglob('*')) if p.is_file()},
        'units': {name: sha(Path('/etc/systemd/system') / name) for name in UNITS},
        'config_sha256': sha(HOME / '.config/liquidity_digest/config.json'), 'producers': producers,
    }))


def validate(release):
    manifest = json.loads((release / 'release_manifest.json').read_text())
    if not manifest.get('git_commit') or not manifest['version'].startswith('liquidity_digest_'):
        raise RuntimeError('Missing committed release identity')
    for name, digest in manifest['files'].items():
        if Path(name).is_absolute() or '..' in Path(name).parts or sha(release / name) != digest:
            raise RuntimeError('Release integrity failure')
    if (set(manifest['producer_baselines']) != set(PRODUCERS)
            or not set(PRODUCERS).issubset(manifest['files'])):
        raise RuntimeError('Unexpected producer scope')
    return manifest


def prepare(args, release, backup):
    guard()
    archive = Path(args.archive)
    if sha(archive) != args.sha256:
        raise RuntimeError('Archive hash mismatch')
    release.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive) as bundle:
        for entry in bundle.getmembers():
            if Path(entry.name).is_absolute() or '..' in Path(entry.name).parts or not entry.isfile():
                raise RuntimeError('Unsafe archive member')
        bundle.extractall(release)
    manifest = validate(release)
    backup.mkdir(parents=True, exist_ok=False)
    before = snapshot()
    for name, expected in manifest['producer_baselines'].items():
        path = HOME / 'market_dashboard' / name
        if before['producers'].get(str(path)) != expected:
            raise RuntimeError('Producer differs from verified baseline: ' + name)
        shutil.copy2(path, backup / name)
    save(backup / 'before.json', {'previous_release': str((ROOT / 'current').resolve()),
                                'archive_sha256': args.sha256, 'invariants': before})
    shutil.copy2(archive, backup / 'release.tgz')
    shutil.copy2(__file__, backup / 'upgrade.py')
    (backup / 'crontab.before').write_bytes(run(['crontab', '-l'], capture_output=True).stdout)
    for unit in UNITS:
        shutil.copy2(Path('/etc/systemd/system') / unit, backup / unit)
    with sqlite3.connect(f'file:{STATE / "captures.sqlite3"}?mode=ro', uri=True) as source:
        with sqlite3.connect(backup / 'captures.sqlite3') as dest:
            source.backup(dest)
    env = {**os.environ, 'MPLBACKEND': 'Agg', 'MPLCONFIGDIR': str(backup / 'matplotlib'),
           'PYTHONDONTWRITEBYTECODE': '1'}
    with (backup / 'requirements.freeze.txt').open('w') as output:
        run([PYTHON, '-m', 'pip', 'freeze'], stdout=output)
    run([PYTHON, '-m', 'compileall', '-q', release])
    with (backup / 'tests.log').open('w') as output:
        result = subprocess.run([str(PYTHON), '-c', 'import pandas_market_calendars; import pytest; '
                                 'raise SystemExit(pytest.main(["-q", "tests", "--tb=short"]))'],
                                cwd=release, env=env, stdout=output, stderr=subprocess.STDOUT)
    print((backup / 'tests.log').read_text(), flush=True)
    result.check_returncode()
    run([PYTHON, '-m', 'liquidity_digest.job', 'demo', '--date',
         str(datetime.now(ZoneInfo('America/New_York')).date()), '--state-dir', backup / 'smoke_state'],
        cwd=release, env=env)
    if before != snapshot():
        raise RuntimeError('Production changed during preparation')
    save(backup / 'prepared.json', {'release': str(release), 'version': manifest['version'],
                                  'files': len(manifest['files']), 'production_unchanged': True})
    print(json.dumps({'phase': 'prepared_not_active', 'backup': str(backup)}), flush=True)


def activate_digest(release, backup):
    guard()
    manifest = validate(release)
    prepared = json.loads((backup / 'prepared.json').read_text())
    before = json.loads((backup / 'before.json').read_text())
    if prepared['release'] != str(release) or str((ROOT / 'current').resolve()) != before['previous_release']:
        raise RuntimeError('Deployment pointer changed')
    if snapshot() != before['invariants']:
        raise RuntimeError('Production changed since preparation')
    link = ROOT / ('pending-' + release.name)
    link.symlink_to(release, target_is_directory=True)
    os.replace(link, ROOT / 'current')
    try:
        output = run([PYTHON, '-c', 'from liquidity_digest import VERSION; '
                      'from liquidity_digest.job import main; print(VERSION)'], cwd=ROOT / 'current',
                     capture_output=True, text=True).stdout.strip()
        if output != manifest['version'] or snapshot() != before['invariants']:
            raise RuntimeError('Post-activation verification failed')
        timers = {}
        for kind in ('collect', 'build', 'send'):
            unit = f'liquidity-digest-{kind}.timer'
            props = run(['systemctl', 'show', unit, '-p', 'ActiveState', '-p', 'UnitFileState',
                         '-p', 'NextElapseUSecRealtime'], capture_output=True, text=True).stdout.strip()
            if 'ActiveState=active' not in props or 'UnitFileState=enabled' not in props:
                raise RuntimeError('Timer is not enabled and active')
            timers[unit] = props
    except BaseException:
        link.symlink_to(before['previous_release'], target_is_directory=True)
        os.replace(link, ROOT / 'current')
        raise
    acceptance = dict(phase='digest_active_verified', git_commit=manifest['git_commit'],
                      version=output, release=str(release), previous_release=before['previous_release'],
                      archive_sha256=before['archive_sha256'], files_verified=len(manifest['files']),
                      frozen_files_unchanged=len(before['invariants']['reports']),
                      receipts_unchanged=len(before['invariants']['deliveries']),
                      cron_units_config_unchanged=True, smtp_called=False, database_writes=False,
                      timers=timers, verified_at=datetime.now(ZoneInfo('America/New_York')).isoformat())
    save(backup / 'digest_acceptance.json', acceptance)
    print(json.dumps(acceptance), flush=True)


def activate_producers(release, backup):
    guard()
    manifest = validate(release)
    before = snapshot()
    original = json.loads((backup / 'before.json').read_text())['invariants']
    if before['producers'] != original['producers']:
        raise RuntimeError('Producer changed since preparation')
    import requests
    config = json.loads((HOME / '.config/liquidity_digest/config.json').read_text())
    response = requests.get(config['SUPABASE_URL'].rstrip('/') + '/rest/v1/environment_daily',
                            headers={'apikey': config['SUPABASE_KEY'],
                                     'Authorization': 'Bearer ' + config['SUPABASE_KEY']},
                            params={'select': 'report_date,calc_version,details', 'limit': 1}, timeout=15)
    if response.status_code != 200 or not isinstance(response.json(), list):
        raise RuntimeError('environment_daily.details migration is not verified')
    replaced = []
    try:
        for name in PRODUCERS:
            dest = HOME / 'market_dashboard' / name
            temp = dest.with_name('.pending-' + name)
            shutil.copy2(release / name, temp)
            os.replace(temp, dest)
            replaced.append(name)
        run(['/usr/bin/python3', '-c', 'import environment_indices, environment_report, '
             'risk_capital_ladder, risk_event_pulse, temporal_monitor, temporal_report; '
             'print(environment_indices.ENV_CALC_VERSION)'], cwd=HOME / 'market_dashboard')
        after = snapshot()
        expected = json.loads(json.dumps(before))
        for name in PRODUCERS:
            expected['producers'][str(HOME / 'market_dashboard' / name)] = manifest['files'][name]
        if after != expected:
            raise RuntimeError('Unexpected post-deployment change')
    except BaseException:
        for name in reversed(replaced):
            dest = HOME / 'market_dashboard' / name
            temp = dest.with_name('.rollback-' + name)
            shutil.copy2(backup / name, temp)
            os.replace(temp, dest)
        raise
    result = dict(phase='producers_active_verified', git_commit=manifest['git_commit'],
                  schema_http_status=200, files={name: manifest['files'][name] for name in PRODUCERS},
                  unrelated_files_unchanged=True, frozen_reports_unchanged=True,
                  score_rows_rewritten=False, verified_at=datetime.now(ZoneInfo('America/New_York')).isoformat())
    save(backup / 'producer_acceptance.json', result)
    print(json.dumps(result), flush=True)


def main():
    os.umask(0o077)
    if Path.home() != HOME:
        raise RuntimeError('Wrong deployment account')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['prepare', 'activate-digest', 'activate-producers'])
    parser.add_argument('--release', required=True)
    parser.add_argument('--archive')
    parser.add_argument('--sha256')
    args = parser.parse_args()
    if not args.release.replace('-', '').replace('_', '').isalnum():
        raise ValueError('Invalid release name')
    release = ROOT / 'releases' / args.release
    backup = HOME / 'deploy_backups' / ('liquidity_digest_' + args.release)
    if args.stage == 'prepare':
        prepare(args, release, backup)
    elif args.stage == 'activate-digest':
        activate_digest(release, backup)
    else:
        activate_producers(release, backup)


if __name__ == '__main__':
    main()
