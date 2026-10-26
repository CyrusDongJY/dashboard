"""Explicit manual corrections, isolated from scheduled reports and receipts."""
import argparse
from datetime import datetime, timedelta, time
from email.message import EmailMessage
from email.utils import format_datetime
import hashlib
import json
import os
import re

from .email_images import add_html_with_images
from .job import (email_addresses, load_config, smtp_connection, verify_bundle,
                  write_bundle)
from .model import NY, UTC, assemble, plan, sessions, stamp
from .sources import collect
from .store import Store, file_hash


def correction_store(state, revision):
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}', revision):
        raise ValueError('Invalid correction revision')
    return Store(state.root / 'corrections' / revision)


def build_correction(state, config, revision, original_date, market_date, note,
                     collector=collect):
    if not note.strip():
        raise ValueError('A correction explanation is required')
    original_folder = state.root / 'reports' / original_date
    original = verify_bundle(original_folder)
    if original['demo'] or original.get('diagnostic'):
        raise ValueError('Correction must refer to an original scheduled report')
    now = datetime.now(NY)
    day = str(now.date())
    report_plan = plan(day)
    if (market_date not in sessions(original['market_date'], day)
            or (market_date == day and now.time().replace(tzinfo=None) < time(16))):
        raise ValueError('Only completed cash sessions may be used')
    store = correction_store(state, revision)
    folder = store.root / 'report'
    with store.lock('build'):
        if folder.exists():
            raise ValueError('Correction already frozen; use its existing artifacts')
        report_plan.update(market_date=market_date,
                           cutoff=(now + timedelta(seconds=210)).isoformat())
        collector(config, store, report_plan)
        # The fetch deadline is not an information cutoff. Freeze actual reception time.
        cutoff = datetime.now(UTC).isoformat()
        report_plan.update(cutoff=cutoff, send_at=None)
        report = assemble(report_plan, store.snapshot(cutoff))
        report.update(diagnostic=True, correction={
            'revision': revision, 'original_date': original_date,
            'original_sha256': file_hash(original_folder / 'report.json'), 'note': note})
        write_bundle(report, folder)
        return report


def send_correction(state, config, revision, connector=smtp_connection):
    store = correction_store(state, revision)
    with store.lock('send'):
        previous = store.delivery(revision)
        if previous:
            return {'status': 'SKIPPED', 'receipt': previous['status']}
        folder = store.root / 'report'
        report = verify_bundle(folder)
        meta = report.get('correction') or {}
        if report['demo'] or not report.get('diagnostic') or meta.get('revision') != revision:
            raise ValueError('Only an explicitly frozen correction may be sent')
        if stamp(report['cutoff']) > datetime.now(UTC):
            raise ValueError('Future correction cutoff')
        original_folder = state.root / 'reports' / meta['original_date']
        verify_bundle(original_folder)
        if file_hash(original_folder / 'report.json') != meta['original_sha256']:
            raise ValueError('Original report changed; investigate before sending')
        sender, recipient = email_addresses(config)
        identifier = hashlib.sha256((revision + file_hash(folder / 'report.json')).encode()).hexdigest()[:32]
        message = EmailMessage()
        message['From'], message['To'] = sender, recipient
        message['Subject'] = '美股流动性与风险传导日报｜核查更新版 ' + report['report_date']
        message['Message-ID'] = f'<liquidity-correction-{identifier}@{sender.split("@")[-1]}>'
        message['Date'] = format_datetime(datetime.now(UTC))
        message.set_content((folder / 'email.txt').read_text())
        add_html_with_images(message, (folder / 'email.html').read_text(), identifier)
        for suffix, main, sub in (('pdf', 'application', 'pdf'), ('html', 'text', 'html')):
            message.add_attachment((folder / ('report.' + suffix)).read_bytes(),
                                   maintype=main, subtype=sub,
                                   filename='liquidity_digest_correction_' + report['report_date'] + '.' + suffix)
        smtp = connector(config)
        try:
            store.reserve_delivery(revision, message['Message-ID'])
            try:
                if smtp.send_message(message, from_addr=sender, to_addrs=[recipient]):
                    store.mark_delivery(revision, 'REJECTED', 'recipient_refused')
                    raise RuntimeError('Recipient rejected')
            except Exception as exc:
                if store.delivery(revision)['status'] == 'PENDING':
                    store.mark_delivery(revision, 'UNCERTAIN', type(exc).__name__)
                raise
            store.mark_delivery(revision, 'SENT', 'smtp_accepted')
        finally:
            try:
                smtp.quit()
            except Exception:
                smtp.close()
        return {'status': 'SENT', 'message_id': message['Message-ID'],
                'meaning': 'SMTP accepted; inbox delivery is not confirmed'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('build', 'send'))
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--original-date')
    parser.add_argument('--market-date')
    parser.add_argument('--note', default='')
    args = parser.parse_args()
    os.umask(0o077)
    state, config = Store(args.state_dir), load_config(args.config)
    if args.command == 'build':
        if not args.original_date or not args.market_date:
            parser.error('build requires original-date and market-date')
        report = build_correction(state, config, args.revision, args.original_date,
                                  args.market_date, args.note)
        result = {'status': 'FROZEN_CORRECTION', 'cutoff': report['cutoff'],
                  'market_date': report['market_date'], 'gaps': report['frontpage']['gaps']}
    else:
        result = send_correction(state, config, args.revision)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'status': 'FAILED', 'error_type': type(error).__name__}))
        raise SystemExit(1)
