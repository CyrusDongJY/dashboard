import os
import sys
import csv
import math
import logging
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.header import Header
from contextlib import contextmanager

import pytz
import pandas as pd
import numpy as np
from ib_insync import *
from supabase import create_client, Client
import pandas_market_calendars as mcal

from market_utils import attach_metadata, safe_upsert, log_data_quality, find_missing_fields

# ================= 🔐 安全挂载全局金库 =================
CONFIG_DIR = os.path.expanduser('~/market_dashboard')
if CONFIG_DIR not in sys.path:
    sys.path.append(CONFIG_DIR)

try:
    import market_config as cfg
except ImportError:
    print("❌ 致命错误：找不到 market_config.py 密钥文件！程序终止。")
    sys.exit(1)

# ================= 全局常量与环境配置 =================
NY_TZ = pytz.timezone('America/New_York')
BATCH_SIZE = 50
SLEEP_SHORT = 2
SLEEP_LONG = 6
DB_MAX_RETRIES = 3 

SYMBOLS = ['SPY', 'QQQ', 'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'META', 'NVDA', 'TSLA', 'ORCL']

# 从金库读取凭证
supabase: Client = create_client(cfg.SUPABASE_URL, cfg.SUPABASE_KEY)
ib = IB()

# ================= 日志防干扰设置 =================
logging.getLogger('ib_insync').setLevel(logging.ERROR)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    formatter = logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s', datefmt='%H:%M:%S')
    ch.setFormatter(formatter)
    logger.addHandler(ch)

def onError(reqId, errorCode, errorString, contract):
    if errorCode != 200: 
        logger.warning(f"IB Error {errorCode}: {errorString}")
ib.errorEvent += onError

# ================= 🛡️ 架构级资源管理与重试机制 =================

@contextmanager
# ✅ 修复: 增加了 tick_list 参数。期权传 '100,101'，VIX期货传 ''
def market_data_subscription(ib_instance, contracts, sleep_time, tick_list=''):
    """【智能沙盒】防连接泄漏"""
    tickers = []
    try:
        tickers = [ib_instance.reqMktData(c, tick_list, False, False) for c in contracts]
        ib_instance.sleep(sleep_time)
        yield tickers
    finally:
        for ticker in tickers:
            try:
                if getattr(ticker, 'contract', None):
                    ib_instance.cancelMktData(ticker.contract)
            except Exception as e:
                logger.debug(f"取消订阅失败: {e}")

def safe_db_upsert(table_name, data):
    """【容灾模块】原生指数退避重试，支持智能覆写"""
    # 根据表名动态选择冲突字段
    conflict_cols = 'date,ticker' if table_name == 'stock_options_daily' else 'date'
    return safe_upsert(supabase, table_name, data, conflict_cols=conflict_cols,
                       max_retries=DB_MAX_RETRIES, sleep_fn=ib.sleep)

# ================= 辅助安全函数 =================
def _safe_market_price(ticker):
    try:
        val = None
        if hasattr(ticker, 'marketPrice'):
            try: val = ticker.marketPrice()
            except: val = None
        if val is None or (isinstance(val, float) and math.isnan(val)):
            val = getattr(ticker, 'close', None)
        if val is None or (isinstance(val, float) and math.isnan(val)):
            val = getattr(ticker, 'last', None)
        if val is None: return None
        if isinstance(val, (int, float, np.floating)):
            if math.isnan(val): return None
            return float(val)
        return float(val)
    except: return None

# ================= 数据库沉淀模块 (本地CSV备份) =================
def append_to_spot_db_local(macro_data, date_str):
    db_path = "/home/winters_dong426/market_dashboard/Macro_Spot_History.csv"
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    
    def safe_format(val):
        return f"{val:.2f}" if isinstance(val, (int, float)) else ("-" if val is None else str(val))

    header = ['日期', 'QQQ动能(%)', '广度差(%)', 'Mag7表现(%)', 'RSP表现(%)', '[QQQ]现货POC', '[SPY]现货POC', 'TQQQ全局损耗(%)']
    new_row = {
        '日期': date_str,
        'QQQ动能(%)': safe_format(macro_data.get('qqq_mom_pct')),
        '广度差(%)': safe_format(macro_data.get('breadth_diff_pct')),
        'Mag7表现(%)': safe_format(macro_data.get('mag7_perf_pct')),
        'RSP表现(%)': safe_format(macro_data.get('rsp_perf_pct')),
        '[QQQ]现货POC': safe_format(macro_data.get('qqq_poc_price')),
        '[SPY]现货POC': safe_format(macro_data.get('spy_poc_price')),
        'TQQQ全局损耗(%)': safe_format(macro_data.get('tqqq_drag_pct'))
    }

    try:
        if os.path.isfile(db_path):
            df = pd.read_csv(db_path, encoding='utf-8-sig')
        else:
            df = pd.DataFrame(columns=header)

        df = df[df['日期'].astype(str) != date_str]
        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)

        df.to_csv(db_path, index=False, encoding='utf-8-sig')
        logger.info("📁 现货宏观数据已成功写入本地 CSV 备份。")
    except Exception as e:
        logger.warning(f"⚠️ 本地数据库写入失败: {e}")

