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
    lag_trading_days,
)
from pre_market_metrics import (
    DISTANCE_SIGN_VERSION,
    GAMMA_BUCKETS,
    apply_gamma_quality_gate,
    calc_delta_gamma,
    calc_vanna_charm,
    distance_pct,
    expected_move_metrics,
    expiration_bucket,
    finite_number,
    format_premarket_quality_summary,
    format_premarket_symbol_summary,
    gamma_structure,
    implied_volatility,
    max_oi_metrics,
    put_call_oi_ratio,
    quote_midpoint,
    select_expirations,
    standard_monthly_oi_walls,
    time_to_expiry_years,
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
SLEEP_LONG = 4
DB_MAX_RETRIES = 3 # 数据库最大重试次数
MAX_GAMMA_DTE = int(getattr(cfg, "MAX_GAMMA_DTE", 60))
MAX_STRIKES = 31
FUTURES_MAP = {
    'SPY': ('ES', 'CME'),
    'QQQ': ('NQ', 'CME'),
}

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


def _bar_timestamp(value):
    try:
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is None:
            return timestamp.tz_localize(NY_TZ)
        return timestamp.tz_convert(NY_TZ)
    except Exception:
        return None


def _historical_price_snapshot(contract, ny_now):
    previous_close = None
    previous_close_date = None
    premarket_last = None
    premarket_last_time = None
    try:
        daily_bars = ib.reqHistoricalData(
            contract, endDateTime='', durationStr='5 D', barSizeSetting='1 day',
            whatToShow='TRADES', useRTH=True, formatDate=2)
        for bar in reversed(daily_bars or []):
            timestamp = _bar_timestamp(bar.date)
            if timestamp is not None and timestamp.date() < ny_now.date():
                previous_close = finite_number(bar.close, positive=True)
                previous_close_date = timestamp.date().isoformat()
                break
    except Exception as e:
        logger.warning(f"{contract.symbol} 昨收获取失败: {e}")

    try:
        intraday_bars = ib.reqHistoricalData(
            contract, endDateTime='', durationStr='1 D', barSizeSetting='1 min',
            whatToShow='TRADES', useRTH=False, formatDate=2)
        candidates = []
        for bar in intraday_bars or []:
            timestamp = _bar_timestamp(bar.date)
            if (
                timestamp is not None
                and timestamp.date() == ny_now.date()
                and timestamp.time() < datetime.strptime("09:30", "%H:%M").time()
            ):
                candidates.append((timestamp, finite_number(bar.close, positive=True)))
        candidates = [item for item in candidates if item[1] is not None]
        if candidates:
            premarket_last_time, premarket_last = candidates[-1]
    except Exception as e:
        logger.warning(f"{contract.symbol} 盘前成交获取失败: {e}")
    return {
        "previous_close": previous_close,
        "previous_close_date": previous_close_date,
        "premarket_last": premarket_last,
        "premarket_last_time": (
            premarket_last_time.astimezone(timezone.utc).isoformat()
            if premarket_last_time is not None else None
        ),
    }


def _streaming_quote(contract):
    preferred = int(getattr(cfg, "IB_MARKET_DATA_TYPE", 1))
    result = {
        "bid": None, "ask": None, "last": None,
        "market_data_type": None, "quote_as_of": None,
    }
    # Always try a current quote before delayed/frozen fallbacks, even if an old
    # deployment still has IB_MARKET_DATA_TYPE=4 in its configuration.
    for requested_type in dict.fromkeys((1, 3, preferred, 4)):
        ticker = None
        try:
            ib.reqMarketDataType(requested_type)
            ticker = ib.reqMktData(contract, '233', False, False)
            ib.sleep(SLEEP_SHORT)
            actual_type = int(
                finite_number(getattr(ticker, "marketDataType", requested_type))
                or requested_type)
            bid = finite_number(getattr(ticker, "bid", None), positive=True)
            ask = finite_number(getattr(ticker, "ask", None), positive=True)
            last = finite_number(getattr(ticker, "last", None), positive=True)
            result.update({
                "bid": bid,
                "ask": ask,
                "last": last,
                "market_data_type": actual_type,
                "quote_as_of": datetime.now(timezone.utc).isoformat(),
            })
            if bid is not None or ask is not None or (
                last is not None and actual_type in (1, 3)
            ):
                break
        except Exception as e:
            logger.debug(f"{contract.symbol} 行情类型{requested_type}不可用: {e}")
        finally:
            if ticker is not None:
                try:
                    ib.cancelMktData(contract)
                except Exception:
                    pass
    return result


def get_price_snapshot(contract, ny_now):
    historical = _historical_price_snapshot(contract, ny_now)
    quote = _streaming_quote(contract)
    market_type = quote.get("market_data_type")
    bid = quote.get("bid") if market_type in (1, 3) else None
    ask = quote.get("ask") if market_type in (1, 3) else None
    midpoint = quote_midpoint(bid, ask)
    premarket_last = historical.get("premarket_last")
    premarket_last_as_of = historical.get("premarket_last_time")
    if premarket_last is None and market_type in (1, 3):
        premarket_last = quote.get("last")
        premarket_last_as_of = quote.get("quote_as_of")
    reference = midpoint or premarket_last or historical.get("previous_close")
    if midpoint is not None:
        source = "PREMARKET_MID"
    elif premarket_last is not None:
        source = "PREMARKET_LAST"
    elif historical.get("previous_close") is not None:
        source = "PREVIOUS_CLOSE_FALLBACK"
    else:
        source = "MISSING"
    if source == "PREMARKET_MID":
        reference_as_of = quote.get("quote_as_of")
    elif source == "PREMARKET_LAST":
        reference_as_of = premarket_last_as_of
    else:
        reference_as_of = None
    return {
        **historical,
        "premarket_last": premarket_last,
        "premarket_bid": bid,
        "premarket_ask": ask,
        "premarket_mid": midpoint,
        "reference_price": reference,
        "price_source": source,
        "market_data_type": market_type,
        "quote_as_of": reference_as_of,
    }


def get_futures_implied_reference(symbol, previous_close, ny_now):
    mapping = FUTURES_MAP.get(symbol)
    previous_close = finite_number(previous_close, positive=True)
    if mapping is None or previous_close is None:
        return {"symbol": None, "price": None, "as_of": None}
    root, exchange = mapping
    try:
        details = ib.reqContractDetails(Future(root, '', exchange, currency='USD'))
        candidates = []
        for detail in details or []:
            expiration = str(
                getattr(detail.contract, "lastTradeDateOrContractMonth", ""))[:8]
            if expiration and expiration >= ny_now.strftime("%Y%m%d"):
                candidates.append((expiration, detail.contract))
        if not candidates:
            return {"symbol": root, "price": None, "as_of": None}
        roll_guard = (ny_now.date() + timedelta(days=5)).strftime("%Y%m%d")
        guarded = [item for item in candidates if item[0] > roll_guard]
        contract = sorted(guarded or candidates, key=lambda item: item[0])[0][1]
        bars = ib.reqHistoricalData(
            contract, endDateTime='', durationStr='5 D', barSizeSetting='5 mins',
            whatToShow='TRADES', useRTH=False, formatDate=2)
        by_date = {}
        for bar in bars or []:
            timestamp = _bar_timestamp(bar.date)
            price = finite_number(bar.close, positive=True)
            if timestamp is not None and price is not None:
                by_date.setdefault(timestamp.date(), []).append((timestamp, price))
        current = by_date.get(ny_now.date(), [])
        prior_dates = sorted(day for day in by_date if day < ny_now.date())
        if not current or not prior_dates:
            return {"symbol": contract.localSymbol or root, "price": None, "as_of": None}
        current_time, futures_now = current[-1]
        futures_previous = by_date[prior_dates[-1]][-1][1]
        implied = previous_close * futures_now / futures_previous
        return {
            "symbol": contract.localSymbol or root,
            "price": implied,
            "as_of": current_time.astimezone(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.warning(f"{symbol} 期货映射参考价获取失败: {e}")
        return {"symbol": root, "price": None, "as_of": None}


def get_previous_premarket_record(symbol, report_date):
    try:
        response = (
            supabase.table("stock_options_pre_market")
            .select("*")
            .eq("ticker", symbol)
            .lt("date", report_date)
            .order("date", desc=True)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None
    except Exception as e:
        logger.debug(f"{symbol} 前一盘前快照读取失败: {e}")
        return None


def _fmt(value, digits=2, prefix=""):
    value = finite_number(value)
    return f"{prefix}{value:.{digits}f}" if value is not None else "NA"


def _round_or_none(value, digits=4):
    value = finite_number(value)
    return round(value, digits) if value is not None else None

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
    report = "\n【市场背景】\n"
    try:
        df = pd.read_csv("https://squeezemetrics.com/monitor/static/DIX.csv")
        last_row = df.iloc[-1]
        date_str = last_row['date']
        dix = float(last_row['dix']) * 100
        gex = float(last_row['gex']) / 1e9 

        report += (
            f"DIX {dix:.1f}% | GEX {gex:.2f}十亿 | "
            f"源日期 {date_str}\n")
        
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
    
    report = (
        f"美股盘前监测摘要 | {ny_now.strftime('%Y-%m-%d %H:%M')} ET\n"
        "说明：正距离表示关键位在参考价上方；完整明细已写入数据库。\n")
    
    try:
        ib.connect('127.0.0.1', 4001, clientId=319, readonly=True, account='')
        ib.reqMarketDataType(4)

        rows_written = 0  # 数据质量审计：成功入库的标的数
        quality_issues = []

        # 模块一：宏观数据
        report += get_gex_dix()
        
        # 个股FINRA场外短售成交量代理
        darkpool_data = get_finra_darkpool(SYMBOLS)
        report += "\n【FINRA场外短售量代理】\n"
        if darkpool_data:
            # FINRA 日报通常滞后 1-3 个交易日：明示数据真实日期，告警端按新鲜度降权
            finra_date_raw = next(iter(darkpool_data.values()))['Date']
            finra_date = f"{finra_date_raw[:4]}-{finra_date_raw[4:6]}-{finra_date_raw[6:]}"
            finra_lag = lag_trading_days(finra_date)
            report += f"源日期 {finra_date} | 滞后 {finra_lag} 个交易日\n"
            for sym in ['SPY', 'QQQ', 'TSLA', 'NVDA']:
                if sym in darkpool_data:
                    dpsv = darkpool_data[sym]['DPSV_%']
                    zone = "高读数" if dpsv > 50 else ("低读数" if dpsv < 40 else "中性区")
                    report += f"{sym} {dpsv:.2f}%({zone}) | "
            report = report.rstrip(" | ") + "\n"
        else:
            finra_date, finra_lag = None, None
            report += "不可用\n"

        # 邮件只展示决策摘要；全量期权结构继续写入数据库。
        report += "\n【标的速览】\n"

        for sym in SYMBOLS:
            stock_contracts = ib.qualifyContracts(Stock(sym, 'SMART', 'USD'))
            if not stock_contracts:
                report += f"{sym} | 合约信息不可用\n"
                continue
            stock = stock_contracts[0]

            price_snapshot = get_price_snapshot(stock, ny_now)
            curr_price = price_snapshot.get("reference_price")
            if curr_price is None:
                report += f"{sym} | 昨收与盘前报价均不可用\n"
                quality_issues.append(f"price:{sym}")
                continue
            futures_ref = get_futures_implied_reference(
                sym, price_snapshot.get("previous_close"), ny_now)
            market_type = price_snapshot.get("market_data_type")
            market_label = {
                1: "实时", 2: "冻结", 3: "延迟", 4: "延迟冻结",
            }.get(market_type, "未知")
            if price_snapshot.get("price_source") == "PREVIOUS_CLOSE_FALLBACK":
                quality_issues.append(f"premarket_quote:{sym}")
            elif market_type != 1:
                quality_issues.append(f"premarket_not_live:{sym}")
            if sym in FUTURES_MAP and futures_ref.get("price") is None:
                quality_issues.append(f"futures_ref:{sym}")
            
            chains = ib.reqSecDefOptParams(stock.symbol, '', stock.secType, stock.conId)
            if not chains:
                report += f"{sym} | 期权链不可用\n"
                continue
                
            chain = next((c for c in chains if c.exchange == 'SMART' and getattr(c, 'tradingClass', None) == stock.symbol), None)
            if not chain: chain = next((c for c in chains if c.exchange == 'SMART'), None)
            if not chain or not getattr(chain, 'expirations', None) or not getattr(chain, 'strikes', None):
                report += f"{sym} | 期权链字段缺失\n"
                continue
            
            target_exps, expiration_coverage = select_expirations(
                chain.expirations, ny_today_date, horizon_days=MAX_GAMMA_DTE)
            if not target_exps:
                report += f"{sym} | 无有效未到期合约\n"
                quality_issues.append(f"expirations:{sym}")
                continue
            short_exp = target_exps[0]

            valid_strikes = sorted(
                s for s in chain.strikes
                if finite_number(s, positive=True) is not None
                and curr_price * 0.80 <= float(s) <= curr_price * 1.20)
            if not valid_strikes:
                report += f"{sym} | 参考价±20%内无有效执行价\n"
                quality_issues.append(f"strikes:{sym}")
                continue
            closest_strike = min(valid_strikes, key=lambda x: abs(x - curr_price))
            closest_idx = valid_strikes.index(closest_strike)
            half_window = MAX_STRIKES // 2
            target_strikes = valid_strikes[
                max(0, closest_idx - half_window):
                min(len(valid_strikes), closest_idx + half_window + 1)]
            
            opts = [Option(sym, exp, s, r, 'SMART', tradingClass=getattr(chain, 'tradingClass', None)) for exp in target_exps for s in target_strikes for r in ['C', 'P']]
            requested_counts = {
                bucket: len(expiration_coverage.get(bucket, []))
                * len(target_strikes) * 2
                for bucket in GAMMA_BUCKETS if bucket != "ALL"
            }
            requested_counts["ALL"] = sum(requested_counts.values())
            try: contracts = ib.qualifyContracts(*opts)
            except Exception: contracts = []
            
            if not contracts:
                report += f"{sym} | 期权合约资格确认失败\n"
                quality_issues.append(f"option_qualification:{sym}")
                continue

            qualified_counts = {bucket: 0 for bucket in GAMMA_BUCKETS}
            for contract in contracts:
                try:
                    expiry = datetime.strptime(
                        contract.lastTradeDateOrContractMonth[:8], '%Y%m%d').date()
                    bucket = expiration_bucket((expiry - ny_today_date).days)
                    if bucket:
                        qualified_counts[bucket] += 1
                        qualified_counts["ALL"] += 1
                except (TypeError, ValueError):
                    continue
            
            rows = []
            for i in range(0, len(contracts), BATCH_SIZE):
                batch = contracts[i:i+BATCH_SIZE]
                
                with market_data_subscription(ib, batch, SLEEP_LONG) as tickers:
                    for t in tickers:
                        if not getattr(t, 'contract', None): continue
                        r = getattr(t.contract, 'right', None)
                        if not r: continue
                        
                        oi = getattr(t, 'callOpenInterest' if r == 'C' else 'putOpenInterest', getattr(t, 'openInterest', 0))
                        oi = finite_number(oi, positive=True)
                        if oi is None:
                            continue
                        s = float(t.contract.strike)
                        exp_date_str = t.contract.lastTradeDateOrContractMonth[:8]
                        exp_date_obj = datetime.strptime(exp_date_str, '%Y%m%d').date()
                        dte_days = (exp_date_obj - ny_today_date).days
                        years = time_to_expiry_years(exp_date_str, ny_now)
                        if years is None:
                            continue
                        bid = finite_number(getattr(t, "bid", None), positive=True)
                        ask = finite_number(getattr(t, "ask", None), positive=True)
                        mid = quote_midpoint(bid, ask)
                        last = finite_number(getattr(t, "last", None), positive=True)
                        option_price = mid or last
                        model_greeks = getattr(t, "modelGreeks", None)
                        model_iv = finite_number(
                            getattr(model_greeks, "impliedVol", None), positive=True)
                        iv = (
                            model_iv if model_iv is not None and model_iv <= 4.0
                            else implied_volatility(
                                option_price, curr_price, s, years, 0.053, r)
                        )
                        delta, gamma = (None, None)
                        vanna, charm = (None, None)
                        if iv is not None:
                            delta, gamma = calc_delta_gamma(
                                curr_price, s, years, 0.053, iv, r)
                            vanna, charm = calc_vanna_charm(
                                curr_price, s, years, 0.053, iv)
                        if r == 'P':
                            vanna = -vanna if vanna is not None else None
                            charm = -charm if charm is not None else None
                        rows.append({
                            'Exp': exp_date_str, 'S': s, 'R': r, 'OI': oi,
                            'Bid': bid, 'Ask': ask, 'Mid': mid, 'Last': last,
                            'IV': iv, 'Delta': delta, 'T': years, 'DTE': dte_days,
                            'Vanna_W': (
                                oi * vanna * 0.01 * 100 if vanna is not None else None),
                            'Charm_W': (
                                oi * charm / 365.0 * 100 if charm is not None else None),
                        })
                                
                ib.sleep(0.5)
            
            if rows:
                df = pd.DataFrame(rows)
                expected_move = expected_move_metrics(df, curr_price, short_exp)
                if expected_move["quality"] == "MISSING":
                    quality_issues.append(f"expected_move:{sym}")
                oi_pcr = put_call_oi_ratio(df)
                if oi_pcr is None:
                    quality_issues.append(f"oi_pcr:{sym}")
                max_oi = max_oi_metrics(df, curr_price)
                monthly_walls = standard_monthly_oi_walls(
                    df, today_str, curr_price)
                if monthly_walls["quality"] != "OK":
                    quality_issues.append(
                        f"monthly_wall:{sym}:{monthly_walls['quality']}")
                oi_valid_counts = {bucket: 0 for bucket in GAMMA_BUCKETS}
                for bucket in GAMMA_BUCKETS:
                    if bucket == "ALL":
                        oi_valid_counts[bucket] = int(len(df))
                    else:
                        oi_valid_counts[bucket] = int(
                            df["DTE"].apply(expiration_bucket).eq(bucket).sum())
                gamma = apply_gamma_quality_gate(
                    gamma_structure(df, curr_price),
                    requested_counts, qualified_counts, oi_valid_counts)
                for bucket in GAMMA_BUCKETS:
                    if gamma[bucket]["quality"] not in ("OK", "NO_EXPIRY"):
                        quality_issues.append(
                            f"gamma_{bucket.lower()}:{sym}:{gamma[bucket]['quality']}")
                
                iv_skew = None
                calls = df[(df['R']=='C') & (df['Delta'] > 0)].dropna(subset=['IV', 'Delta'])
                puts = df[(df['R']=='P') & (df['Delta'] < 0)].dropna(subset=['IV', 'Delta'])
                if not calls.empty and not puts.empty:
                    call_25 = calls.iloc[(calls['Delta'] - 0.25).abs().argsort()[:1]]
                    put_25 = puts.iloc[(puts['Delta'] - (-0.25)).abs().argsort()[:1]]
                    if not call_25.empty and not put_25.empty:
                        put_iv, call_iv = put_25['IV'].values[0], call_25['IV'].values[0]
                        if put_iv > 0 and call_iv > 0:
                            iv_skew = put_iv - call_iv

                near = df[df["DTE"] <= 7]
                vanna_m = pd.to_numeric(
                    near.get('Vanna_W'), errors='coerce').sum(min_count=1) / 1e6
                charm_m = pd.to_numeric(
                    near.get('Charm_W'), errors='coerce').sum(min_count=1) / 1e6
                active_near = [
                    bucket for bucket in ("0DTE", "1-7D")
                    if gamma[bucket]["requested_contract_count"] > 0
                ]
                near_quality_ok = bool(active_near) and all(
                    gamma[bucket]["quality"] == "OK" for bucket in active_near)
                if not near_quality_ok:
                    vanna_m = None
                    charm_m = None
                short_values = [
                    gamma[bucket]["net_gamma_m"] for bucket in ("0DTE", "1-7D")
                    if gamma[bucket]["net_gamma_m"] is not None
                ]
                short_gamma_total = sum(short_values) if short_values else None
                long_gamma_total = gamma["8-30D"]["net_gamma_m"]
                zgl_strike = gamma["ALL"]["primary_flip"]
                call_w, put_w = gamma["call_wall"], gamma["put_wall"]
                gamma_expirations = {
                    bucket: gamma[bucket]["expirations"] for bucket in GAMMA_BUCKETS
                }
                previous_context = get_previous_premarket_record(sym, today_str)
                previous_flip = None
                gamma_flip_change_pct = None
                gamma_roll_changed = None
                gamma_flip_quality = (
                    gamma["ALL"]["quality"]
                    if gamma["ALL"]["quality"] != "OK"
                    else ("NO_CROSSING" if zgl_strike is None else "BASELINE_RESET")
                )
                if previous_context and gamma["ALL"]["quality"] == "OK":
                    previous_flip = finite_number(
                        previous_context.get(
                            "gamma_flip_all", previous_context.get("zgl_price")),
                        positive=True)
                    previous_model = previous_context.get("gamma_sign_model")
                    previous_expirations = previous_context.get("gamma_expirations")
                    gamma_roll_changed = (
                        previous_expirations != gamma_expirations
                        if previous_expirations is not None else None)
                    if (
                        zgl_strike is not None and previous_flip is not None
                        and previous_model == gamma["sign_model"]
                    ):
                        gamma_flip_change_pct = (
                            (zgl_strike - previous_flip) / curr_price * 100.0)
                        jump_threshold = max(10.0, curr_price * 0.05)
                        if abs(zgl_strike - previous_flip) >= jump_threshold:
                            gamma_flip_quality = "JUMP_REVIEW"
                            quality_issues.append(f"gamma_flip_jump:{sym}")
                        elif gamma_roll_changed:
                            gamma_flip_quality = "ROLL_CHANGED"
                        else:
                            gamma_flip_quality = "OK"
                    elif previous_model and previous_model != gamma["sign_model"]:
                        gamma_flip_quality = "METHOD_CHANGED"

                report += format_premarket_symbol_summary(
                    sym,
                    reference_price=curr_price,
                    price_source=price_snapshot.get("price_source"),
                    market_label=market_label,
                    previous_close=price_snapshot.get("previous_close"),
                    expected_move=expected_move,
                    oi_pcr=oi_pcr,
                    iv_skew=iv_skew,
                    gamma=gamma,
                    gamma_flip_quality=gamma_flip_quality,
                ) + "\n"

                try:
                    # ✅ V9.0：写入独立的盘前表 stock_options_pre_market（与盘后现货解耦，避免时点错配）
                    gamma_zeroes = {
                        bucket: gamma[bucket]["zero_points"] for bucket in GAMMA_BUCKETS
                    }
                    payload = {
                        "date": today_str, "ticker": sym,
                        # 兼容旧下游；新代码必须读取下方明确命名字段。
                        "current_price": _round_or_none(curr_price, 2),
                        "previous_close": _round_or_none(price_snapshot.get("previous_close"), 2),
                        "previous_close_date": price_snapshot.get("previous_close_date"),
                        "premarket_last": _round_or_none(price_snapshot.get("premarket_last"), 2),
                        "premarket_bid": _round_or_none(price_snapshot.get("premarket_bid"), 2),
                        "premarket_ask": _round_or_none(price_snapshot.get("premarket_ask"), 2),
                        "premarket_mid": _round_or_none(price_snapshot.get("premarket_mid"), 2),
                        "premarket_reference_price": _round_or_none(curr_price, 2),
                        "premarket_price_source": price_snapshot.get("price_source"),
                        "market_data_type": market_type,
                        "quote_as_of": price_snapshot.get("quote_as_of"),
                        "futures_symbol": futures_ref.get("symbol"),
                        "futures_reference_price": _round_or_none(futures_ref.get("price"), 2),
                        "futures_as_of": futures_ref.get("as_of"),
                        "zgl_price": _round_or_none(zgl_strike, 4),
                        "call_wall": _round_or_none(call_w, 4),
                        "put_wall": _round_or_none(put_w, 4),
                        "distance_to_call_wall_pct": _round_or_none(distance_pct(call_w, curr_price)),
                        "distance_to_put_wall_pct": _round_or_none(distance_pct(put_w, curr_price)),
                        "distance_to_zgl_pct": _round_or_none(distance_pct(zgl_strike, curr_price)),
                        "distance_sign_version": DISTANCE_SIGN_VERSION,
                        "pin_strike": _round_or_none(gamma["pin_strike"], 4),
                        "pin_state": gamma["pin_state"],
                        "vanna_m": _round_or_none(vanna_m, 4),
                        "charm_m": _round_or_none(charm_m, 4),
                        "expected_move_value": _round_or_none(expected_move["value"], 4),
                        "expected_move_pct": _round_or_none(expected_move["pct"], 4),
                        "expected_move_source": expected_move["source"],
                        "expected_move_dte": _round_or_none(expected_move["dte"], 6),
                        "expected_move_quality": expected_move["quality"],
                        "max_oi_strike": _round_or_none(max_oi["strike"], 4),
                        "max_oi_type": max_oi["right"],
                        "max_oi_expiry": (
                            datetime.strptime(max_oi["expiration"], "%Y%m%d").date().isoformat()
                            if max_oi["expiration"] else None),
                        "max_oi_count": max_oi["oi"],
                        "max_oi_delta": _round_or_none(max_oi["delta"], 6),
                        "max_oi_gamma_dollar_m": _round_or_none(max_oi["gamma_dollar_m"], 6),
                        "max_oi_distance_pct": _round_or_none(max_oi["distance_pct"]),
                        "monthly_wall_expiry": monthly_walls["expiry"],
                        "monthly_call_wall": _round_or_none(
                            monthly_walls["call_wall"], 4),
                        "monthly_put_wall": _round_or_none(
                            monthly_walls["put_wall"], 4),
                        "monthly_call_wall_oi": monthly_walls["call_oi"],
                        "monthly_put_wall_oi": monthly_walls["put_oi"],
                        "monthly_wall_oi_source_date": price_snapshot.get(
                            "previous_close_date"),
                        "monthly_wall_method": monthly_walls["method"],
                        "monthly_wall_quality": monthly_walls["quality"],
                        "iv_skew": _round_or_none(iv_skew, 6),
                        "short_gamma_m": _round_or_none(short_gamma_total, 6),
                        "long_gamma_m": _round_or_none(long_gamma_total, 6),
                        "gamma_0dte_m": _round_or_none(gamma["0DTE"]["net_gamma_m"], 6),
                        "gamma_1_7d_m": _round_or_none(gamma["1-7D"]["net_gamma_m"], 6),
                        "gamma_8_30d_m": _round_or_none(gamma["8-30D"]["net_gamma_m"], 6),
                        "gamma_31_60d_m": _round_or_none(gamma["31-60D"]["net_gamma_m"], 6),
                        "gamma_all_m": _round_or_none(gamma["ALL"]["net_gamma_m"], 6),
                        "gamma_flip_0dte": _round_or_none(gamma["0DTE"]["primary_flip"], 4),
                        "gamma_flip_1_7d": _round_or_none(gamma["1-7D"]["primary_flip"], 4),
                        "gamma_flip_8_30d": _round_or_none(gamma["8-30D"]["primary_flip"], 4),
                        "gamma_flip_31_60d": _round_or_none(gamma["31-60D"]["primary_flip"], 4),
                        "gamma_flip_all": _round_or_none(gamma["ALL"]["primary_flip"], 4),
                        "gamma_previous_flip": _round_or_none(previous_flip, 4),
                        "gamma_flip_change_pct": _round_or_none(gamma_flip_change_pct),
                        "gamma_roll_changed": gamma_roll_changed,
                        "gamma_flip_quality": gamma_flip_quality,
                        "gamma_quality": gamma["quality"],
                        "gamma_requested_contract_count": requested_counts["ALL"],
                        "gamma_qualified_contract_count": qualified_counts["ALL"],
                        "gamma_oi_valid_contract_count": oi_valid_counts["ALL"],
                        "gamma_valid_contract_count": gamma["ALL"]["contract_count"],
                        "gamma_zeroes": gamma_zeroes,
                        "gamma_expirations": gamma_expirations,
                        "gamma_curve_version": gamma["curve_version"],
                        "gamma_grid_width_pct": gamma["grid_width_pct"],
                        "gamma_grid_points": gamma["grid_points"],
                        "gamma_max_dte": MAX_GAMMA_DTE,
                        "gamma_sign_model": gamma["sign_model"],
                        "oi_pcr": _round_or_none(oi_pcr, 6),
                        "oi_source_date": price_snapshot.get("previous_close_date"),
                    }

                    if darkpool_data and sym in darkpool_data:
                        payload["dpsv_pct"] = darkpool_data[sym]['DPSV_%']
                        # DPSV 用 FINRA 文件真实日期，不再冒充当日数据
                        payload["dpsv_source_date"] = finra_date

                    attach_metadata(payload, source_date=today_str)
                    if price_snapshot.get("quote_as_of"):
                        payload["as_of_time"] = price_snapshot["quote_as_of"]
                    main_result = safe_upsert(
                        supabase, 'stock_options_pre_market', payload,
                        conflict_cols='date,ticker')
                    if main_result is not None:
                        rows_written += 1
                    else:
                        quality_issues.append(f"db_main:{sym}")
                    bucket_rows = []
                    for bucket in GAMMA_BUCKETS:
                        metrics = gamma[bucket]
                        bucket_row = {
                            "date": today_str, "ticker": sym, "bucket": bucket,
                            "spot_reference": _round_or_none(curr_price, 4),
                            "net_gamma_m": _round_or_none(metrics["net_gamma_m"], 6),
                            "primary_flip": _round_or_none(metrics["primary_flip"], 4),
                            "zero_points": metrics["zero_points"],
                            "expirations": metrics["expirations"],
                            "contract_count": metrics["contract_count"],
                            "expiration_count": metrics["expiration_count"],
                            "requested_contract_count": metrics["requested_contract_count"],
                            "qualified_contract_count": metrics["qualified_contract_count"],
                            "oi_valid_contract_count": metrics["oi_valid_contract_count"],
                            "coverage_pct": _round_or_none(metrics["coverage_pct"], 4),
                            "call_count": metrics["call_count"],
                            "put_count": metrics["put_count"],
                            "strike_count": metrics["strike_count"],
                            "quality": metrics["quality"],
                            "raw_net_gamma_m": _round_or_none(metrics["raw_net_gamma_m"], 6),
                            "raw_primary_flip": _round_or_none(metrics["raw_primary_flip"], 4),
                            "curve_version": gamma["curve_version"],
                            "grid_width_pct": gamma["grid_width_pct"],
                            "grid_points": gamma["grid_points"],
                            "sign_model": gamma["sign_model"],
                        }
                        attach_metadata(
                            bucket_row,
                            source_date=price_snapshot.get("previous_close_date") or today_str)
                        bucket_row["as_of_time"] = (
                            price_snapshot.get("quote_as_of")
                            or datetime.now(timezone.utc).isoformat())
                        bucket_rows.append(bucket_row)
                    bucket_result = safe_upsert(
                        supabase, 'option_gamma_buckets', bucket_rows,
                        conflict_cols='date,ticker,bucket')
                    if bucket_result is None:
                        quality_issues.append(f"db_gamma_bucket:{sym}")
                    if main_result is not None and bucket_result is not None:
                        logger.info(f"✅ [{sym}] 进阶期权阵地数据推送成功！")
                except Exception as e:
                    logger.warning(f"⚠️ {sym} 入库异常: {e}")
                    quality_issues.append(f"db_main:{sym}")

            else:
                quality_issues.append(f"option_rows:{sym}")
                report += f"{sym} | 期权持仓或报价不可用\n"

        # 数据质量审计：记录本次抓取覆盖率与 FINRA 滞后
        try:
            missing_syms = [
                s for s in SYMBOLS if darkpool_data is None or s not in darkpool_data]
            quality_issues.extend(f"dpsv:{s}" for s in missing_syms)
            log_data_quality(
                supabase, job_name='daily_pre_market', table_name='stock_options_pre_market',
                status=(
                    'ok' if rows_written == len(SYMBOLS) and not quality_issues
                    else ('partial' if rows_written > 0 else 'failed')),
                rows_written=rows_written,
                missing_fields=sorted(set(quality_issues)) or None,
                lag_days=finra_lag or 0,
                notes=(
                    f"FINRA源日期: {finra_date or '不可用'}; "
                    f"Gamma期限≤{MAX_GAMMA_DTE}日，符号模型=Call正/Put负代理"),
            )
        except Exception as e:
            logger.warning(f"⚠️ 数据质量记录失败: {e}")

        report += (
            "\n【运行质量】\n"
            + format_premarket_quality_summary(
                rows_written, len(SYMBOLS), quality_issues)
            + "\n完整期权链、Gamma零点、样本覆盖、最大OI及对冲代理已保留在数据库。"
        )

        if send_email(f"美股盘前监测摘要 | {today_str}", report):
            logger.info("\n✅ 轨道二运行完毕并发送成功！")
        else:
            logger.warning("\n⚠️ 轨道二数据已处理，但邮件发送失败。")

    except Exception as e: logger.error(f"❌ 运行失败: {e}")
    finally: ib.disconnect()

if __name__ == "__main__": get_report()
