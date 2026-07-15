"""
============================================================
 market_sentinel.py — 全局异常哨兵 (V9.0)
============================================================
职责与其它程序彻底解耦：

  抓取程序(daily_pre_market / daily_post_close / auto_review)只管写库；
  本哨兵在数据落库后跨所有表跑 anomaly_engine，
  只在异常越过【预警红线 ALERT_GATE】时主动发预警邮件，
  并把每次评估结果写回 anomaly_events / sentinel_runs 供回测校准。

设计要点：
  1. 单一红线配置 ALERT_GATE —— 所有"发不发邮件"的判断集中在这里，可回测、可调参。
  2. 去重防轰炸 —— 同一交易日同一"预警指纹"只发一次（记录在 sentinel_runs）。
  3. 降级不静默 —— 数据缺失/引擎异常时发降级提示，绝不"无声通过"。

用法：
  python3 market_sentinel.py            # 正常：非交易日自动跳过
  python3 market_sentinel.py now        # 强制运行（调试用，绕过交易日闸门）
  python3 market_sentinel.py dry        # 只评估打印，不发邮件、不写库
============================================================
"""
import os
import sys

CONFIG_DIR = os.path.expanduser('~/market_dashboard')
if CONFIG_DIR not in sys.path:
    sys.path.append(CONFIG_DIR)

os.environ["PYTHONWARNINGS"] = "ignore"
import warnings
warnings.filterwarnings("ignore")

import smtplib
import logging
import hashlib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.header import Header

import pytz
import pandas_market_calendars as mcal
from supabase import create_client, Client

try:
    import market_config as cfg
    from anomaly_engine import run_engine, format_matrix
    from market_utils import log_data_quality
except ImportError as e:
    print(f"❌ 致命错误：共享模块缺失 ({e})。请确认 market_config.py / anomaly_engine.py / "
          f"market_utils.py 均在 ~/market_dashboard/ 下。")
    sys.exit(1)

NY_TZ = pytz.timezone('America/New_York')
supabase: Client = create_client(cfg.SUPABASE_URL, cfg.SUPABASE_KEY)

logger = logging.getLogger("sentinel")
logger.setLevel(logging.INFO)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s', datefmt='%H:%M:%S'))
    logger.addHandler(ch)


# ============================================================
#  🚦 预警红线 —— 唯一的"发不发邮件"判定标准（稳健档）
#  改这里就能调灵敏度，无需动引擎逻辑。
#  回测方法：anomaly_events 表存了每天所有异常（含未触发预警的），
#  可统计"若把某项调成 X，过去 N 天会发多少封"，用数据校准而非拍脑袋。
# ============================================================
ALERT_GATE = {
    # --- 单指标极端：任意一条 sev>=此值 且置信达标 → 立即预警 ---
    "solo_severity": 3,          # 稳健档=3（只有极端值单独触发）；激进档可设 2
    "solo_min_confidence": 0.50, # 单指标预警的最低置信度

    # --- 聚集：多条中度异常同现 → 预警（防单点误报）---
    "cluster_severity": 2,       # 计入聚集的最低 severity
    "cluster_count": 2,          # 达到几条才触发
    "cluster_min_confidence": 0.50,

    # --- 结构化信号：命中即预警（这两类本身就是多指标确认，价值最高）---
    "resonance_always": True,    # 任意跨资产共振必报
    "multiwindow_always": True,  # 任意多窗口确认必报

    # --- 全局置信度地板：低于此的异常只入库备查，永不触发邮件 ---
    "hard_confidence_floor": 0.35,
}


def is_trading_day():
    try:
        today = datetime.now(NY_TZ).strftime('%Y-%m-%d')
        sched = mcal.get_calendar('NYSE').schedule(start_date=today, end_date=today)
        return not sched.empty
    except Exception as e:
        logger.warning(f"交易日历检测异常，默认放行: {e}")
        return True


def send_email(subject, body):
    msg = MIMEText(body, 'plain', 'utf-8')
    msg['From'] = cfg.SENDER_EMAIL
    msg['To'] = cfg.RECEIVER_EMAIL
    msg['Subject'] = Header(subject, 'utf-8')
    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=15)
        server.login(cfg.SENDER_EMAIL, cfg.APP_PASSWORD)
        server.sendmail(cfg.SENDER_EMAIL, [cfg.RECEIVER_EMAIL], msg.as_string())
        server.quit()
        return True
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")
        return False