# ================= 邮件发送与节假日门卫 =================
def send_email(subject, body):
    msg = MIMEText(body, 'plain', 'utf-8')
    # 从金库读取发送人与收件人
    msg['From'] = cfg.SENDER_EMAIL
    msg['To'] = cfg.RECEIVER_EMAIL
    msg['Subject'] = Header(subject, 'utf-8')
    
    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(cfg.SENDER_EMAIL, cfg.APP_PASSWORD)
        server.sendmail(cfg.SENDER_EMAIL, [cfg.RECEIVER_EMAIL], msg.as_string())
        server.quit()
        return True
    except Exception as e: 
        logger.warning(f"邮件发送失败: {e}")
        return False

def check_market_status():
    if len(sys.argv) > 1 and sys.argv[1].lower() == 'now': return True
    ny_time = datetime.now(NY_TZ)
    if ny_time.hour != 16:
        logger.info(f"🛑 拦截：当前美东时间 {ny_time.strftime('%H:%M')}，非盘后清算期。")
        return False
    return True

def is_trading_day():
    try:
        ny_time = datetime.now(NY_TZ)
        today_str = ny_time.strftime('%Y-%m-%d')
        nyse = mcal.get_calendar('NYSE')
        schedule = nyse.schedule(start_date=today_str, end_date=today_str)
        return not schedule.empty
    except Exception as e:
        logger.warning(f"日历检测异常: {e}")
        return False 

# ================= 核心计算模块 =================
def get_market_breadth_ib(ib):
    report = "【模块一：市场广度与巨头真实表现 (今日盘终)】\n"
    perf_dict = {}
    symbols = ['SPY', 'RSP', 'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'META', 'NVDA', 'TSLA']
    
    for sym in symbols:
        try:
            contracts = ib.qualifyContracts(Stock(sym, 'SMART', 'USD'))
            if not contracts:
                perf_dict[sym] = 0
                continue
            bars = ib.reqHistoricalData(contracts[0], endDateTime='', durationStr='2 D', barSizeSetting='1 day', whatToShow='TRADES', useRTH=True)
            if bars and len(bars) >= 2:
                perf_dict[sym] = (bars[-1].close / bars[-2].close - 1) * 100
            else: perf_dict[sym] = 0
        except: perf_dict[sym] = 0

    mag7_list = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'META', 'NVDA', 'TSLA']
    mag7_perf = [perf_dict[sym] for sym in mag7_list if sym in perf_dict]
    avg_mag7 = sum(mag7_perf) / len(mag7_perf) if mag7_perf else 0

    spy_perf = perf_dict.get('SPY', 0)
    rsp_perf = perf_dict.get('RSP', 0)
    diff_perf = avg_mag7 - rsp_perf

    report += f"- 头部科技 (Mag 7) 平均涨跌: {avg_mag7:+.2f}%\n"
    report += f"- 标普500 (SPY) 整体涨跌: {spy_perf:+.2f}%\n"
    report += f"- 标普等权重 (RSP) 真实涨跌: {rsp_perf:+.2f}%\n"
    report += f"- 巨头与等权重偏差率: {diff_perf:+.2f}% (正值代表资金极度抱团巨头)\n\n"
    
    return report, {
        "mag7_perf_pct": round(avg_mag7, 2), "rsp_perf_pct": round(rsp_perf, 2), "breadth_diff_pct": round(diff_perf, 2)
    }

