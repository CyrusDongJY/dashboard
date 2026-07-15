import sys
import os
import math
import logging
import smtplib
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.header import Header
from contextlib import contextmanager
import io

import pytz
import pandas as pd
import numpy as np
import requests
from ib_insync import *
from supabase import create_client, Client
import pandas_market_calendars as mcal

from market_utils import (
    attach_metadata, safe_upsert, log_data_quality,
    find_missing_fields, lag_trading_days,
)

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
DB_MAX_RETRIES = 3 # 数据库最大重试次数

SYMBOLS = ['SPY', 'QQQ', 'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'META', 'NVDA', 'TSLA', 'ORCL']

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
def market_data_subscription(ib_instance, contracts, sleep_time):
    """【智能沙盒】确保无论如何发生异常，退出时自动释放行情订阅，杜绝连接泄漏"""
    tickers = []
    try:
        tickers = [ib_instance.reqMktData(c, '100,101', False, False) for c in contracts]
        ib_instance.sleep(sleep_time)
        yield tickers
    finally:
        for ticker in tickers:
            try:
                if getattr(ticker, 'contract', None):
                    ib_instance.cancelMktData(ticker.contract)
            except Exception as e:
                logger.debug(f"取消订阅失败: {e}")

# ✅ V9.0：统一走 market_utils.safe_upsert；保留包装以兼容 ib 事件循环的 sleep
def safe_db_upsert(table_name, data, conflict_cols='date,ticker'):
    """【容灾模块】原生指数退避重试，防止网络瞬间抖动导致数据丢失"""
    return safe_upsert(supabase, table_name, data, conflict_cols=conflict_cols,
                       max_retries=DB_MAX_RETRIES, sleep_fn=ib.sleep)

# ================= 安全取价与数据清洗 =================
def _safe_market_price(ticker):
    try:
        val = None
        if hasattr(ticker, 'marketPrice'):
            try: val = ticker.marketPrice()
            except Exception: val = None
        if val is None or (isinstance(val, float) and math.isnan(val)):
            val = getattr(ticker, 'close', None)
        if val is None or (isinstance(val, float) and math.isnan(val)):
            val = getattr(ticker, 'last', None)
        if val is None:
            return None
        if isinstance(val, (int, float, np.floating)):
            if math.isnan(val): return None
            return float(val)
        return float(val)
    except Exception:
        return None

# ================= 华尔街级手搓期权核算引擎 (Black-Scholes) =================
def norm_cdf(x):
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

def norm_pdf(x):
    return math.exp(-x**2 / 2.0) / math.sqrt(2 * math.pi)

def bs_price(S, K, T, r, sigma, opt_type):
    if T <= 0 or sigma <= 0: return 0.0
    d1 = (math.log(S / K) + (r + sigma**2 / 2.0) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if opt_type == 'C': return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    else: return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)

def bs_vega(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0: return 0.0
    d1 = (math.log(S / K) + (r + sigma**2 / 2.0) * T) / (sigma * math.sqrt(T))
    return S * norm_pdf(d1) * math.sqrt(T)

def implied_volatility(target_price, S, K, T, r, opt_type):
    if target_price <= 0: return 0.001
    if opt_type == 'C' and target_price < (S - K * math.exp(-r*T)): return 0.001
    if opt_type == 'P' and target_price < (K * math.exp(-r*T) - S): return 0.001

    sigma = 0.5 
    for _ in range(50): 
        price = bs_price(S, K, T, r, sigma, opt_type)
        diff = price - target_price
        if abs(diff) < 1e-4: return sigma
        vega = bs_vega(S, K, T, r, sigma)
        if vega < 1e-6: 
            sigma = 0.001 if diff > 0 else 2.0
            break
        sigma -= diff / vega
        if sigma < 0.001: sigma = 0.001
        if sigma > 3.0: sigma = 3.0
    return sigma

def calc_delta_gamma(S, K, T, r, sigma, opt_type):
    if T <= 0 or sigma <= 0: return 0.0, 0.0
    d1 = (math.log(S / K) + (r + sigma**2 / 2.0) * T) / (sigma * math.sqrt(T))
    gamma = norm_pdf(d1) / (S * sigma * math.sqrt(T))
    delta = norm_cdf(d1) if opt_type == 'C' else norm_cdf(d1) - 1.0
    return delta, gamma

def calc_vanna_charm(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0.001 or S <= 0 or K <= 0: return 0, 0
    d1 = (math.log(S / K) + (r + sigma**2 / 2.0) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    pdf_d1 = norm_pdf(d1)
    vanna = -pdf_d1 * (d2 / sigma)
    charm = -pdf_d1 * ((r / (sigma * math.sqrt(T))) - (d2 / (2 * T)))
    return vanna, charm

# ================= 辅助工具与守卫模块 =================
def send_email(subject, body):
    msg = MIMEText(body, 'plain', 'utf-8')
    msg['From'] = cfg.SENDER_EMAIL
    msg['To'] = cfg.RECEIVER_EMAIL
    msg['Subject'] = Header(subject, 'utf-8')
    try:
        # ✅ 补充了 10 秒超时防护
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=10)
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
    if ny_time.hour != 8:
        logger.info(f"🛑 拦截：当前美东时间 {ny_time.strftime('%H:%M')}，非盘前期权测算期 (08:00时段)。")
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

# FINRA Consolidated NMS 场外短售成交量代理
def get_finra_darkpool(tickers):
    """抓取 FINRA 个股场外短售成交量；不等同于空头持仓或机构方向。"""
    for i in range(5):
        date_obj = datetime.now(NY_TZ) - timedelta(days=i)
        date_str = date_obj.strftime("%Y%m%d")
        url = f"https://cdn.finra.org/equity/regsho/daily/CNMSshvol{date_str}.txt"
        try:
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                logger.info(f"✅ 成功命中 FINRA Consolidated NMS 数据源: {date_str}")
                df = pd.read_csv(io.StringIO(response.text), sep='|')
                results = {}
                for ticker in tickers:
                    row = df[df['Symbol'] == ticker]
                    if not row.empty:
                        short_vol = int(row['ShortVolume'].values[0])
                        total_vol = int(row['TotalVolume'].values[0])
                        dpsv_ratio = (short_vol / total_vol) * 100 if total_vol > 0 else 0
                        results[ticker] = {
                            "Date": date_str,
                            "Short_Vol": short_vol,
                            "Total_Vol": total_vol,
                            "DPSV_%": round(dpsv_ratio, 2)
                        }
                return results
        except Exception:
            pass
    logger.warning("❌ 连续 5 天未找到 FINRA 数据。")
    return None

def get_gex_dix():
    report = "\n【模块一：暗池与做市商宏观敞口 (GEX & DIX)】\n"
    try:
        df = pd.read_csv("https://squeezemetrics.com/monitor/static/DIX.csv")
        last_row = df.iloc[-1]
        date_str = last_row['date']
        dix = float(last_row['dix']) * 100
        gex = float(last_row['gex']) / 1e9 

        report += f"📊 隔夜数据更新日期: {date_str}\n   - 暗池指数 (DIX): {dix:.1f}%\n   - 做市商敞口 (GEX): {gex:.2f} 十亿\n"
        
        try:
            today_str = datetime.now(NY_TZ).strftime('%Y-%m-%d')
            # DIX CSV 是隔夜数据：source_date 用文件自带日期，不冒充当日
            payload = {"date": today_str, "dix_pct": dix, "gex_billions": gex}
            attach_metadata(payload, source_date=str(date_str))
            safe_upsert(supabase, 'macro_options_daily', payload, conflict_cols='date')
            logger.info("✅ 宏观敞口数据已推送到 Supabase。")
        except Exception as db_e:
            logger.warning(f"⚠️ 宏观数据推送失败: {db_e}")

        return report
    except Exception as e: return report + f"⚠️ GEX/DIX 数据获取失败: {e}\n"

# ================= 主控制流 =================
def get_report():
    if not check_market_status(): return 

    if not is_trading_day():
        logger.info(f"[{datetime.now().strftime('%H:%M:%S')}] 🛑 拦截：今天是美股休市日，程序静默退出。")
        return

    ny_now = datetime.now(NY_TZ)
    ny_today_date = ny_now.date() 
    today_str = ny_now.strftime('%Y-%m-%d')
    
    report = f"📊 美股战略日报 [轨道二] 盘前 GEX 与期权微观阵地\n生成时间: {ny_now.strftime('%Y-%m-%d %H:%M')}\n" + "="*55 + "\n"
    
    try:
        ib.connect('127.0.0.1', 4001, clientId=319, readonly=True, account='')
        ib.reqMarketDataType(4)

        rows_written = 0  # 数据质量审计：成功入库的标的数

        # 模块一：宏观数据
        report += get_gex_dix()
        
        # 个股FINRA场外短售成交量代理
        darkpool_data = get_finra_darkpool(SYMBOLS)
        report += "\n【模块二：核心个股FINRA场外短售量代理 (DPSV)】\n"
        if darkpool_data:
            # FINRA 日报通常滞后 1-3 个交易日：明示数据真实日期，告警端按新鲜度降权
            finra_date_raw = next(iter(darkpool_data.values()))['Date']
            finra_date = f"{finra_date_raw[:4]}-{finra_date_raw[4:6]}-{finra_date_raw[6:]}"
            finra_lag = lag_trading_days(finra_date)
            report += f"   📅 FINRA 数据日期: {finra_date} (滞后 {finra_lag} 个交易日)\n"
            for sym in ['SPY', 'QQQ', 'TSLA', 'NVDA']:
                if sym in darkpool_data:
                    dpsv = darkpool_data[sym]['DPSV_%']
                    zone = "高读数" if dpsv > 50 else ("低读数" if dpsv < 40 else "中性区")
                    report += f"   - {sym:4} FINRA短售量占比: {dpsv:5.2f}% ({zone}，方向待验证)\n"
        else:
            finra_date, finra_lag = None, None
            report += "   ⚠️ 暗池数据目前暂不可用\n"

        # 模块三（原模块二重命名）：期权数据
        report += "\n【模块三：期权 Gamma 结构与高级微观敞口】\n"

        for sym in SYMBOLS:
            report += f"\n--- {sym} 关键位与微观结构 ---\n"
            
            stock_contracts = ib.qualifyContracts(Stock(sym, 'SMART', 'USD'))
            if not stock_contracts:
                report += f"⚠️ 无法获取 {sym} 合约信息。\n"
                continue
            stock = stock_contracts[0]
            
            bars = ib.reqHistoricalData(stock, endDateTime='', durationStr='1 D', barSizeSetting='1 day', whatToShow='TRADES', useRTH=True)
            if not bars: 
                report += f"⚠️ 无法获取 {sym} 现价。\n"
                continue
            curr_price = bars[-1].close
            report += f"📌 当前现价: ${curr_price:.2f}\n"
            
            chains = ib.reqSecDefOptParams(stock.symbol, '', stock.secType, stock.conId)
            if not chains:
                report += "⚠️ 未找到可用期权链。\n"
                continue
                
            chain = next((c for c in chains if c.exchange == 'SMART' and getattr(c, 'tradingClass', None) == stock.symbol), None)
            if not chain: chain = next((c for c in chains if c.exchange == 'SMART'), None)
            if not chain or not getattr(chain, 'expirations', None) or not getattr(chain, 'strikes', None):
                report += "⚠️ 期权链数据缺失。\n"
                continue
            
            valid_exps = []
            for exp in sorted(chain.expirations):
                try:
                    exp_date = datetime.strptime(exp, '%Y%m%d').date()
                    if (exp_date - ny_today_date).days >= 0:
                        valid_exps.append(exp)
                except: pass
                
            if not valid_exps: 
                report += "⚠️ 无有效未到期合约。\n"
                continue
            
            short_exp = valid_exps[0]
            long_exp = next((exp for exp in valid_exps if 15 <= (datetime.strptime(exp, '%Y%m%d').date() - ny_today_date).days <= 45), valid_exps[-1])
            target_exps = list(set([short_exp, long_exp]))

            valid_strikes = sorted([s for s in list(chain.strikes) if s % 0.5 == 0])
            if not valid_strikes: continue
            
            closest_strike = min(valid_strikes, key=lambda x: abs(x - curr_price))
            closest_idx = valid_strikes.index(closest_strike)
            target_strikes = valid_strikes[max(0, closest_idx - 25):min(len(valid_strikes), closest_idx + 26)]
            
            opts = [Option(sym, exp, s, r, 'SMART', tradingClass=getattr(chain, 'tradingClass', None)) for exp in target_exps for s in target_strikes for r in ['C', 'P']]
            try: contracts = ib.qualifyContracts(*opts)
            except Exception: contracts = []
            
            if not contracts: continue
            
            rows = []
            for i in range(0, len(contracts), BATCH_SIZE):
                batch = contracts[i:i+BATCH_SIZE]
                
                with market_data_subscription(ib, batch, SLEEP_LONG) as tickers:
                    for t in tickers:
                        if not getattr(t, 'contract', None): continue
                        r = getattr(t.contract, 'right', None)
                        if not r: continue
                        
                        oi = getattr(t, 'callOpenInterest' if r == 'C' else 'putOpenInterest', getattr(t, 'openInterest', 0))
                        if oi is None or (isinstance(oi, float) and math.isnan(oi)): oi = 0
                        
                        if oi > 0:
                            price = _safe_market_price(t)
                            if price is not None and price > 0:
                                s = t.contract.strike
                                exp_date_str = t.contract.lastTradeDateOrContractMonth
                                exp_date_obj = datetime.strptime(exp_date_str, '%Y%m%d').date()
                                dte_days = (exp_date_obj - ny_today_date).days
                                T = max(dte_days / 365.0, 0.002) 
                                
                                iv = implied_volatility(price, curr_price, s, T, 0.053, r)
                                delta, gamma = calc_delta_gamma(curr_price, s, T, 0.053, iv, r)
                                v, c = calc_vanna_charm(curr_price, s, T, 0.053, iv)
                                if r == 'P': v, c = -v, -c
                                
                                v_per_1pct = v * 0.01
                                c_per_day = c / 365.0
                                
                                gamma_shares = oi * gamma * 100
                                vanna_shares = oi * v_per_1pct * 100
                                charm_shares = oi * c_per_day * 100
                                
                                rows.append({
                                    'Exp': exp_date_str, 'S': s, 'R': r, 'OI': oi, 'Price': price,
                                    'IV': iv, 'Delta': delta, 'Gamma_W': gamma_shares, 
                                    'Vanna_W': vanna_shares, 'Charm_W': charm_shares
                                })
                                
                ib.sleep(SLEEP_SHORT)
            
            if rows:
                df = pd.DataFrame(rows)
                
                max_oi_strike, max_oi_type = None, None
                if not df.empty and df['OI'].sum() > 0:
                    max_oi_idx = df['OI'].idxmax()
                    max_oi_row = df.loc[max_oi_idx]
                    max_oi_strike = float(max_oi_row['S'])
                    max_oi_type = str(max_oi_row['R'])

                expected_move_pct = 0.0
                expected_move = 0.0
                if not df.empty:
                    atm_strike = min(df['S'].unique(), key=lambda x: abs(x - curr_price))
                    atm_call = df[(df['S'] == atm_strike) & (df['R'] == 'C') & (df['Exp'] == short_exp)]
                    atm_put = df[(df['S'] == atm_strike) & (df['R'] == 'P') & (df['Exp'] == short_exp)]
                    atm_call_px = atm_call['Price'].mean() if not atm_call.empty else 0
                    atm_put_px = atm_put['Price'].mean() if not atm_put.empty else 0
                    if pd.isna(atm_call_px): atm_call_px = 0
                    if pd.isna(atm_put_px): atm_put_px = 0
                    expected_move = atm_call_px + atm_put_px
                    expected_move_pct = (expected_move / curr_price) * 100 if curr_price > 0 else 0

                total_call_oi = df[df['R']=='C']['OI'].sum()
                total_put_oi = df[df['R']=='P']['OI'].sum()
                oi_pcr = total_put_oi / total_call_oi if total_call_oi > 0 else 0
                
                iv_skew = 0
                calls = df[(df['R']=='C') & (df['Delta'] > 0)].dropna(subset=['IV', 'Delta'])
                puts = df[(df['R']=='P') & (df['Delta'] < 0)].dropna(subset=['IV', 'Delta'])
                if not calls.empty and not puts.empty:
                    call_25 = calls.iloc[(calls['Delta'] - 0.25).abs().argsort()[:1]]
                    put_25 = puts.iloc[(puts['Delta'] - (-0.25)).abs().argsort()[:1]]
                    if not call_25.empty and not put_25.empty:
                        put_iv, call_iv = put_25['IV'].values[0], call_25['IV'].values[0]
                        if put_iv > 0 and call_iv > 0: iv_skew = put_iv - call_iv

                df_short = df[df['Exp'] == short_exp]
                df_long = df[df['Exp'] == long_exp]
                
                short_net_gamma = df_short[df_short['R']=='C'].groupby('S')['Gamma_W'].sum().add(-df_short[df_short['R']=='P'].groupby('S')['Gamma_W'].sum(), fill_value=0).dropna()
                long_net_gamma = df_long[df_long['R']=='C'].groupby('S')['Gamma_W'].sum().add(-df_long[df_long['R']=='P'].groupby('S')['Gamma_W'].sum(), fill_value=0).dropna()
                
                short_gamma_total = short_net_gamma.sum() / 1e6
                long_gamma_total = long_net_gamma.sum() / 1e6
                
                zgl_strike = float(short_net_gamma.abs().idxmin()) if not short_net_gamma.empty else 0.0
                
                calls_s = df_short[df_short['R']=='C'].sort_values('Gamma_W', ascending=False)
                puts_s = df_short[df_short['R']=='P'].sort_values('Gamma_W', ascending=False)
                call_w = float(calls_s.iloc[0]['S']) if not calls_s.empty else None
                put_w = float(puts_s.iloc[0]['S']) if not puts_s.empty else None
                
                vanna_m = df_short['Vanna_W'].sum() / 1e6
                charm_m = df_short['Charm_W'].sum() / 1e6
                
                report += f"⚖️ OI PCR (隔夜真实多空比): {oi_pcr:.2f}\n"
                report += f"📉 IV Skew (恐慌指数偏斜): {iv_skew*100:+.2f}%\n"
                report += f"📏 预期振幅 (Expected Move): ±${expected_move:.2f} (±{expected_move_pct:.2f}%)\n"
                report += f"⚔️ 资金主战场 (最大OI): ${max_oi_strike} {'看涨(Call)' if max_oi_type == 'C' else '看跌(Put)'}\n" 
                report += f"🧱 短期防线 ({short_exp}): Call 墙 ${call_w} / Put 墙 ${put_w}\n"
                report += f"🌊 短期净 Gamma ({short_exp}): {short_gamma_total:+.2f} M\n"
                report += f"🌊 中长波段净 Gamma ({long_exp}): {long_gamma_total:+.2f} M\n"
                report += f"🧲 零伽马分水岭 (ZGL): ${zgl_strike:.2f}\n"
                report += f"🌪️ 做市商动态对冲: Vanna {vanna_m:+.2f} M | Charm {charm_m:+.2f} M/天\n"

                try:
                    # ✅ V9.0：写入独立的盘前表 stock_options_pre_market（与盘后现货解耦，避免时点错配）
                    payload = {
                        "date": today_str, "ticker": sym, "current_price": round(curr_price, 2),
                        "zgl_price": float(zgl_strike), "call_wall": call_w, "put_wall": put_w,
                        "vanna_m": round(vanna_m, 2), "charm_m": round(charm_m, 2),
                        "expected_move_pct": round(expected_move_pct, 2),
                        "max_oi_strike": max_oi_strike, "max_oi_type": max_oi_type,
                        "iv_skew": round(iv_skew, 4), "short_gamma_m": round(short_gamma_total, 2), "long_gamma_m": round(long_gamma_total, 2), "oi_pcr": round(oi_pcr, 2)
                    }

                    if darkpool_data and sym in darkpool_data:
                        payload["dpsv_pct"] = darkpool_data[sym]['DPSV_%']
                        # DPSV 用 FINRA 文件真实日期，不再冒充当日数据
                        payload["dpsv_source_date"] = finra_date

                    attach_metadata(payload, source_date=today_str)
                    if safe_upsert(supabase, 'stock_options_pre_market', payload, conflict_cols='date,ticker') is not None:
                        rows_written += 1
                    logger.info(f"✅ [{sym}] 进阶期权阵地数据推送成功！")
                except Exception as e:
                    logger.warning(f"⚠️ {sym} 入库异常: {e}")

            else: report += f"⚠️ 冻结期权数据获取失败。\n"

        report += "\n" + "="*55 + "\n[声明] 轨道二：盘前期权阵地扫描完毕！"

        # 数据质量审计：记录本次抓取覆盖率与 FINRA 滞后
        try:
            missing_syms = [s for s in SYMBOLS if darkpool_data is None or s not in darkpool_data]
            log_data_quality(
                supabase, job_name='daily_pre_market', table_name='stock_options_pre_market',
                status='ok' if rows_written == len(SYMBOLS) else ('partial' if rows_written > 0 else 'failed'),
                rows_written=rows_written,
                missing_fields=[f"dpsv:{s}" for s in missing_syms] if missing_syms else None,
                lag_days=finra_lag or 0,
                notes=f"FINRA源日期: {finra_date or '不可用'}",
            )
        except Exception as e:
            logger.warning(f"⚠️ 数据质量记录失败: {e}")

        send_email(f"美股盘前客观数据切片 - 纯净高阶版", report)
        logger.info("\n✅ 轨道二运行完毕并发送成功！")

    except Exception as e: logger.error(f"❌ 运行失败: {e}")
    finally: ib.disconnect()

if __name__ == "__main__": get_report()
