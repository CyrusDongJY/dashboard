import sys
import os
import time
import logging
import calendar
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from email.header import Header

import pytz
import pandas_market_calendars as mcal
NY_TZ = pytz.timezone('America/New_York')

# ================= 🔐 安全挂载 =================
CONFIG_DIR = os.path.expanduser('~/market_dashboard')
if CONFIG_DIR not in sys.path: 
    sys.path.append(CONFIG_DIR)

try:
    import market_config as cfg
    from market_probes import scan_macro_regime, scan_micro_options, scan_volatility_spot
    from market_utils import trading_days_back, log_data_quality
    from anomaly_engine import run_engine, format_matrix
    from environment_indices import compute_indices, format_env_summary
    from environment_report import generate_environment_chart
except ImportError as e:
    print(f"❌ 致命错误：缺少核心配置文件或探针库 ({e})！")
    sys.exit(1)

from supabase import create_client, Client
from google import genai

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s', datefmt='%H:%M:%S')

def is_trading_day():
    try:
        today_str = datetime.now(NY_TZ).strftime('%Y-%m-%d')
        nyse = mcal.get_calendar('NYSE')
        schedule = nyse.schedule(start_date=today_str, end_date=today_str)
        return not schedule.empty
    except Exception as e:
        logging.warning(f"日历检测异常: {e}")
        return False

def get_days_to_opex(current_date):
    c = calendar.Calendar(firstweekday=calendar.SUNDAY)
    monthcal = c.monthdatescalendar(current_date.year, current_date.month)
    fridays = [day for week in monthcal for day in week if day.weekday() == calendar.FRIDAY and day.month == current_date.month]
    opex_date = fridays[2] if len(fridays) >= 3 else fridays[-1]
    if current_date.date() > opex_date:
        next_month = current_date.month + 1 if current_date.month < 12 else 1
        next_year = current_date.year if current_date.month < 12 else current_date.year + 1
        monthcal = c.monthdatescalendar(next_year, next_month)
        fridays = [day for week in monthcal for day in week if day.weekday() == calendar.FRIDAY and day.month == next_month]
        opex_date = fridays[2] if len(fridays) >= 3 else fridays[-1]
    return (opex_date - current_date.date()).days, opex_date

def generate_ai_report(macro_info, vol_info, micro_info, opex_context, anomaly_matrix_text):
    client = genai.Client(api_key=cfg.GEMINI_API_KEY)

    system_prompt = """
    你是一位服务于华尔街顶级对冲基金的首席宏观量化策略师。
    系统已经通过【多窗口异常矩阵引擎】完成客观判定：每条异常带有 severity(0-3)、置信度、
    观察窗口(1D/5D/21D)与统计基准(252D z-score/分位)，并标注了数据滞后天数。

    【撰写军规】：
    1. 你的职责是"叙述与归因"，不是"重新判定"。严禁推翻或加码矩阵给出的 severity；
       低置信度(<60%)或高滞后(⏳)的异常必须在行文中明示其局限。
    2. 逻辑链条：先讲跨资产共振（如有），再按 severity 从高到低归因单指标异常，
       最后结合探针快照补充盘面色彩。
    3. 拒绝废话：矩阵未报警的维度直接略过；若矩阵为空，明确说"今日无客观异常"，不得编造风险。
    4. 战术指引：结尾给出与 severity 等级匹配的机构级应对（无异常=常规仓位，共振3级=防御指引）。
    """

    data_feed = f"""
    {opex_context}

    {anomaly_matrix_text}

    === 探针背景快照（供叙述用，不作为判定依据） ===
    【探针一：宏观大势与极值信号】 {macro_info['alert_text']}
    {macro_info['text']}
    【探针二：波动率结构与市场广度】 {vol_info['alert_text']}
    {vol_info['text']}
    【探针三：微观筹码与期权防线】 {micro_info['alert_text']}
    {micro_info['text']}
    """
    
    for attempt in range(3):
        try:
            response = client.models.generate_content(model='gemini-2.5-flash', contents=f"{system_prompt}\n\n【情报输入】:\n{data_feed}")
            return response.text, data_feed
        except Exception as e:
            if attempt < 2: 
                time.sleep(5 * (2 ** attempt))
                continue
            return f"🚨 AI 推演引擎掉线: {e}", data_feed