def get_spot_poc_obv_ib(ib, symbol, duration='6 M'):
    report = f"\n--- 📍 现货筹码与资金动能 ({symbol}) ---\n"
    try:
        contracts = ib.qualifyContracts(Stock(symbol, 'SMART', 'USD'))
        if not contracts: return report + "⚠️ 无法获取合约信息。\n", {}
        
        bars = ib.reqHistoricalData(contracts[0], endDateTime='', durationStr=duration, barSizeSetting='1 day', whatToShow='TRADES', useRTH=True, formatDate=1)
        if not bars: return report + "⚠️ 无法获取历史交易数据。\n", {}
            
        df = pd.DataFrame([{'Date': pd.to_datetime(b.date), 'Close': b.close, 'Volume': b.volume} for b in bars])
        if df.empty: return report + "⚠️ 数据帧为空。\n", {}

        curr_price = float(df['Close'].iloc[-1])

        sign = np.sign(df['Close'].diff().fillna(0))
        df['OBV'] = (df['Volume'].fillna(0) * sign).cumsum()

        obv_5d = df['OBV'].iloc[-5:] if len(df) >= 2 else df['OBV']
        price_5d = df['Close'].iloc[-5:] if len(df) >= 2 else df['Close']

        obv_status = "数据不足以判断"
        if len(price_5d) >= 2 and len(obv_5d) >= 2:
            if price_5d.iloc[-1] > price_5d.iloc[0] and obv_5d.iloc[-1] < obv_5d.iloc[0]: obv_status = "价格创新高 | 资金净流出 (量价背离)"
            elif price_5d.iloc[-1] < price_5d.iloc[0] and obv_5d.iloc[-1] > obv_5d.iloc[0]: obv_status = "价格创新低 | 资金净流入 (量价背离)"
            elif obv_5d.iloc[-1] > obv_5d.iloc[0]: obv_status = "价格与量能同步上升"
            else: obv_status = "价格与量能同步下降"

        poc_price = None
        try:
            close_vals = df['Close'].dropna().values
            vol_vals = df['Volume'].fillna(0).values
            if len(close_vals) == 0: raise ValueError("无收盘价数据")
            
            price_range = close_vals.max() - close_vals.min()
            if price_range < 1e-6:
                poc_price = close_vals.mean()
            else:
                bins = np.linspace(close_vals.min(), close_vals.max(), 51)
                inds = np.digitize(close_vals, bins) - 1
                inds = np.clip(inds, 0, len(bins) - 2) 
                
                bin_vol = {}
                for idx, v in zip(inds, vol_vals):
                    bin_vol[idx] = bin_vol.get(idx, 0) + v
                    
                if not bin_vol: raise ValueError("POC 计算无有效成交量")
                max_idx = max(bin_vol.items(), key=lambda x: x[1])[0]
                poc_price = (bins[max_idx] + bins[max_idx + 1]) / 2.0
        except Exception as e:
            logger.warning(f"{symbol} POC 计算失败: {e}")

        report += f"绝对成本核 (POC): {('$' + format(poc_price, '.2f')) if poc_price is not None else '未知'}\n"
        if poc_price is not None:
            report += "当前价格位阶: 位于 POC 之上\n" if curr_price >= poc_price else "当前价格位阶: 跌破 POC 水位\n"
        report += f"近5日动能 (OBV): {obv_status}\n"

        return report, {"poc_price": round(poc_price, 2) if poc_price is not None else None, "obv_status": obv_status, "close_price": round(curr_price, 2)}
    except Exception as e: 
        return report + f"⚠️ 现货筹码计算异常: {e}\n", {}

