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
import pandas as pd
from supabase import create_client, Client

import pytz
NY_TZ = pytz.timezone('America/New_York')

# ✅ V9.0 架构升级：不再 subprocess 抓取 stdout 再用正则抠数字，
#    而是直接 import 引擎类，拿到结构化 payload 落库。
#    文案/emoji 变化不再造成字段静默变 None。
from ultimate_dashboard import UltimateDashboard
from market_utils import (
    is_trading_day, attach_metadata, safe_upsert,
    log_data_quality, find_missing_fields,
)

# 落库后用于数据质量审计的关键字段
CRITICAL_FIELDS = [
    'macro_score', 'micro_score', 'vix', 'move', 'credit_spread',
    'pct_200ma', 'trin', 'dix', 'gex', 'hyg_tlt_ratio', 'credit_z', 'cg_z',
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
            df_combined = pd.concat([df_old, df_new], ignore_index=True)
            df_combined = df_combined.drop_duplicates(subset=['日期'], keep='last')
            df_combined.to_csv(db_path, index=False, encoding='utf-8-sig')
        else:
            df_new.to_csv(db_path, index=False, encoding='utf-8-sig')
        print(f"📁 本地 CSV 已更新: {db_path}")
    except Exception as e:
        print(f"❌ 写入本地数据库失败: {e}")

def push_to_supabase(payload, date_str):
    print("🔗 准备同步结构化数据至 Supabase (market_history)...")
    try:
        supabase: Client = create_client(cfg.SUPABASE_URL, cfg.SUPABASE_KEY)
    except Exception as e:
        print(f"❌ Supabase 连接失败: {e}")
        return

    attach_metadata(payload, source_date=date_str)
    res = safe_upsert(supabase, 'market_history', payload, conflict_cols='record_date')

    missing = find_missing_fields(payload, CRITICAL_FIELDS)
    status = 'failed' if res is None else ('partial' if missing else 'ok')
    log_data_quality(
        supabase, job_name='auto_review', table_name='market_history',
        status=status, rows_written=0 if res is None else 1,
        missing_fields=missing,
        notes='结构化直写 (V9.0)，正则链路已废弃',
    )
    if res is not None:
        print(f"✅ market_history 结构化直写成功！关键字段缺失: {missing or '无'}")

def generate_ai_report(market_data):
    client = genai.Client(api_key=cfg.GEMINI_API_KEY)
    prompt = f"""
    你是一位顶尖的华尔街宏观量化基金经理。请根据底层监控数据撰写【极简、冰冷、杀气腾腾】的每日复盘报告。

    【绝对红线禁令 - 违反将立即清退】：
    1. 绝不允许提及“巨头基本面”或“EPS”！
    2. 绝不允许提及“期权实操”、“做市商防线”、“Max Pain”、“期权策略(如宽跨式、Call/Put)”！
    如果你在输出中包含了上述任何词汇，报告将被视为不合格。

    严格要求：总字数控制在 400-500 字左右。必须且只能输出以下三个板块（禁止自己编造新板块）：
    一、核心定性：一句话定性当前宏观象限。必须结合【宏观压力得分】和全新的【微观战术得分】（注：微观得分越高代表底层流动性挤兑和恐慌越严重）。
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

if __name__ == "__main__":
    current_time = datetime.datetime.now(NY_TZ)
    today_str = current_time.strftime("%Y-%m-%d")

    print(f"\n[{current_time.strftime('%H:%M:%S')}] 🚀 开始执行云端自动化投研流 (结构化直写 V9.0)...")

    if not is_trading_day():
        print("🛑 今天是美股休市日，云端战报机自动停止抓取与发稿。")
        sys.exit(0)

    try:
        data, payload, cn_row = run_dashboard()
    except Exception as e:
        print(f"❌ 引擎运行崩溃，触发熔断保护。不写库，直接发警报邮件。({e})")
        send_email(f"⚠️ 美股监控器异常报告 (云端) - {today_str}",
                   f"云端监控系统在抓取数据时发生崩溃：{e}\n请检查云服务器上的 ultimate_dashboard.py 是否正常运行。")
        sys.exit(1)

    # 熔断：核心分数缺失视为引擎异常
    if payload.get('macro_score') is None:
        print("❌ 检测到数据异常（宏观得分缺失），触发熔断保护。")
        send_email(f"⚠️ 美股监控器异常报告 (云端) - {today_str}",
                   "引擎运行完成但宏观得分缺失，疑似上游数据源故障。本次不写库。")
        sys.exit(1)

    append_local_csv(cn_row, today_str)
    push_to_supabase(payload, today_str)

    print("🧠 尝试连接 AI 生成战报...")
    try:
        report = generate_ai_report(data)
    except Exception as e:
        print(f"⚠️ 无法连接至 Google AI: {e}")
        report = "【系统提示】\n由于当前云端网络受到 Google API 访问限制，本次自动写稿失败。\n但底层的宏观数据已成功入库，请直接查阅下方的【数据明细】。"

    send_email(f"美股宏观战报 (机构级雷达V9.0) - {today_str}", report, data)
    print("🎉 收工！云端定时任务圆满完成。")