def format_raw_appendix(macro_raw, vol_raw, micro_raw_list):
    appendix = "=== 🗄️ 全息数据库快照 (用于交易员交叉验证) ===\n\n"
    
    appendix += "【一、宏观流动性与大势全息扫描】\n"
    full_metrics = macro_raw.get('full_metrics', {})
    if full_metrics:
        for k, v in full_metrics.items():
            appendix += f"{k}: {v}\n"
    else:
        for k, v in macro_raw.items():
            if k not in ['id', 'created_at', 'full_metrics']: appendix += f"{k}: {v}\n"
            
    appendix += "\n【二、现货结构与广度损耗全息扫描】\n"
    for k, v in vol_raw.items():
        if k not in ['id', 'created_at']: appendix += f"{k}: {v}\n"

    appendix += "\n【三、十大核心标的期权与暗池明细全息扫描】\n"
    for stock in micro_raw_list:
        sym = stock.get('ticker', 'UNKNOWN')
        appendix += f"[{sym}]: "
        details = []
        if stock.get('current_price'): details.append(f"现价 ${stock['current_price']:.2f}")
        if stock.get('poc_price'): details.append(f"POC ${stock['poc_price']:.2f}")
        if stock.get('dpsv_pct'): details.append(f"FINRA短售量代理 {stock['dpsv_pct']}%")
        if stock.get('ivr_pct'): details.append(f"IVR {stock['ivr_pct']}%")
        if stock.get('zgl_price'): details.append(f"ZGL分水岭 ${stock['zgl_price']:.2f}")
        if stock.get('charm_m'): details.append(f"Charm推力 {stock['charm_m']}M")
        appendix += " | ".join(details) + "\n"
        
    return appendix

def send_email(subject, ai_report, raw_data_feed, raw_appendix, image_path=None):
    msg = MIMEMultipart()
    msg['From'] = cfg.SENDER_EMAIL
    msg['To'] = cfg.RECEIVER_EMAIL
    msg['Subject'] = Header(subject, 'utf-8')
    
    full_content = f"{ai_report}\n\n" + "="*50 + f"\n\n【机密附件一：探针异常报警清单】\n{raw_data_feed}\n\n" + "="*50 + f"\n\n{raw_appendix}"
    msg.attach(MIMEText(full_content, 'plain', 'utf-8'))
    if image_path and os.path.exists(image_path):
        with open(image_path, 'rb') as image_file:
            image = MIMEImage(image_file.read(), _subtype='png')
        image.add_header('Content-Disposition', 'attachment',
                         filename=os.path.basename(image_path))
        msg.attach(image)
    
    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=15)
        server.login(cfg.SENDER_EMAIL, cfg.APP_PASSWORD)
        server.sendmail(cfg.SENDER_EMAIL, [cfg.RECEIVER_EMAIL], msg.as_string())
        server.quit()
        logging.info("✅ 终极复盘战报(带全息附录)已送达！")
    except Exception as e:
        logging.error(f"❌ 邮件发送失败: {e}")

