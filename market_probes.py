import pandas as pd
import numpy as np

# ==========================================
# V9.0 说明：
#   - 探针只负责"文本快照 + 粗粒度危险指数"，供 AI 叙述背景；
#     真正的客观异常判定移交 anomaly_engine（252D 基准 + 多窗口 + 共振）。
#   - DPSV 仅作为FINRA场外短售成交量代理展示；方向未经回测，不加减危险分。
#   - 微观探针改读拆分后的 stock_options_pre_market + stock_spot_post_close，
#     旧表 stock_options_daily 作为回退，保证迁移期不断档。
# ==========================================

# ==========================================
# 探针一：宏观大势与极值探针 (Macro & Regime Probe)
# ==========================================
def scan_macro_regime(supabase, cutoff_date):
    response = supabase.table('market_history').select("*").gte('record_date', cutoff_date).order('record_date', desc=False).execute()
    if not response.data: return "⚠️ 宏观数据缺失", 0, {}

    df = pd.DataFrame(response.data)
    today = df.iloc[-1]
    report_lines = []
    alert_level = 0

    mac_score = today.get('macro_score', 'N/A')
    mic_score = today.get('micro_score', 'N/A')
    regime = today.get('regime', '未知')
    skeleton = today.get('skeleton', '未知')

    report_lines.append(f"🎯 【系统量化评分】 宏观压力得分: {mac_score}/100 | 微观战术得分: {mic_score}/100")
    report_lines.append(f"📍 宏观象限判定: {regime} | 市场骨架评级: {skeleton}")

    # --- 区分真正的恐慌与底部推力 ---
    breadth = str(today.get('breadth_thrust', ''))
    if "🚀" in breadth:
        report_lines.append(f"🟢 【底部确立】广度推力触发: {breadth} -> 此为极强多头信号！")
    elif "🚨" in breadth:
        report_lines.append(f"🩸 【极致恐慌】广度推力触发: {breadth}")
        alert_level += 2

    if "🚨" in str(today.get('dix_div', '')):
        report_lines.append(f"ℹ️ 【DIX结构代理】{today['dix_div']} -> 仅作场外成交结构观察，方向需价格确认。")

    if "倒挂" in str(today.get('vix_term', '')):
        report_lines.append(f"🩸 【波动率倒挂】VIX结构崩溃 ({today.get('vix_term')})，市场进入极度恐慌！")
        alert_level += 2

    vrp = today.get('vrp', '')
    if "极度恐慌" in str(vrp): report_lines.append(f"⚠️ VRP溢价异动: {vrp} -> 做市商Sell Put底牌显现"); alert_level += 1
    hyg_trend = today.get('hyg_tlt_trend', '')
    if "避险" in str(hyg_trend): report_lines.append(f"⚠️ 避险剪刀差告警: {hyg_trend}"); alert_level += 1

    # 动能扫描：观察最新值 vs 至多252个交易日基准（此前是"最近约20天"短样本，切换期误报高）
    numeric_cols = ['reserves', 'jpy', 'cmf', 'us10y', 'dxy', 'oil', 'btc']
    for col in numeric_cols:
        if col not in df.columns: continue
        col_data = df[col].apply(pd.to_numeric, errors='coerce').dropna()
        if len(col_data) > 30:
            baseline = col_data.iloc[-252:] if len(col_data) > 252 else col_data
            mean_val, std_val = baseline.mean(), baseline.std()
            latest_val = col_data.iloc[-1]
            if std_val > 0:
                z_score = (latest_val - mean_val) / std_val
                if z_score > 2.0: report_lines.append(f"📈 【动能向上】{col.upper()} 飙升 {z_score:.2f} 倍标准差 (现值: {latest_val})")
                elif z_score < -2.0: report_lines.append(f"📉 【动能向下】{col.upper()} 暴跌 {z_score:.2f} 倍标准差 (现值: {latest_val})")

    return "\n".join(report_lines), alert_level, today.to_dict()

