import os
import sys

# ================= 🔐 安全挂载全局金库 =================
CONFIG_DIR = os.path.expanduser('~/market_dashboard')
if CONFIG_DIR not in sys.path:
    sys.path.append(CONFIG_DIR)

try:
    import market_config as cfg
except ImportError:
    print("❌ 致命错误：找不到 market_config.py 密钥文件！程序终止。")
    sys.exit(1)

os.environ["PYTHONWARNINGS"] = "ignore"
import warnings
warnings.filterwarnings("ignore")

import smtplib
from email.mime.text import MIMEText
from email.header import Header
from google import genai
import datetime
import argparse
import fcntl
import json
import pandas as pd
from supabase import create_client, Client

import pytz
NY_TZ = pytz.timezone('America/New_York')

# ✅ V9.0 架构升级：不再 subprocess 抓取 stdout 再用正则抠数字，
#    而是直接 import 引擎类，拿到结构化 payload 落库。
#    文案/emoji 变化不再造成字段静默变 None。
from ultimate_dashboard import UltimateDashboard
from market_utils import (
    is_trading_day,
    log_data_quality, find_missing_fields,
)
from macro_recovery import (
    RecoveryStore, HistoricalMacroProvider, sync_captures,
    discover_missing_dates, repair_pending,
)

# 落库后用于数据质量审计的关键字段
CRITICAL_FIELDS = [
    'macro_score', 'micro_score', 'eod_stress_score',
    'stress_coverage', 'stress_confidence', 'stress_calc_version',
    'vix', 'move', 'credit_spread',
    'pct_200ma', 'trin', 'dix', 'gex', 'hyg_tlt_ratio', 'credit_z', 'cg_z',
    'dix_gex_source_date', 'cot_report_date', 'concentration_quality',
]

def run_dashboard():
    """运行引擎，返回 (文本报告, 结构化payload, 中文CSV行)。任一步崩溃则抛出。"""
    app = UltimateDashboard()
    app.fetch_market_data()
    app.fetch_liquidity_and_smf()
    app.calculate_metrics()
    text = app.generate_outputs()
    payload, cn_row = app.get_db_payload(
        datetime.datetime.now(NY_TZ).strftime("%Y-%m-%d"))
    return text, payload, cn_row

def append_local_csv(cn_row, date_str):
    db_path = os.path.expanduser("~/market_dashboard/Market_History.csv")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    df_new = pd.DataFrame([cn_row], columns=list(cn_row.keys()))
    try:
        if os.path.exists(db_path):
            df_old = pd.read_csv(db_path, encoding='utf-8-sig')
            # A failed source on a rerun must not erase a previously valid cell.
            df_old = df_old.drop_duplicates(subset=['日期'], keep='last').set_index('日期')
            df_new = df_new.replace('-', float('nan')).set_index('日期')
            df_combined = df_new.combine_first(df_old).sort_index().reset_index()
            df_combined.to_csv(db_path, index=False, encoding='utf-8-sig')
        else:
            df_new.to_csv(db_path, index=False, encoding='utf-8-sig')
        print(f"📁 本地 CSV 已更新: {db_path}")
    except Exception as e:
        print(f"❌ 写入本地数据库失败: {e}")

def push_to_supabase(client, store):
    outcome = sync_captures(client, store)
    if outcome["failed"]:
        log_data_quality(
            client, job_name='auto_review_persistence', table_name='market_history',
            status='failed', rows_written=outcome['synced'],
            notes='outbox保留原始采集结果，等待后续重试',
        )
    print(json.dumps({'capture_persistence': outcome}, ensure_ascii=False))
    return outcome


def repair_summary_zh(repairs):
    if not repairs:
        return "\n【历史补缺】本轮没有待处理的历史日期。"
    lines = ["\n【历史补缺】"]
    for item in repairs:
        reasons = sorted({x.get("reason", "UNKNOWN") for x in item.get("errors", [])})
        state = "已补齐可回溯宏观项" if item["status"] == "REPAIRED" else "仍待补齐"
        lines.append(f"{item['date']}：{state}；已补 {len(item.get('filled_fields', []))} 项，"
                     f"待补 {len(item.get('remaining_fields', []))} 项。"
                     + (" 原因：" + ", ".join(reasons) if reasons else ""))
    lines.append("历史评分仅作为事后重建保存，不改写当时的原始评分。")
    return "\n".join(lines)