if __name__ == "__main__":
    logging.info(">>> 启动 V9.1 全域量化盘后复盘流水线 (异常矩阵 + 环境影子图) <<<")
    
    if not is_trading_day():
        logging.info("🛑 今天是美股休市日，Auto Analyst 进入法定休眠。")
        sys.exit(0)
    
    # ✅ 修复重点：正确使用 cfg.SUPABASE_URL 并且补充完整的报错日志！
    try: 
        supabase: Client = create_client(cfg.SUPABASE_URL, cfg.SUPABASE_KEY)
        logging.info("✅ 数据库连接成功！")
    except Exception as e: 
        logging.error(f"❌ 数据库连接致命错误: {e}")
        sys.exit(1)
        
    current_time = datetime.now(NY_TZ)
    report_date = current_time.strftime('%Y-%m-%d')
    # ✅ V9.0：数据拉取范围按交易日计（280个交易日，覆盖252D统计基准）。
    #    1D/5D/21D 只是观察窗口，由异常引擎与探针内部各自截取，不再用自然日 timedelta。
    cutoff_date = trading_days_back(280)
    days_to_opex, opex_date = get_days_to_opex(current_time)
    opex_context = f"📅 【时间轴锚点】：今日距离最近的期权交割日（OPEX: {opex_date.strftime('%Y-%m-%d')}）还有 {days_to_opex} 天。"

    logging.info("🧮 运行多窗口异常矩阵引擎 (252D基准 / 1D-5D-21D观察 / 跨资产共振)...")
    try:
        anomaly_events, snapshot = run_engine(
            supabase, report_date=report_date, persist=True,
            session="EOD", is_final=True)
        anomaly_matrix_text = format_matrix(anomaly_events)
        max_sev = max([e.severity for e in anomaly_events], default=0)
        log_data_quality(supabase, job_name='auto_analyst', table_name='anomaly_events',
                         status='ok', rows_written=len(anomaly_events),
                         anomaly_count=len(anomaly_events),
                         notes=f"最高severity={max_sev}")
        logging.info(f"✅ 异常矩阵生成：{len(anomaly_events)} 条 (最高severity={max_sev})")
    except Exception as e:
        anomaly_matrix_text = f"⚠️ 异常矩阵引擎运行失败: {e}（本报告降级为纯探针模式）"
        logging.error(anomaly_matrix_text)
        anomaly_events = []

    # 环境指数严格处于影子模式：只进入文本附录和PNG，不接入告警门控或AI判定。
    environment_text = "=== 环境指数（影子观察） ===\n本日计算失败或数据不足。"
    environment_chart = None
    try:
        environment = compute_indices(
            supabase, report_date=report_date, persist=True,
            events=anomaly_events, session="EOD")
        environment_text = format_env_summary(environment)
        environment_chart = generate_environment_chart(supabase, report_date)
        logging.info("✅ 环境影子报告生成完成（不参与预警）")
    except Exception as e:
        logging.warning(f"环境影子报告降级为无图模式: {e}")

    logging.info("📡 唤醒三大联邦探针...")
    macro_text, macro_alert, macro_raw = scan_macro_regime(supabase, cutoff_date)
    vol_text, vol_alert, vol_raw = scan_volatility_spot(supabase, cutoff_date)
    micro_text, micro_alert, micro_raw_list = scan_micro_options(supabase, cutoff_date)

    macro_info = {"text": macro_text, "alert_text": f"(危险指数: {macro_alert}/5)"}
    vol_info = {"text": vol_text, "alert_text": f"(危险指数: {vol_alert}/5)"}
    micro_info = {"text": micro_text, "alert_text": f"(危险指数: {micro_alert}/5)"}

    logging.info("🧠 呼叫 Gemini 进行深度逻辑融合...")
    ai_report, raw_data_feed = generate_ai_report(macro_info, vol_info, micro_info, opex_context, anomaly_matrix_text)
    
    logging.info("🗄️ 正在组装全息数据附录...")
    raw_appendix = format_raw_appendix(macro_raw, vol_raw, micro_raw_list)
    raw_appendix = f"{environment_text}\n\n" + "="*50 + f"\n\n{raw_appendix}"
    
    logging.info("🚀 推送最终战报...")
    send_email(f"🚨 机构级全域交叉复盘 (V9.1 影子环境监测) [{report_date}]",
               ai_report, raw_data_feed, raw_appendix, image_path=environment_chart)
    logging.info(">>> 流水线执行完毕，司令部休眠 <<<")