# ============================================================
#  红线评估：把异常矩阵映射为"触发原因"清单
# ============================================================
def evaluate_gate(events):
    """返回 (should_alert: bool, reasons: list[str], triggering: list[event])。
    只有越过 ALERT_GATE 的异常才进入预警；其余仅入库备查。"""
    reasons = []
    triggering = []

    # 先按硬地板过滤：置信度太低的一律不参与预警判定
    eligible = [e for e in events if e.confidence >= ALERT_GATE["hard_confidence_floor"]]

    # 1) 结构化信号：共振
    if ALERT_GATE["resonance_always"]:
        res = [e for e in eligible if e.layer == "resonance"]
        if res:
            for e in res:
                reasons.append(f"跨资产共振：{e.explanation}")
                triggering.append(e)

    # 2) 结构化信号：多窗口确认
    if ALERT_GATE["multiwindow_always"]:
        mw = [e for e in eligible if e.layer == "multiwindow"]
        if mw:
            for e in mw:
                reasons.append(f"多窗口确认：{e.explanation}")
                triggering.append(e)

    # 3) 单指标极端
    solo = [e for e in eligible
            if e.layer == "single"
            and e.severity >= ALERT_GATE["solo_severity"]
            and e.confidence >= ALERT_GATE["solo_min_confidence"]]
    for e in solo:
        reasons.append(f"单指标极端 (sev{e.severity})：{e.explanation}")
        triggering.append(e)

    # 4) 中度异常聚集
    cluster = [e for e in eligible
               if e.layer == "single"
               and e.severity >= ALERT_GATE["cluster_severity"]
               and e.confidence >= ALERT_GATE["cluster_min_confidence"]]
    if len(cluster) >= ALERT_GATE["cluster_count"]:
        names = "、".join(sorted({e.explanation.split(" ")[0] for e in cluster}))
        reasons.append(f"中度异常聚集：{len(cluster)} 条 sev≥{ALERT_GATE['cluster_severity']} 同现 [{names}]")
        # 聚集触发时，把这些也纳入触发集（去重在下游做）
        triggering.extend(cluster)

    # 去重（同一 event 可能被多条规则命中）
    seen = set()
    uniq = []
    for e in triggering:
        k = (e.metric, e.scope, e.window, e.layer)
        if k not in seen:
            seen.add(k)
            uniq.append(e)

    return (len(reasons) > 0), reasons, uniq


def alert_fingerprint(report_date, triggering):
    """预警指纹：同一交易日、同一组触发指标只发一次，防止多次落库触发重复邮件。"""
    key = report_date + "|" + "|".join(sorted(
        f"{e.metric}:{e.scope}:{e.severity}" for e in triggering))
    return hashlib.md5(key.encode('utf-8')).hexdigest()[:16]


def already_alerted(report_date, fingerprint):
    """查 sentinel_runs：今天这个指纹是否已发过预警。"""
    try:
        res = (supabase.table('sentinel_runs')
               .select('fingerprint')
               .eq('report_date', report_date)
               .eq('fingerprint', fingerprint)
               .eq('alerted', True)
               .limit(1).execute())
        return bool(res.data)
    except Exception as e:
        logger.warning(f"去重查询失败（按未发过处理）: {e}")
        return False


def record_run(report_date, fingerprint, alerted, n_events, reasons, dry):
    if dry:
        return
    try:
        supabase.table('sentinel_runs').upsert({
            'report_date': report_date,
            'fingerprint': fingerprint,
            'alerted': alerted,
            'n_events': n_events,
            'reasons': " | ".join(reasons) if reasons else None,
            'ran_at': datetime.now(timezone.utc).isoformat(),
        }, on_conflict='report_date,fingerprint').execute()
    except Exception as e:
        logger.warning(f"sentinel_runs 记录失败: {e}")