def get_vix_term_structure(ib):
    report = f"\n--- 🌋 VIX 期限结构宏观状态 ---\n"
    try:
        cds = ib.reqContractDetails(Future('VIX', exchange='CFE'))
        if not cds: return "⚠️ VIX 抓取失败: 未返回合约。\n", {}
        
        # ✅ 修复 1：僵尸过滤器。严格剔除已经过期（或今天早晨刚刚交割）的合约
        today_str = datetime.now(NY_TZ).strftime('%Y%m%d')
        valid_futures = [cd.contract for cd in cds if cd.contract.lastTradeDateOrContractMonth > today_str]
        futures = sorted(valid_futures, key=lambda x: x.lastTradeDateOrContractMonth)
        
        if len(futures) < 2: return "⚠️ VIX 抓取失败: 近期可用合约不足。\n", {}

        m1_fut, m2_fut = futures[0], futures[1]
        ib.qualifyContracts(m1_fut, m2_fut)

        m1_px, m2_px = None, None
        
        # ✅ 修复 2：引入"重武器"（历史 K 线）作为首选获取方式，避开流式延迟数据的盲区
        for fut in [m1_fut, m2_fut]:
            try:
                bars = ib.reqHistoricalData(
                    fut,
                    endDateTime='',
                    durationStr='2 D',
                    barSizeSetting='1 day',
                    whatToShow='TRADES',
                    useRTH=False
                )
                if bars:
                    px = float(bars[-1].close)
                    if fut == m1_fut: m1_px = px
                    else: m2_px = px
            except: pass

        # 兜底机制：如果重武器因为权限问题失败，再回退到原来的流式订阅
        if m1_px is None or m2_px is None:
            with market_data_subscription(ib, [m1_fut, m2_fut], SLEEP_LONG, tick_list='') as tickers:
                if m1_px is None and len(tickers) > 0: m1_px = _safe_market_price(tickers[0])
                if m2_px is None and len(tickers) > 1: m2_px = _safe_market_price(tickers[1])

        if m1_px is None or m2_px is None or m1_px <= 0 or m2_px <= 0:
            return "⚠️ VIX 延迟快照价格异常或不可用。\n", {}

        diff = m2_px - m1_px
        contango_pct = (diff / m1_px) * 100 if m1_px != 0 else 0
        structure_type = "Contango (升水)" if diff > 0 else "Backwardation (贴水倒挂)"

        report += f"近月 (M1 - {m1_fut.lastTradeDateOrContractMonth}): {m1_px:.2f}\n"
        report += f"次月 (M2 - {m2_fut.lastTradeDateOrContractMonth}): {m2_px:.2f}\n"
        report += f"价差结构: {structure_type}\n"
        report += f"价差比例: {contango_pct:+.2f}%\n"

        return report, {
            "vix_m1": round(m1_px, 2), "vix_m2": round(m2_px, 2), "vix_contango_pct": round(contango_pct, 2)
        }
    except Exception as e: 
        return f"⚠️ VIX 异常报错: {e}\n", {}

def get_ivr_only(ib, symbol):
    report = f"\n--- ⚡ {symbol} 波动率情绪 (IVR) ---\n"
    try:
        contracts = ib.qualifyContracts(Stock(symbol, 'SMART', 'USD'))
        if not contracts: return report + "⚠️ 无法获取合约信息。\n", {}
        
        iv_bars = ib.reqHistoricalData(contracts[0], endDateTime='', durationStr='1 Y', barSizeSetting='1 day', whatToShow='OPTION_IMPLIED_VOLATILITY', useRTH=True)
        ivr = None
        if iv_bars:
            iv_closes = [bar.close for bar in iv_bars if bar.close is not None and bar.close > 0]
            if iv_closes:
                current_iv = iv_closes[-1]
                min_iv, max_iv = min(iv_closes), max(iv_closes)
                ivr = ((current_iv - min_iv) / (max_iv - min_iv)) * 100 if max_iv > min_iv else 0.0
                report += f"📊 隐含波动率 (IV): {current_iv:.4f} (52周区间: {min_iv:.4f} - {max_iv:.4f})\n"
                report += f"📈 IVR (波动率百分位): {ivr:.1f}%\n"
        else: report += "⚠️ 未获取到历史 IV 数据。\n"

        return report, {"ivr_pct": round(ivr, 2) if ivr is not None else None}
    except Exception as e: return report + f"⚠️ IVR 分析异常: {e}\n", {}

