"""CLI: python -m liquidity_digest.job {collect,build,send,status,demo}."""
import argparse
from datetime import date, datetime, time
from email.message import EmailMessage
from email.utils import format_datetime, parseaddr
import hashlib
import json
import os
from pathlib import Path
import shutil
import smtplib
import ssl
import stat
import tempfile

from . import VERSION
from .model import NY, UTC, assemble, plan, sessions, stamp
from .render import chart_images, email_html, email_text, render_pdf
from .html_report import full_html
from .frontpage import index_images
from .email_images import add_html_with_images
from .sources import collect
from .store import Store, atomic_json, file_hash


def load_config(path):
    if path is None:
        raise ValueError("Pass an explicit private --config JSON file")
    path = Path(path).expanduser()
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("Config must be owned by this user with mode 0600")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Config must be a JSON object")
    return data


def stage_guard(stage, report_plan, now=None):
    now = stamp(now or datetime.now(UTC)).astimezone(NY)
    if str(now.date()) != report_plan["report_date"]:
        raise ValueError("Live jobs cannot replay a different date")
    windows = {
        "collect": (time(9, 0), time(9, 15)),
        "build": (time(9, 15), time(9, 20)),
        "send": (time(9, 20), time(9, 25)),
    }
    lower, upper = windows[stage]
    if not lower <= now.time().replace(tzinfo=None) < upper:
        raise ValueError("Outside live " + stage + " window")


def verify_bundle(folder):
    folder = Path(folder)
    manifest = json.loads((folder / "manifest.json").read_text())
    legacy_files = {"report.json", "report.pdf", "email.txt", "email.html"}
    files = set(manifest["files"])
    if files not in (legacy_files, legacy_files | {"report.html"}):
        raise ValueError("Unexpected manifest entries")
    for name, expected in manifest["files"].items():
        if file_hash(folder / name) != expected:
            raise ValueError("Frozen report integrity check failed")
    report = json.loads((folder / "report.json").read_text())
    if report["version"] not in (VERSION, "liquidity_digest_v1.7.3-shadow", "liquidity_digest_v1.7.2-shadow", "liquidity_digest_v1.7.1-shadow", "liquidity_digest_v1.7-shadow", "liquidity_digest_v1.6-shadow", "liquidity_digest_v1.5-shadow", "liquidity_digest_v1.4-shadow", "liquidity_digest_v1.3-shadow", "liquidity_digest_v1.2-shadow"):
        raise ValueError("Report version mismatch")
    expected = legacy_files | ({"report.html"} if report["version"] != "liquidity_digest_v1.2-shadow" else set())
    if files != expected:
        raise ValueError("Report attachment set does not match its version")
    return report


def write_bundle(report, folder):
    folder = Path(folder)
    if folder.exists():
        existing = verify_bundle(folder)
        if existing["report_date"] != report["report_date"] or existing["demo"] != report["demo"]:
            raise ValueError("Frozen report collision")
        return existing
    folder.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=".digest-build-", dir=folder.parent))
    try:
        atomic_json(temp / "report.json", report)
        (temp / "email.txt").write_text(email_text(report), encoding="utf-8")
        front_images = index_images(report)
        (temp / "email.html").write_text(email_html(report, front_images=front_images), encoding="utf-8")
        images = chart_images(report)
        render_pdf(report, temp / "report.pdf", images=images, front_images=front_images)
        (temp / "report.html").write_text(full_html(report, images=images, front_images=front_images), encoding="utf-8")
        files = {name: file_hash(temp / name) for name in (
            "report.json", "report.pdf", "report.html", "email.txt", "email.html")}
        atomic_json(temp / "manifest.json", {"files": files, "built_at": datetime.now(UTC).isoformat()})
        os.replace(temp, folder)
    finally:
        if temp.exists():
            shutil.rmtree(temp)
    return report


def build(store, report_plan, now=None):
    stage_guard("build", report_plan, now)
    with store.lock("build"):
        inputs = store.snapshot(report_plan["cutoff"])
        report = assemble(report_plan, inputs)
        return write_bundle(report, store.root / "reports" / report_plan["report_date"])


def email_addresses(config):
    result = []
    for key in ("SENDER_EMAIL", "RECEIVER_EMAIL"):
        raw = config[key]
        if not isinstance(raw, str) or "\n" in raw or "\r" in raw:
            raise ValueError("Invalid email address")
        address = parseaddr(raw)[1]
        if address != raw or "@" not in address or "," in address or ";" in address:
            raise ValueError("Use one plain email address per setting")
        result.append(address)
    return result


def smtp_connection(config):
    host = config.get("SMTP_HOST", "smtp.gmail.com")
    port = int(config.get("SMTP_PORT", 465))
    context = ssl.create_default_context()
    if port == 465:
        smtp = smtplib.SMTP_SSL(host, port, timeout=25, context=context)
    elif port == 587:
        smtp = smtplib.SMTP(host, port, timeout=25)
        try:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
        except Exception:
            smtp.close()
            raise
    else:
        raise ValueError("Only authenticated TLS ports 465/587 are supported")
    try:
        smtp.login(config["SENDER_EMAIL"], config["APP_PASSWORD"])
    except Exception:
        smtp.close()
        raise
    return smtp