def build_alert_body(report_date, reasons, triggering, all_events):
    lines = [
        f"🚨 全局哨兵预警 | {report_date}",
        "=" * 44,
        "",
        "【触发原因】",
    ]
    for i, r in enumerate(reasons, 1):
        lines.append(f"  {i}. {r}")
    lines += [
        "",
        f"【触发指标明细】（{len(triggering)} 项越过红线）",
    ]
    for e in triggering:
        scope = "" if e.scope in ("MACRO", None) else f"[{e.scope}] "
        lag = f" ⏳滞后{e.lag_days}d" if e.lag_days else ""
        lines.append(f"  🔴 {scope}{e.explanation}  (sev{e.severity}/3, 置信{int(e.confidence*100)}%{lag})")
    lines += [
        "",
        "-" * 44,
        "【完整异常矩阵】（含未触发预警项，供人工研判）",
        format_matrix(all_events),
        "",
        "-" * 44,
        f"红线档位：稳健档 | 单指标≥sev{ALERT_GATE['solo_severity']} 或 "
        f"≥{ALERT_GATE['cluster_count']}条sev≥{ALERT_GATE['cluster_severity']} 或 共振/多窗口确认",
        "如需调灵敏度：改 market_sentinel.py 顶部 ALERT_GATE，用 anomaly_events 表回测。",
    ]
    return "\n".join(lines)


def main():
    args = [a.lower() for a in sys.argv[1:]]
    force = 'now' in args
    dry = 'dry' in args

    if not force and not dry and not is_trading_day():
        logger.info("🛑 非交易日，哨兵跳过。")
        return

    report_date = datetime.now(NY_TZ).strftime('%Y-%m-%d')
    snapshot_session = "PRE" if datetime.now(NY_TZ).hour < 12 else "POST"
    logger.info(f"🛰️  全局哨兵启动 | {report_date} | {'DRY-RUN' if dry else '实盘'}")

    # ---- 跑引擎（dry 时不落库 anomaly_events）----
    try:
        events, snapshot = run_engine(
            supabase, report_date=report_date, persist=not dry,
            session=snapshot_session, is_final=False)
    except Exception as e:
        logger.error(f"异常引擎运行失败: {e}")
        # 降级不静默：引擎崩了也要通知，避免"以为没异常"
        if not dry:
            send_email(f"⚠️ 哨兵降级 | 异常引擎故障 [{report_date}]",
                       f"anomaly_engine 运行抛错，本交易日未能完成全局审查：\n\n{e}\n\n"
                       f"请检查数据表与依赖。")
        return

    logger.info(f"引擎产出 {len(events)} 条异常。")

    # ---- 数据覆盖度自检：一条都没读到 → 大概率是上游没写库 ----
    if not snapshot or all(v is None for v in snapshot.values()):
        logger.warning("快照为空，疑似上游数据缺失。")
        if not dry:
            send_email(f"⚠️ 哨兵降级 | 数据缺失 [{report_date}]",
                       "全局审查读不到任何有效指标，疑似当日抓取程序未写库或写库延迟。\n"
                       "请检查 market_history / macro_spot_daily / 盘前盘后表。")
            log_data_quality(supabase, 'market_sentinel', 'anomaly_events',
                             status='failed', rows_written=0,
                             notes='snapshot empty — 上游疑似未写库')
        return

    # ---- 红线评估 ----
    should_alert, reasons, triggering = evaluate_gate(events)

    print("\n" + format_matrix(events) + "\n")

    if not should_alert:
        logger.info("✅ 无异常越过预警红线，仅入库备查，不发邮件。")
        record_run(report_date, "clean", alerted=False, n_events=len(events),
                   reasons=[], dry=dry)
        if not dry:
            log_data_quality(supabase, 'market_sentinel', 'anomaly_events',
                             status='ok', rows_written=len(events),
                             anomaly_count=len(events), notes='no alert')
        return

    # ---- 去重防轰炸 ----
    fp = alert_fingerprint(report_date, triggering)
    if not dry and already_alerted(report_date, fp):
        logger.info(f"🔁 相同预警指纹今日已发过 (fp={fp})，跳过重复邮件。")
        return

    body = build_alert_body(report_date, reasons, triggering, events)
    logger.warning(f"🚨 触发预警：{len(reasons)} 条原因，{len(triggering)} 项指标越线。")

    if dry:
        print("=" * 44 + "\n[DRY-RUN] 以下为将发送的预警邮件正文：\n" + "=" * 44)
        print(body)
        return

    ok = send_email(f"🚨 全局哨兵预警 [{report_date}] {len(triggering)}项越线", body)
    record_run(report_date, fp, alerted=ok, n_events=len(events), reasons=reasons, dry=dry)
    log_data_quality(supabase, 'market_sentinel', 'anomaly_events',
                     status='ok' if ok else 'partial', rows_written=len(events),
                     anomaly_count=len(triggering),
                     notes=f"{len(triggering)} triggers, sent={ok}")
    logger.info("邮件已发送。" if ok else "邮件发送失败，已记录。")


if __name__ == "__main__":
    main()