def generate_ai_report(market_data):
    client = genai.Client(api_key=cfg.GEMINI_API_KEY)
    prompt = f"""
    你是一位顶尖的华尔街宏观量化基金经理。请根据底层监控数据撰写【极简、冰冷、杀气腾腾】的每日复盘报告。

    【绝对红线禁令 - 违反将立即清退】：
    1. 绝不允许提及“巨头基本面”或“EPS”！
    2. 绝不允许提及“期权实操”、“做市商防线”、“Max Pain”、“期权策略(如宽跨式、Call/Put)”！
    如果你在输出中包含了上述任何词汇，报告将被视为不合格。
    宏观评分覆盖率不足100%时必须注明“宏观数据部分缺失”及覆盖率，不得将缺失项解释成零压力。宏观评分不可用时不会调用本写稿步骤。

    严格要求：总字数控制在 400-500 字左右。必须且只能输出以下三个板块（禁止自己编造新板块）：
    一、核心定性：一句话定性当前宏观象限。结合【宏观压力得分】与【盘后跨资产战术压力观察值】；后者只反映EOD跨资产压力证据，不等同于底层流动性挤兑，也不单独确认行情方向或反转。若其为N/A或质量状态为UNAVAILABLE，必须明确写“压力评分数据不足”，不得按0分解释；若状态为PARTIAL，必须披露覆盖率并降低结论强度。
    二、异动盘点：挑出最具警告意味的 2-3 个核心数据进行关联分析（只看资金流向、避险剪刀差、NFCI、以及大资金净持仓）。
    三、底顶雷达与机构防御：扫视【深度广度推力信号】、【机构暗池背离信号】及【VRP风险溢价】明确有无抄底机会。结合 11大 GICS 行业表现，指出目前应该配置防御型还是进攻型板块。

    数据如下：\n{market_data}
    """
    return client.models.generate_content(model='gemini-2.5-flash', contents=prompt).text

def send_email(subject, content, raw_data=""):
    full_email = f"{content}\n\n---------------------------------\n【数据明细】\n{raw_data}\n\n[提示：本战报由云端自动化作战中心V9.0生成]"
    msg = MIMEText(full_email, 'plain', 'utf-8')
    msg['Subject'], msg['From'], msg['To'] = Header(subject, 'utf-8'), cfg.SENDER_EMAIL, cfg.RECEIVER_EMAIL
    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=10)
        server.login(cfg.SENDER_EMAIL, cfg.APP_PASSWORD)
        server.sendmail(cfg.SENDER_EMAIL, [cfg.RECEIVER_EMAIL], msg.as_string())
        server.quit()
        print("✅ 战报邮件已通过 Gmail 火速送达邮箱！")
    except Exception as e:
        print(f"❌ 邮件异常: {e}")