def send(store, config, report_plan, now=None, connector=smtp_connection):
    stage_guard("send", report_plan, now)
    with store.lock("send"):
        previous = store.delivery(report_plan["report_date"])
        if previous:
            return {"status": "SKIPPED", "receipt": previous["status"]}
        folder = store.root / "reports" / report_plan["report_date"]
        report = verify_bundle(folder)
        if report["demo"] or report.get("diagnostic") or report.get("correction") or any(report[k] != report_plan[k]
                                 for k in ("report_date", "market_date", "cutoff", "send_at")):
            raise ValueError("Only today's frozen live report may be sent")
        sender, recipient = email_addresses(config)
        identifier = hashlib.sha256((report_plan["report_date"] + file_hash(
            folder / "report.json")).encode()).hexdigest()[:32]
        message = EmailMessage()
        message["From"], message["To"] = sender, recipient
        message["Subject"] = "美股流动性与风险传导日报｜盘前版 " + report_plan["report_date"]
        message["Message-ID"] = f"<liquidity-digest-{identifier}@{sender.split('@')[-1]}>"
        message["Date"] = format_datetime(datetime.now(UTC))
        message.set_content((folder / "email.txt").read_text())
        add_html_with_images(message, (folder / "email.html").read_text(), identifier)
        message.add_attachment((folder / "report.pdf").read_bytes(), maintype="application",
                               subtype="pdf", filename="liquidity_digest_" + report_plan["report_date"] + ".pdf")
        if report["version"] != "liquidity_digest_v1.2-shadow":
            message.add_attachment((folder / "report.html").read_bytes(), maintype="text",
                                   subtype="html", params={"charset": "utf-8"},
                                   filename="liquidity_digest_" + report_plan["report_date"] + ".html")
        smtp = connector(config)
        try:
            # Authentication may be retried; uncertain DATA acceptance must not be.
            stage_guard("send", report_plan, now)
            store.reserve_delivery(report_plan["report_date"], message["Message-ID"])
            try:
                rejected = smtp.send_message(message, from_addr=sender, to_addrs=[recipient])
                if rejected:
                    store.mark_delivery(report_plan["report_date"], "REJECTED", "recipient_refused")
                    raise RuntimeError("Recipient rejected")
            except Exception as exc:
                if store.delivery(report_plan["report_date"])["status"] == "PENDING":
                    store.mark_delivery(report_plan["report_date"], "UNCERTAIN", type(exc).__name__)
                raise
            # Mark accepted before QUIT: disconnect after acceptance is not a failed delivery.
            store.mark_delivery(report_plan["report_date"], "SENT", "smtp_accepted")
        finally:
            try:
                smtp.quit()
            except Exception:
                smtp.close()
        return {"status": "SENT", "meaning": "SMTP accepted; inbox delivery is not confirmed"}


def status(store, report_plan):
    inputs = store.snapshot(report_plan["cutoff"])
    folder = store.root / "reports" / report_plan["report_date"]
    report = verify_bundle(folder) if folder.exists() else None
    return {
        **report_plan, "frozen_report_exists": report is not None,
        "source_status": {k: {"status": v["status"], "captured_at": v["captured_at"]}
                          for k, v in inputs.items()},
        "receipt": store.delivery(report_plan["report_date"]),
        "missing_or_lagged": report["gaps"] if report else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("collect", "build", "send", "status", "demo"))
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--config")
    parser.add_argument("--date", help="Only supported for status or synthetic demo")
    args = parser.parse_args()
    os.umask(0o077)
    if args.date and args.command not in ("status", "demo"):
        parser.error("--date is forbidden for live jobs")
    day = args.date or str(datetime.now(NY).date())
    if day not in sessions(day, day):
        print(json.dumps({"status": "SKIPPED", "reason": "NYSE_CLOSED", "date": day}))
        return
    report_plan = plan(day)
    store = Store(args.state_dir)
    os.environ.setdefault("MPLCONFIGDIR", str(store.root / "matplotlib"))
    if args.command == "demo":
        from .sample import sample_report
        report = sample_report(report_plan)
        output = store.root / "demo" / day
        write_bundle(report, output)
        result = {"status": "DEMO_ONLY", "path": str(output)}
    elif args.command == "collect":
        stage_guard("collect", report_plan)
        config = load_config(args.config)
        with store.lock("collect"):
            result = collect(config, store, report_plan)
    elif args.command == "build":
        report = build(store, report_plan)
        result = {"status": "FROZEN", "gaps": report["gaps"], "date": report["report_date"]}
    elif args.command == "send":
        result = send(store, load_config(args.config), report_plan)
    else:
        result = status(store, report_plan)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Do not leak config/API secrets through third-party exception messages.
        print(json.dumps({"status": "FAILED", "error_type": type(error).__name__}))
        raise SystemExit(1)