def get_unusual_options_activity(ib, symbol):
    report = f"\n--- 🚨 期权成交异动扫描 (Vol/OI - {symbol}) ---\n"
    try:
        contracts = ib.qualifyContracts(Stock(symbol, 'SMART', 'USD'))
        if not contracts: return report + "⚠️ 无法获取合约信息。\n"
        stock = contracts[0]
        
        bars = ib.reqHistoricalData(stock, endDateTime='', durationStr='1 D', barSizeSetting='1 day', whatToShow='TRADES', useRTH=True)
        if not bars: return report + "⚠️ 获取现价失败。\n"
        curr_price = bars[-1].close

        chains = ib.reqSecDefOptParams(stock.symbol, '', stock.secType, stock.conId)
        if not chains: return report + "⚠️ 未找到期权链信息。\n"
        
        chain = next((c for c in chains if c.exchange == 'SMART' and getattr(c, 'tradingClass', None) == stock.symbol), None)
        if not chain: chain = next((c for c in chains if c.exchange == 'SMART'), None)
        if not chain: return report + "⚠️ 未匹配到可用期权链 (SMART)。\n"

        now = datetime.now(NY_TZ).date()
        expirations = sorted(chain.expirations) if getattr(chain, 'expirations', None) else []
        if not expirations: return report + "⚠️ 期权到期日信息缺失。\n"
            
        target_exps = []
        for exp in expirations:
            try:
                if 7 <= (datetime.strptime(exp, '%Y%m%d').date() - now).days <= 45: target_exps.append(exp)
            except: pass
        if not target_exps: target_exps = [expirations[0]]

        valid_strikes = sorted(list(chain.strikes)) if getattr(chain, 'strikes', None) else []
        if not valid_strikes: return report + "⚠️ 期权行权价数据缺失。\n"
            
        target_strikes = [s for s in valid_strikes if curr_price * 0.88 <= s <= curr_price * 1.12]
        if not target_strikes:
            mid = int(len(valid_strikes) / 2)
            target_strikes = valid_strikes[max(0, mid - 20):min(len(valid_strikes), mid + 20)]

        all_opt_contracts = []
        for exp in target_exps[:2]:
            opts = [Option(symbol, exp, s, r, 'SMART', tradingClass=getattr(chain, 'tradingClass', None)) for s in target_strikes for r in ['C', 'P']]
            try:
                qualified = ib.qualifyContracts(*opts)
                if qualified: all_opt_contracts.extend(qualified)
            except: pass

        anomalies = []
        for i in range(0, len(all_opt_contracts), BATCH_SIZE):
            batch = all_opt_contracts[i:i+BATCH_SIZE]
            
            # ✅ 期权数据必须使用 '100,101'
            with market_data_subscription(ib, batch, SLEEP_LONG, tick_list='100,101') as tickers:
                for t in tickers:
                    if t is None or getattr(t, 'contract', None) is None: continue
                    r = getattr(t.contract, 'right', None)
                    oi = getattr(t, 'callOpenInterest' if r == 'C' else 'putOpenInterest', getattr(t, 'openInterest', 0))
                    if oi is None or (isinstance(oi, float) and math.isnan(oi)): oi = 0
                    
                    vol = t.volume
                    if vol is None or (isinstance(vol, float) and math.isnan(vol)): vol = 0
                    
                    if oi > 100 and vol > 500 and (vol / oi) >= 3.0:
                        anomalies.append({'Exp': t.contract.lastTradeDateOrContractMonth, 'Strike': t.contract.strike, 'Right': 'Call' if r == 'C' else 'Put', 'Vol': int(vol), 'OI': int(oi), 'Ratio': vol / oi})
            ib.sleep(SLEEP_SHORT)
            
        if not anomalies: report += "未侦测到成交量显著大于持仓量(Vol/OI > 3)的合约。\n"
        else:
            df = pd.DataFrame(anomalies).sort_values('Ratio', ascending=False).head(5)
            report += "发现成交量异常合约 (Vol/OI > 3倍)：\n"
            for _, row in df.iterrows(): report += f"   ⚡ [{row['Exp']}] ${row['Strike']} {row['Right']} -> 单日成交 {row['Vol']}手 (历史持仓 {row['OI']}手) | 换手倍数: {row['Ratio']:.1f}x\n"
        return report
    except Exception as e: return report + f"⚠️ 异动扫描报错: {e}\n"

