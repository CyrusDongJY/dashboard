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
if CONFIG_DIR in sys.path:
    sys.path.remove(CONFIG_DIR)
sys.path.insert(0, CONFIG_DIR)

try:
    import market_config as cfg
    from market_probes import scan_macro_regime, scan_micro_options, scan_volatility_spot
    from market_utils import trading_days_back, log_data_quality
    from anomaly_engine import run_engine, format_matrix
    from environment_indices import compute_indices, format_env_summary
    from environment_report import generate_environment_chart
    from liquidity_monitor import compute_liquidity_monitor, format_liquidity_summary
    from liquidity_report import build_liquidity_history, generate_liquidity_chart
    from liquidity_sources import OfficialLiquiditySources
    from tactical_stress import format_tactical_stress_summary
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
    event_row = next((
        stock for stock in micro_raw_list
        if stock.get('expected_move_event_name')
        or stock.get('expected_move_event_status')
    ), None)
    if event_row:
        event_name = event_row.get('expected_move_event_name')
        event_status = event_row.get('expected_move_event_status')
        if event_name:
            event_at = event_row.get('expected_move_event_at')
            try:
                event_at = datetime.fromisoformat(str(event_at)).astimezone(
                    NY_TZ).strftime('%Y-%m-%d %H:%M ET')
            except (TypeError, ValueError):
                event_at = '时间未记录'
            event_days = event_row.get('expected_move_event_trading_days')
            event_risk = event_row.get('expected_move_event_risk') or '未评级'
            appendix += (
                f"【已核验重大事件】{event_name} | {event_at} | "
                f"距离{event_days if event_days is not None else 'NA'}个交易日 | "
                f"风险{event_risk}\n")
        elif event_status == 'EVENT_CALENDAR_UNAVAILABLE':
            appendix += (
                "【重大事件日历】EVENT_CALENDAR_UNAVAILABLE | "
                "Expected Move事件分项不可用\n")
    for stock in micro_raw_list:
        sym = stock.get('ticker', 'UNKNOWN')
        appendix += f"[{sym}]: "
        details = []
        pre_ref = stock.get('premarket_reference_price', stock.get('current_price'))
        if pre_ref:
            source = stock.get('premarket_price_source', '兼容字段')
            details.append(f"盘前参考价 ${pre_ref:.2f} ({source})")
        if stock.get('previous_close'):
            details.append(f"昨收 ${stock['previous_close']:.2f}")
        if stock.get('poc_price'): details.append(f"POC ${stock['poc_price']:.2f}")
        if stock.get('dpsv_pct'): details.append(f"FINRA短售量代理 {stock['dpsv_pct']}%")
        if stock.get('ivr_pct'): details.append(f"IVR {stock['ivr_pct']}%")
        gamma_quality = stock.get('gamma_quality')
        gamma_eligible = stock.get('gamma_decision_eligible')
        if gamma_eligible is None:
            gamma_eligible = (
                isinstance(gamma_quality, dict)
                and gamma_quality.get('ALL') == 'OK')
        if gamma_eligible:
            gamma_flip = stock.get('gamma_flip_all', stock.get('zgl_price'))
            if gamma_flip:
                details.append(f"主Gamma Flip观察位 ${gamma_flip:.2f}")
            if stock.get('charm_m'):
                details.append(f"Charm推力 {stock['charm_m']}M")
        else:
            details.append(
                "Gamma数据不足（战术权重0；期权OI仅作价格活动区）")
        appendix += " | ".join(details) + "\n"
        
    return appendix

def send_email(subject, ai_report, raw_data_feed, raw_appendix,
               image_path=None, image_paths=None):
    msg = MIMEMultipart()
    msg['From'] = cfg.SENDER_EMAIL
    msg['To'] = cfg.RECEIVER_EMAIL
    msg['Subject'] = Header(subject, 'utf-8')
    
    full_content = f"{ai_report}\n\n" + "="*50 + f"\n\n【机密附件一：探针异常报警清单】\n{raw_data_feed}\n\n" + "="*50 + f"\n\n{raw_appendix}"
    msg.attach(MIMEText(full_content, 'plain', 'utf-8'))
    attachments = []
    if image_path:
        attachments.append(image_path)
    attachments.extend(image_paths or [])
    seen = set()
    for path in attachments:
        if not path or path in seen:
            continue
        seen.add(path)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            with open(path, 'rb') as image_file:
                image = MIMEImage(image_file.read(), _subtype='png')
            image.add_header('Content-Disposition', 'attachment',
                             filename=os.path.basename(path))
            msg.attach(image)
            logging.info(
                f"📎 PNG报告已加入邮件附件: {path} "
                f"({os.path.getsize(path) / 1024:.1f} KB)")
        else:
            logging.warning(f"⚠️ PNG附件不存在或为空，将跳过该附件: {path}")
    
    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=15)
        server.login(cfg.SENDER_EMAIL, cfg.APP_PASSWORD)
        server.sendmail(cfg.SENDER_EMAIL, [cfg.RECEIVER_EMAIL], msg.as_string())
        server.quit()
        logging.info("✅ 终极复盘战报(带全息附录)已送达！")
        return True
    except Exception as e:
        logging.error(f"❌ 邮件发送失败: {e}")
        return False

