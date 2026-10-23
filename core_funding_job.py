"""Isolated scheduled job. No imports from existing market/broker programs."""
from __future__ import annotations

import argparse
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
import json
import logging
import os
from pathlib import Path
import smtplib
import ssl

import pandas as pd

from core_funding_index import CALC_VERSION
from core_funding_pipeline import market_sessions, run_core_funding
from core_funding_sources import NY, utc_time, write_json_atomic

logger = logging.getLogger(__name__)


def load_private_config(path):
    path = Path(path)
    stat = path.stat()
    if stat.st_mode & 0o077 or stat.st_uid != os.getuid():
        raise ValueError("Config must be owned by the job user, with mode 600")
    config = json.loads(path.read_text())
    if not config.get("FRED_API_KEY"):
        raise ValueError("FRED_API_KEY missing from independent config")
    return config


def send_report(config, output, receipt_path):
    receipt_path = Path(receipt_path)
    if receipt_path.exists():
        status = json.loads(receipt_path.read_text()).get("status")
        if status == "SENT":
            logger.info("Email already accepted; not sending again")
            return "ALREADY_SENT"
        raise RuntimeError("Uncertain prior email delivery; inspect receipt before retry")
    result = output["result"]
    replay = result["observation_mode"] == "PIT_REPLAY"
    subject = "[历史回放/部署验收]" if replay else "[独立影子观察]"
    message = EmailMessage()
    message["Subject"] = f"{subject} 核心资金充裕度指数 {result['report_date']}"
    message["From"] = config["SENDER_EMAIL"]
    message["To"] = config["RECEIVER_EMAIL"]
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain=config["SENDER_EMAIL"].split("@")[-1])
    note = "历史回放验收，不是今天的实时报告。\n\n" if replay else ""
    message.set_content(note + output["summary"] + "\n\n独立任务；不参与原系统预警或交易评分。")
    if output.get("chart"):
        chart = Path(output["chart"])
        message.add_attachment(chart.read_bytes(), maintype="image", subtype="png", filename=chart.name)
    if output.get("diagnostics_chart"):
        try:
            chart = Path(output["diagnostics_chart"])
            message.add_attachment(chart.read_bytes(), maintype="image", subtype="png", filename=chart.name)
        except OSError:
            logger.warning("Optional diagnostics attachment unavailable; sending core report")
    # Write before SMTP to avoid duplicate mail after ambiguous disconnects/crashes.
    receipt = {"status": "PENDING", "message_id": message["Message-ID"],
               "report_date": result["report_date"], "mode": result["observation_mode"]}
    write_json_atomic(receipt_path, receipt)
    with smtplib.SMTP_SSL(config.get("SMTP_HOST", "smtp.gmail.com"),
                         int(config.get("SMTP_PORT", 465)), timeout=30,
                         context=ssl.create_default_context()) as smtp:
        smtp.login(config["SENDER_EMAIL"], config["APP_PASSWORD"])
        refused = smtp.send_message(message)
        if refused:
            raise RuntimeError("SMTP refused a recipient")
    write_json_atomic(receipt_path, {**receipt, "status": "SENT",
                                   "accepted_at": pd.Timestamp.now(tz="UTC").isoformat()})
    return "SENT"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Independent core funding daily job")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--as-of")
    parser.add_argument("--sessions", type=int, default=300)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--email", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args(argv)
    if args.email and not args.write:
        parser.error("--email requires --write and verified database readback")
    now = utc_time(args.as_of) if args.as_of else pd.Timestamp.now(tz="UTC")
    date = str(now.tz_convert(NY).date())
    sessions = market_sessions(date, 1)
    if not sessions or str(sessions[-1].date()) != date:
        logger.info("SKIPPED_NON_TRADING_DAY %s", date)
        return 0
    if not args.as_of and now < pd.Timestamp(f"{date} 16:45", tz=NY).tz_convert("UTC"):
        raise ValueError("Live job may only run after 16:45 ET")
    config = load_private_config(args.config)
    mode = "PIT_REPLAY" if args.as_of else "LIVE_SHADOW"
    receipt_path = Path(args.output_dir) / "mail_receipts" / f"{date}_{CALC_VERSION}_{mode}.json"
    if args.email and receipt_path.exists():
        if json.loads(receipt_path.read_text()).get("status") == "SENT":
            logger.info("SKIPPED_ALREADY_SENT %s %s", date, mode)
            return 0
        raise RuntimeError("Prior email state uncertain; manual receipt review required")
    client = None
    if args.write:
        from supabase import create_client
        client = create_client(config["SUPABASE_URL"], config["SUPABASE_KEY"])
    output = run_core_funding(config["FRED_API_KEY"], client, args.output_dir,
                              as_of=args.as_of, sessions=args.sessions, refresh=args.refresh)
    print(output["summary"], flush=True)
    print(f"Chart: {output['chart']}", flush=True)
    if args.write and output["result"]["persistence_status"] != "VERIFIED":
        raise RuntimeError("Database readback not verified; email withheld")
    if args.email:
        logger.info("EMAIL_%s", send_report(config, output, receipt_path))
    return 0 if output["result"]["score"] is not None and output["chart"] else 2


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Avoid leaking credentials embedded in HTTP exception URLs.
        logger.error("CORE_FUNDING_JOB_FAILED (%s)", type(exc).__name__)
        raise SystemExit(1)