# ==========================================
# 探针二：微观筹码与期权防线探针 (Micro Options Probe)
# ==========================================
def _fetch_micro_df(supabase, cutoff_date):
    """优先读拆分后的盘前/盘后表并按 (date,ticker) 合并；两表皆空则回退旧表。"""
    def _q(table):
        try:
            r = supabase.table(table).select("*").gte('date', cutoff_date).order('date', desc=False).execute()
            return pd.DataFrame(r.data) if r.data else pd.DataFrame()
        except Exception:
            return pd.DataFrame()

    pre, post = _q('stock_options_pre_market'), _q('stock_spot_post_close')
    if pre.empty and post.empty:
        return _q('stock_options_daily')

    if pre.empty: return post
    if post.empty: return pre
    drop_cols = [c for c in ['id', 'created_at', 'source_date', 'as_of_time', 'ingested_at'] if c in post.columns]
    merged = pd.merge(
        pre, post.drop(columns=drop_cols),
        on=['date', 'ticker'], how='outer', suffixes=('', '_post')
    )
    # 盘后收盘价（current_price_post）优先于盘前快照价，用于与 POC 比较
    if 'current_price_post' in merged.columns:
        merged['current_price'] = merged['current_price_post'].combine_first(merged.get('current_price'))
    return merged