if __name__ == "__main__":
    logging.info(">>> 启动 V9.2 全域量化盘后复盘流水线 (环境 + 流动性影子监测) <<<")
    
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
    try:
        environment = compute_indices(
            supabase, report_date=report_date, persist=True,
            events=anomaly_events, session="EOD")
        environment_text = format_env_summary(environment)
    except Exception as e:
        logging.warning(f"⚠️ 环境指数计算失败，文本摘要降级: {e}")

    environment_chart = None
    report_dir = os.path.expanduser(
        getattr(cfg, "REPORT_DIR", os.path.join(CONFIG_DIR, "reports")))
    chart_output_path = os.path.join(
        report_dir, f"environment_shadow_{report_date}.png")
    try:
        environment_chart = generate_environment_chart(
            supabase, report_date, output_path=chart_output_path)
        if environment_chart and os.path.isfile(environment_chart):
            logging.info(
                f"✅ 环境影子图已生成并持久化: {environment_chart} "
                f"({os.path.getsize(environment_chart) / 1024:.1f} KB，不参与预警)")
        else:
            environment_chart = None
            logging.warning("⚠️ 环境影子图未生成：metric_daily 历史数据为空")
    except Exception as e:
        environment_chart = None
        logging.warning(f"⚠️ 环境影子图生成失败，邮件降级为无图模式: {e}")

    # 流动性水位仪同样处于影子模式：官方源优先；失败时只接受带原生来源日期的
    # 数据库历史，旧行缺少来源日期则主动降低覆盖率。
    liquidity_text = "=== 市场流动性水位仪（影子观察） ===\n本日计算失败或数据不足。"
    liquidity_chart = None
    liquidity_output_path = os.path.join(
        report_dir, f"liquidity_waterline_{report_date}.png")
    try:
        source = OfficialLiquiditySources(cfg.FRED_API_KEY)
        official_frame = source.build_frame(
            (current_time - timedelta(days=1200)).strftime('%Y-%m-%d'),
            report_date,
        )
        liquidity, liquidity_frame = compute_liquidity_monitor(
            supabase, report_date=report_date,
            official_frame=official_frame, persist=True,
        )
        liquidity_text = format_liquidity_summary(liquidity)
        liquidity_history = build_liquidity_history(liquidity_frame)
        liquidity_chart = generate_liquidity_chart(
            liquidity, liquidity_history, output_path=liquidity_output_path)
        if liquidity_chart and os.path.isfile(liquidity_chart):
            logging.info(
                f"✅ 流动性水位仪生成完成: {liquidity.state}, "
                f"覆盖 {liquidity.coverage:.0%}, {liquidity_chart} "
                f"({os.path.getsize(liquidity_chart) / 1024:.1f} KB)")
        else:
            liquidity_chart = None
            logging.warning("⚠️ 流动性水位仪未生成：有效数据覆盖不足")
    except Exception as e:
        liquidity_chart = None
        logging.warning(f"流动性水位仪降级为无图模式: {e}")

    logging.info("📡 唤醒三大联邦探针...")
    macro_text, macro_alert, macro_raw = scan_macro_regime(supabase, cutoff_date)
    vol_text, vol_alert, vol_raw = scan_volatility_spot(supabase, cutoff_date)
    micro_text, micro_alert, micro_raw_list = scan_micro_options(supabase, cutoff_date)
    tactical_stress_text = format_tactical_stress_summary(macro_raw)

    macro_info = {"text": macro_text, "alert_text": f"(危险指数: {macro_alert}/5)"}
    vol_info = {"text": vol_text, "alert_text": f"(危险指数: {vol_alert}/5)"}
    micro_info = {"text": micro_text, "alert_text": f"(危险指数: {micro_alert}/5)"}

    logging.info("🧠 呼叫 Gemini 进行深度逻辑融合...")
    ai_report, raw_data_feed = generate_ai_report(macro_info, vol_info, micro_info, opex_context, anomaly_matrix_text)
    
    logging.info("🗄️ 正在组装全息数据附录...")
    raw_appendix = format_raw_appendix(macro_raw, vol_raw, micro_raw_list)
    raw_appendix = (
        f"{environment_text}\n\n" + "="*50 +
        f"\n\n{liquidity_text}\n\n" + "="*50 +
        f"\n\n{tactical_stress_text}\n\n" + "="*50 + f"\n\n{raw_appendix}"
    )
    
    logging.info("🚀 推送最终战报...")
    email_sent = send_email(
        f"🚨 机构级全域交叉复盘 (V9.2 环境、流动性与战术压力影子监测) [{report_date}]",
        ai_report, raw_data_feed, raw_appendix,
        image_paths=[environment_chart, liquidity_chart])
    if not email_sent:
        logging.warning("⚠️ 复盘数据已完成入库，但邮件投递失败，请检查SMTP日志。")
    logging.info(">>> 流水线执行完毕，司令部休眠 <<<")
