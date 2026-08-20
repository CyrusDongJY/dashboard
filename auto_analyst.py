import sys
import os
import logging
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
    from daily_review import (
        build_daily_review, fetch_review_inputs, format_daily_review_email,
        persist_daily_review,
    )
    from environment_indices import compute_indices, format_env_summary
    from environment_report import generate_environment_chart
    from liquidity_monitor import compute_liquidity_monitor, format_liquidity_summary
    from liquidity_report import build_liquidity_history, generate_liquidity_chart
    from liquidity_sources import OfficialLiquiditySources
    from risk_capital_ladder import (
        fetch_risk_capital_ladder, format_risk_capital_summary,
    )
    from risk_event_pulse import (
        fetch_event_pulse, format_event_pulse_summary,
    )
    from tactical_stress import format_tactical_stress_summary
except ImportError as e:
    print(f"❌ 致命错误：缺少核心配置文件或探针库 ({e})！")
    sys.exit(1)

from supabase import create_client, Client

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

def send_email(subject, report, supplemental_sections=None,
               image_path=None, image_paths=None):
    msg = MIMEMultipart()
    msg['From'] = cfg.SENDER_EMAIL
    msg['To'] = cfg.RECEIVER_EMAIL
    msg['Subject'] = Header(subject, 'utf-8')
    
    sections = [report]
    sections.extend(section for section in (supplemental_sections or []) if section)
    full_content = ("\n\n" + "=" * 50 + "\n\n").join(sections)
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
        logging.info("✅ 日度规则复盘报告已送达（完整快照仅入库，不进入正文）。")
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
    logging.info("🧮 运行多窗口异常矩阵引擎 (252D基准 / 1D-5D-21D观察 / 跨资产共振)...")
    try:
        anomaly_events, snapshot = run_engine(
            supabase, report_date=report_date, persist=True,
            session="EOD", is_final=True)
        max_sev = max([e.severity for e in anomaly_events], default=0)
        log_data_quality(supabase, job_name='auto_analyst', table_name='anomaly_events',
                         status='ok', rows_written=len(anomaly_events),
                         anomaly_count=len(anomaly_events),
                         notes=f"最高severity={max_sev}")
        logging.info(f"✅ 异常矩阵生成：{len(anomaly_events)} 条 (最高severity={max_sev})")
    except Exception as e:
        logging.error(f"⚠️ 异常矩阵引擎运行失败: {e}（复盘质量降级）")
        anomaly_events = []
        log_data_quality(
            supabase, job_name='auto_analyst', table_name='anomaly_events',
            status='failed', rows_written=0, notes=str(e)[:300])

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

    tactical_stress_text = format_tactical_stress_summary({})
    try:
        response = (
            supabase.table("market_history")
            .select(
                "record_date,eod_stress_score,micro_score,stress_components,"
                "stress_coverage,stress_confidence,stress_calc_version,"
                "stress_computed_at"
            )
            .eq("record_date", report_date).limit(1).execute()
        )
        tactical_stress_row = (
            dict(response.data[0]) if response.data else {"record_date": report_date}
        )
        tactical_stress_text = format_tactical_stress_summary(
            tactical_stress_row)
        logging.info(
            "盘后跨资产战术压力摘要完成: score=%s, coverage=%s",
            tactical_stress_row.get("eod_stress_score"),
            tactical_stress_row.get("stress_coverage"),
        )
    except Exception as e:
        logging.warning(f"盘后跨资产战术压力摘要降级: {e}")

    risk_capital_text = (
        "=== 风险资本阶梯（影子观察，不触发预警） ===\n"
        "本日数据尚未入库。")
    try:
        risk_capital = fetch_risk_capital_ladder(supabase, report_date)
        risk_capital_text = format_risk_capital_summary(risk_capital)
        logging.info(
            "风险资本阶梯摘要完成: state=%s, coverage=%.0f%%",
            risk_capital.get('state'),
            float(risk_capital.get('coverage') or 0) * 100)
    except Exception as e:
        logging.warning(f"风险资本阶梯摘要降级: {e}")

    event_pulse_text = (
        "=== 当日动态异动脉冲（影子观察，不触发预警） ===\n"
        "本日数据尚未入库。")
    try:
        event_pulse = fetch_event_pulse(supabase, report_date)
        event_pulse_text = format_event_pulse_summary(event_pulse)
        logging.info(
            "动态异动脉冲摘要完成: state=%s, candidates=%d, eligible=%d",
            event_pulse.get('state'),
            int(event_pulse.get('candidate_count') or 0),
            int(event_pulse.get('eligible_count') or 0))
    except Exception as e:
        logging.warning(f"动态异动脉冲摘要降级: {e}")

    logging.info("📡 唤醒三大联邦探针...")
    # Keep all probes running for their existing capture/persistence side effects;
    # only the compact deterministic review is rendered in the email.
    _macro_text, _macro_alert, _macro_raw = scan_macro_regime(supabase, cutoff_date)
    _vol_text, _vol_alert, _vol_raw = scan_volatility_spot(supabase, cutoff_date)
    _micro_text, _micro_alert, micro_raw_list = scan_micro_options(supabase, cutoff_date)
    logging.info("🧭 生成确定性日度复盘状态并写入 daily_review...")
    try:
        review_inputs = fetch_review_inputs(supabase, report_date)
        review = build_daily_review(
            report_date,
            market_row=review_inputs['market_row'],
            spot_row=review_inputs['spot_row'],
            prior_market_row=review_inputs['prior_market_row'],
            prior_spot_row=review_inputs['prior_spot_row'],
            prior_review=review_inputs['prior_review'],
            quality_rows=review_inputs['quality_rows'],
            metric_rows=review_inputs['metric_rows'],
            anomaly_events=anomaly_events,
            micro_rows=micro_raw_list,
        )
        persist_daily_review(supabase, review)
        review_report = format_daily_review_email(
            review, prior_review=review_inputs['prior_review'])
    except Exception as e:
        logging.exception(f"❌ 日度复盘状态引擎失败，邮件进入只读降级模式: {e}")
        review_report = (
            f"美股日度复盘 | {report_date}\n"
            "数据质量：低\n"
            "状态：数据不可用\n"
            "原因：日度复盘规则引擎运行失败。原始数据和异常事件仍按原流程入库。\n\n"
            "异常矩阵：\n" + format_matrix(anomaly_events)
        )
    
    logging.info("🚀 推送最终战报...")
    email_sent = send_email(
        f"美股日度规则复盘 [{report_date}]",
        review_report,
        supplemental_sections=[
            environment_text, liquidity_text, tactical_stress_text,
            risk_capital_text,
            event_pulse_text],
        image_paths=[environment_chart, liquidity_chart])
    if not email_sent:
        logging.warning("⚠️ 复盘数据已完成入库，但邮件投递失败，请检查SMTP日志。")
    logging.info(">>> 流水线执行完毕，司令部休眠 <<<")