def get_etf_structural_drag(ib, base_symbol, lev_symbol, leverage, duration='6 M'):
    report = f"\n【模块四：🧮 杠杆产品结构损耗监控 ({base_symbol} vs {lev_symbol})】\n"
    global_slippage = None
    try:
        base_contracts = ib.qualifyContracts(Stock(base_symbol, 'SMART', 'USD'))
        lev_contracts = ib.qualifyContracts(Stock(lev_symbol, 'SMART', 'USD'))
        if not base_contracts or not lev_contracts: return report + "⚠️ 获取底层合约信息失败。\n", {}
            
        base_bars = ib.reqHistoricalData(base_contracts[0], endDateTime='', durationStr=duration, barSizeSetting='1 day', whatToShow='TRADES', useRTH=True)
        lev_bars = ib.reqHistoricalData(lev_contracts[0], endDateTime='', durationStr=duration, barSizeSetting='1 day', whatToShow='TRADES', useRTH=True)
        if not base_bars or not lev_bars: return report + "⚠️ 历史数据抓取失败。\n", {}
        
        df = pd.DataFrame([{'Date': b.date, 'Base_Close': b.close} for b in base_bars]).set_index('Date').join(pd.DataFrame([{'Date': b.date, 'Lev_Close': b.close} for b in lev_bars]).set_index('Date'), how='inner').dropna()
        df['Base_Ret'], df['Lev_Ret'] = df['Base_Close'].pct_change(), df['Lev_Close'].pct_change()
        df = df.dropna()
        df['Theory_Ret'] = df['Base_Ret'] * leverage
        
        for label, days in {'1个月 (近21天)': 21, '3个月 (近63天)': 63, '6个月 (全局)': len(df)}.items():
            if len(df) < days: continue
            df_slice = df.iloc[-days:]
            actual_base_ret, actual_lev_ret, theory_lev_ret = (df_slice['Base_Ret'] + 1).prod() - 1, (df_slice['Lev_Ret'] + 1).prod() - 1, (df_slice['Theory_Ret'] + 1).prod() - 1
            slippage = actual_lev_ret - theory_lev_ret
            report += f"🔹 {label} 回溯:\n   - {base_symbol} 实际涨跌: {actual_base_ret*100:+.2f}%\n   - {lev_symbol} 理论应有: {theory_lev_ret*100:+.2f}%\n   - {lev_symbol} 真实复合: {actual_lev_ret*100:+.2f}%\n"
            
            if slippage < -0.02: report += f"   - 隐性震荡损耗 (Beta Drag): {slippage*100:+.2f}%\n\n"
            elif slippage > 0.01: report += f"   - 单边复利增益: {slippage*100:+.2f}%\n\n"
            else: report += f"   - 损耗处于合理范围: {slippage*100:+.2f}%\n\n"
            
            if days == len(df): global_slippage = slippage * 100
            
        return report, {"tqqq_drag_pct": round(global_slippage, 2) if global_slippage is not None else None}
    except Exception as e: return report + f"⚠️ 测算报错: {e}\n", {}