def scan_micro_options(supabase, cutoff_date):
    SYMBOLS = ['SPY', 'QQQ', 'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'META', 'NVDA', 'TSLA', 'ORCL']
    df = _fetch_micro_df(supabase, cutoff_date)
    if df.empty: return "⚠️ 期权微观数据缺失", 0, []

    report_lines = []
    alert_level = 0
    raw_options_list = []

    for ticker in SYMBOLS:
        tdf = df[df['ticker'] == ticker].dropna(subset=['date']).sort_values('date')
        if len(tdf) < 2: continue

        today, yday = tdf.iloc[-1], tdf.iloc[-2]
        last_week = tdf.iloc[-6] if len(tdf) >= 6 else None
        ticker_anomalies = []
        raw_options_list.append(today.to_dict())

        curr_px, poc_px = pd.to_numeric(today.get('current_price', 0), errors='coerce'), pd.to_numeric(today.get('poc_price', 0), errors='coerce')
        if pd.notna(poc_px) and pd.notna(curr_px) and curr_px > 0:
            if curr_px < poc_px * 0.98: ticker_anomalies.append(f"[筹码断层] 现价(${curr_px})严重跌破POC核心成本(${poc_px})。")
            elif curr_px > poc_px * 1.02: ticker_anomalies.append(f"[筹码脱离] 现价(${curr_px})强势突破POC核心成本(${poc_px})。")

        if "背离" in str(today.get('obv_status', '')): ticker_anomalies.append(f"[资金动能] {today['obv_status']}")

        dpsv = pd.to_numeric(today.get('dpsv_pct', np.nan), errors='coerce')
        if pd.notna(dpsv):
            dpsv_src = today.get('dpsv_source_date')
            src_note = f" (FINRA日期: {dpsv_src})" if dpsv_src else ""
            if dpsv > 50:
                ticker_anomalies.append(f"[FINRA短售代理] 读数偏高({dpsv:.2f}%){src_note}，方向需结合价格与成交验证。")
            elif dpsv < 40:
                ticker_anomalies.append(f"[FINRA短售代理] 读数偏低({dpsv:.2f}%){src_note}，方向需结合价格与成交验证。")

        zgl_today = pd.to_numeric(
            today.get('gamma_flip_all', today.get('zgl_price', np.nan)),
            errors='coerce')
        if last_week is not None:
            zgl_lw = pd.to_numeric(
                last_week.get('gamma_flip_all', last_week.get('zgl_price', np.nan)),
                errors='coerce')
            if pd.notna(zgl_today) and pd.notna(zgl_lw) and zgl_today != zgl_lw:
                direction = "上移 🔼" if zgl_today > zgl_lw else "下移 🔽"
                ticker_anomalies.append(
                    f"[Gamma Flip观察-WoW] 主零点{direction} "
                    f"(上周: {zgl_lw} -> 今日: {zgl_today})；"
                    "到期滚动和采样变化可能造成跳变。")

        pin_strike = pd.to_numeric(today.get('pin_strike', np.nan), errors='coerce')
        pin_state = today.get('pin_state')
        if pd.notna(pin_strike) and pin_state:
            label = "Pin候选位" if pin_state == "PIN_CANDIDATE" else "突破枢轴"
            ticker_anomalies.append(f"[双边Gamma集中] {label} ${pin_strike:.2f}。")

        charm_t0, charm_t1 = pd.to_numeric(today.get('charm_m', np.nan), errors='coerce'), pd.to_numeric(yday.get('charm_m', np.nan), errors='coerce')
        if pd.notna(charm_t0) and pd.notna(charm_t1):
            if charm_t0 > 0 > charm_t1: ticker_anomalies.append(f"[Charm翻转] 由负转正 (昨日: {charm_t1:.2f}M -> 今日: {charm_t0:.2f}M)")
            elif charm_t0 < 0 < charm_t1: ticker_anomalies.append(f"[Charm翻转] 由正转负 (昨日: {charm_t1:.2f}M -> 今日: {charm_t0:.2f}M)")

        ivr = pd.to_numeric(today.get('ivr_pct', np.nan), errors='coerce')
        if pd.notna(ivr) and ivr < 10: ticker_anomalies.append(f"[波动率极值] IVR极低({ivr:.1f}%) -> 情绪极度麻木，随时可能爆发Gamma Squeeze。")

        if ticker_anomalies:
            report_lines.append(f"🎯 [{ticker}] 微观雷达:")
            report_lines.extend([f"   - {a}" for a in ticker_anomalies])

    if not report_lines: report_lines.append("各标的微观筹码与期权防线结构稳定，无显著异动。")
    return "\n".join(report_lines), alert_level, raw_options_list

# ==========================================
# 探针三：波动率结构与市场广度探针 (Volatility & Spot Breadth Probe)
# ==========================================
def scan_volatility_spot(supabase, cutoff_date):
    response = supabase.table('macro_spot_daily').select("*").gte('date', cutoff_date).order('date', desc=False).execute()
    if not response.data: return "⚠️ 现货波动率数据缺失", 0, {}

    df = pd.DataFrame(response.data)
    today = df.iloc[-1]
    report_lines = []
    alert_level = 0

    contango = pd.to_numeric(today.get('vix_contango_pct', np.nan), errors='coerce')
    if pd.notna(contango):
        if contango < 0: report_lines.append(f"🩸 【极度高危】VIX期货 Backwardation 倒挂 ({contango:+.2f}%)"); alert_level += 2
        elif contango < 3: report_lines.append(f"⚠️ VIX升水极窄 ({contango:+.2f}%)，市场脆弱平衡。"); alert_level += 1
        else: report_lines.append(f"🟢 VIX期限结构健康 (升水 {contango:+.2f}%)。")

    breadth_diff = pd.to_numeric(today.get('breadth_diff_pct', np.nan), errors='coerce')
    if pd.notna(breadth_diff):
        if breadth_diff > 1.5: report_lines.append(f"⚠️ 【严重抱团】Mag7 表现领先等权重(RSP) {breadth_diff:+.2f}%，掩盖内部抛压。"); alert_level += 1
        elif breadth_diff < -1.5: report_lines.append(f"⚠️ 【巨头失血】资金撤出头部科技股，落后大盘 {breadth_diff:+.2f}%。")

    tqqq_drag = pd.to_numeric(today.get('tqqq_drag_pct', np.nan), errors='coerce')
    if pd.notna(tqqq_drag) and tqqq_drag < -2.0:
        report_lines.append(f"📉 TQQQ存在显著隐性震荡损耗({tqqq_drag:+.2f}%)，证明近期行情为剧烈洗盘。")

    return "\n".join(report_lines), alert_level, today.to_dict()