def main(argv=None):
    parser = argparse.ArgumentParser(description="Partial-safe EOD macro capture and bounded historical repair")
    parser.add_argument("--no-email", action="store_true")
    parser.add_argument("--repair-only", action="store_true")
    parser.add_argument("--repair-date", action="append", default=[])
    parser.add_argument("--max-repair-dates", type=int, default=2, choices=range(1, 6))
    args = parser.parse_args(argv)
    if args.repair_date and not args.repair_only:
        parser.error("--repair-date requires --repair-only; it never relabels a live capture")
    current_time = datetime.datetime.now(NY_TZ)
    today = current_time.strftime("%Y-%m-%d")
    for day in args.repair_date:
        try:
            parsed = datetime.date.fromisoformat(day)
        except ValueError:
            parser.error("repair dates must be YYYY-MM-DD")
        if parsed > current_time.date() or (day == today and current_time.hour < 17):
            parser.error("only completed past sessions may be repaired")
        if not is_trading_day(day):
            parser.error("repair date is not an NYSE trading session")
    state_dir = os.path.expanduser("~/market_dashboard/state")
    os.makedirs(state_dir, exist_ok=True, mode=0o700)
    lock = open(os.path.join(state_dir, "auto_review.lock"), "a")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another auto_review capture/repair is running; skipped.")
        return 0
    store = RecoveryStore(os.path.join(state_dir, "macro_recovery.sqlite3"))
    data, payload = "", {}
    if not args.repair_only:
        if not is_trading_day(today):
            print("NYSE closed; no live collection.")
            return 0
        try:
            data, payload, cn_row = run_dashboard()
            if payload.get("record_date") != today:
                raise RuntimeError("CAPTURE_CROSSED_MARKET_DATE")
            store.stage(payload)
            full = payload.get("full_metrics") or {}
            cn_row['[质量]宏观评分状态'] = full.get("macro_score_status", "UNAVAILABLE")
            cn_row['[质量]宏观覆盖率'] = full.get("macro_score_coverage")
            append_local_csv(cn_row, today)
        except Exception as exc:
            message = "引擎异常，未生成可保存的完整采集对象：" + type(exc).__name__
            print(message)
            if not args.no_email:
                send_email("宏观采集异常 - " + today, message)
            return 1
    try:
        client = create_client(cfg.SUPABASE_URL, cfg.SUPABASE_KEY)
        persisted = push_to_supabase(client, store)
        if persisted["failed"]:
            raise RuntimeError("PERSISTENCE_PENDING")
        if args.repair_date:
            for day in args.repair_date:
                store.enqueue(day)
        else:
            # Pending dates remain in SQLite even after leaving this discovery window.
            discover_missing_dates(client, store, today, is_trading_day)
        provider = HistoricalMacroProvider(cfg.FRED_API_KEY)
        for error in (payload.get("full_metrics") or {}).get("source_errors", []):
            if error.get("reason") in ("UPSTREAM_MAINTENANCE", "NON_JSON_RESPONSE", "NETWORK_ERROR"):
                provider.fred.unavailable_reason = error["reason"]
                break
        before = str(current_time.date() + datetime.timedelta(days=1)) if args.repair_date else today
        repairs = repair_pending(client, store, provider, before, args.max_repair_dates)
        print(json.dumps({"historical_repair": repairs}, ensure_ascii=False))
    except Exception as exc:
        message = ("数据库同步或补缺流程异常；本次已采集数据保存在本地 outbox，"
                   "下次自动重试：" + type(exc).__name__)
        print(message)
        if not args.no_email:
            send_email("宏观数据待同步 - " + today, message, data)
        return 1
    if args.repair_only:
        return 0
    full = payload.get("full_metrics") or {}
    missing_fields = find_missing_fields(payload, CRITICAL_FIELDS)
    partial = full.get("macro_score_status") != "OK" or bool(full.get("source_errors")) or bool(missing_fields)
    log_data_quality(
        client, job_name='auto_review_capture', table_name='market_history',
        status='partial' if partial else 'ok', rows_written=1,
        missing_fields=missing_fields,
        notes=f"record_date={today}; macro_status={full.get('macro_score_status')}; coverage={full.get('macro_score_coverage')}",
    )
    repair_text = repair_summary_zh(repairs)
    if payload.get("macro_score") is None:
        report = (
            "【部分数据已保存】有效字段已落库并读回核验；缺失字段为 NULL，等待下次自动补缺。\n"
            f"宏观评分不可用，覆盖率 {float(full.get('macro_score_coverage') or 0):.0%}，"
            "仍执行原有 70% 门槛。本次不生成宏观交易结论。\n"
            "历史补算会单独标记为事后重建，不替换当时的宏观评分。"
        )
    elif args.no_email:
        report = "数据已保存；--no-email 同时跳过 AI 写稿。"
    else:
        try:
            report = generate_ai_report(data)
        except Exception as exc:
            print("AI report unavailable: " + type(exc).__name__)
            report = "数据已保存，自动写稿不可用；请查看带质量标记的数据明细。"
    subject = ("部分数据 / " if partial else "") + "美股宏观战报 - " + today
    if not args.no_email:
        send_email(subject, report + repair_text, data)
    print(report + repair_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