# ================= 主控制流 =================
def get_report():
    if not check_market_status(): return 
    if not is_trading_day():
        logger.info(f"[{datetime.now().strftime('%H:%M:%S')}] 🛑 拦截：今天是美股休市日，程序静默退出。")
        return

    ny_now = datetime.now(NY_TZ)
    today_str = ny_now.strftime('%Y-%m-%d')
    report = f"📊 美股战略日报 [轨道一] 盘后绝对客观数据切片\n生成时间: {ny_now.strftime('%Y-%m-%d %H:%M')}\n" + "="*55 + "\n"
    
    macro_data_db = {"date": today_str}
    
    try:
        ib.connect('127.0.0.1', 4001, clientId=318, readonly=True, account='')
        ib.reqMarketDataType(4)

        report += "\n【模块零：日内动能绝对值】\n"
        ib.qualifyContracts(*[Stock('QQQ', 'SMART', 'USD'), Stock('QQQE', 'SMART', 'USD')])
        hist_data_c2c = {}
        hist_data_o2c = {}
        
        for s in [Stock('QQQ', 'SMART', 'USD'), Stock('QQQE', 'SMART', 'USD')]:
            try:
                contracts = ib.qualifyContracts(s)
                if not contracts: continue
                bars = ib.reqHistoricalData(contracts[0], endDateTime='', durationStr='2 D', barSizeSetting='1 day', whatToShow='TRADES', useRTH=True)
                if bars and len(bars) >= 2:
                    hist_data_c2c[s.symbol] = (bars[-1].close - bars[-2].close) / bars[-2].close * 100
                    hist_data_o2c[s.symbol] = (bars[-1].close - bars[-1].open) / bars[-1].open * 100
                elif bars and len(bars) == 1:
                    hist_data_c2c[s.symbol] = 0
                    hist_data_o2c[s.symbol] = (bars[0].close - bars[0].open) / bars[0].open * 100
            except: pass

        qqq_c2c = hist_data_c2c.get('QQQ', 0)
        qqqe_c2c = hist_data_c2c.get('QQQE', 0)
        qqq_o2c = hist_data_o2c.get('QQQ', 0)
        qqqe_o2c = hist_data_o2c.get('QQQE', 0)
        
        diff_c2c = qqq_c2c - qqqe_c2c
        diff_o2c = qqq_o2c - qqqe_o2c

        report += f"【模块零：双轨动能透视】\n"
        report += f"📊 宏观表象 (包含跳空，看总盈亏)\n"
        report += f"- QQQ 总涨跌幅: {qqq_c2c:+.2f}%\n"
        report += f"- QQQE 总涨跌幅: {qqqe_c2c:+.2f}%\n"
        report += f"- 宏观广度差额: {diff_c2c:+.2f}% (正值代表巨头拉盘)\n\n"
        
        report += f"🕵️ 微观真相 (排除跳空，看主力日内买卖)\n"
        report += f"- QQQ 日内真实动能: {qqq_o2c:+.2f}%\n"
        report += f"- QQQE 日内真实动能: {qqqe_o2c:+.2f}%\n"
        report += f"- 日内广度差额: {diff_o2c:+.2f}% (负值代表巨头盘中遭暗中派发)\n\n"
        
        rep_breadth, dict_breadth = get_market_breadth_ib(ib)
        report += rep_breadth
        macro_data_db.update(dict_breadth)
        macro_data_db['qqq_mom_pct'] = round(qqq_c2c, 2)

        rep_drag, dict_drag = get_etf_structural_drag(ib, 'QQQ', 'TQQQ', leverage=3)
        macro_data_db.update(dict_drag)

        report += "\n【模块二：宏观波动率与异动雷达】"
        rep_vix, dict_vix = get_vix_term_structure(ib)
        report += rep_vix
        macro_data_db.update(dict_vix)

        report += "\n【模块三：现货筹码与个股情绪 (全表巡视)】\n"

        rows_written = 0
        for sym in SYMBOLS:
            logger.info(f"-> 正在计算 {sym} 盘后现货与情绪底牌...")
            rep_poc, dict_poc = get_spot_poc_obv_ib(ib, sym)
            report += rep_poc

            if sym == 'QQQ': macro_data_db['qqq_poc_price'] = dict_poc.get('poc_price')
            if sym == 'SPY': macro_data_db['spy_poc_price'] = dict_poc.get('poc_price')

            rep_ivr, dict_ivr = get_ivr_only(ib, sym)
            report += rep_ivr

            report += get_unusual_options_activity(ib, sym)

            # ✅ V9.0：写入独立的盘后现货表（16:00 截面），与盘前期权表解耦
            spot_payload = {
                "date": today_str, "ticker": sym,
                "current_price": dict_poc.get("close_price"),
                "poc_price": dict_poc.get("poc_price"),
                "obv_status": dict_poc.get("obv_status"),
                "ivr_pct": dict_ivr.get("ivr_pct"),
            }
            attach_metadata(spot_payload, source_date=today_str)
            if safe_upsert(supabase, 'stock_spot_post_close', spot_payload, conflict_cols='date,ticker') is not None:
                rows_written += 1

        attach_metadata(macro_data_db, source_date=today_str)
        safe_upsert(supabase, 'macro_spot_daily', macro_data_db, conflict_cols='date')
        logger.info("☁️ ✅ 现货大势与VIX宏观数据已完美推送到 Supabase (macro_spot_daily)！")

        # 数据质量审计
        try:
            macro_missing = find_missing_fields(macro_data_db, ['vix_contango_pct', 'breadth_diff_pct', 'tqqq_drag_pct', 'qqq_poc_price', 'spy_poc_price'])
            log_data_quality(
                supabase, job_name='daily_post_close', table_name='stock_spot_post_close',
                status='ok' if rows_written == len(SYMBOLS) else ('partial' if rows_written > 0 else 'failed'),
                rows_written=rows_written,
                missing_fields=macro_missing if macro_missing else None,
                notes='盘后 16:00 截面',
            )
        except Exception as e:
            logger.warning(f"⚠️ 数据质量记录失败: {e}")

        report += rep_drag
        report += "="*55 + "\n[声明] 轨道一：盘后数据切片生成完毕！"
        
        append_to_spot_db_local(macro_data_db, today_str)
        
        send_email(f"美股盘后复盘 - 客观数据切片", report)
        logger.info("\n✅ 轨道一运行完毕并发送成功！")

    except Exception as e: logger.error(f"❌ 运行失败: {e}")
    finally: ib.disconnect()

if __name__ == "__main__": get_report()