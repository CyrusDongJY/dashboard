import sys
import os
import math
import logging
import smtplib
import time
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.header import Header
from contextlib import contextmanager
import urllib.parse

# Shared contracts and secrets live outside TradingRadar on the cloud host.  The
# path must be installed before importing either module; cron does not set
# PYTHONPATH for this job.
CONFIG_DIR = os.path.expanduser('~/market_dashboard')
if CONFIG_DIR not in sys.path:
    sys.path.insert(0, CONFIG_DIR)

import pytz
import pandas as pd
import numpy as np
import requests
from ib_insync import *
from supabase import create_client, Client
import pandas_market_calendars as mcal  

# ================= 🔐 安全挂载全局金库 =================
try:
    import market_config as cfg
except ImportError:
    print("❌ 致命错误：找不到 market_config.py 密钥文件！程序终止。")
    sys.exit(1)

# ================= 全局常量与环境配置 =================
NY_TZ = pytz.timezone('America/New_York')
DB_MAX_RETRIES = 3
IB_CONNECT_RETRIES = 3
AD_MAX_QUOTE_AGE_SECONDS = 180
AD_REPEAT_THRESHOLD = 3
IB_INFORMATION_CODES = {2104, 2106, 2107, 2108, 2158}
IBKR_BREADTH_CONTRACTS = {
    'TICK': ('TICK-NYSE', 'NYSE'),
    'TRIN': ('TRIN-NYSE', 'NYSE'),
    'AD': ('AD-NYSE', 'NYSE'),
}
UD_UNSUPPORTED_STATUS = 'UNSUPPORTED_BY_IBKR_CONTRACT'
UD_UNSUPPORTED_SOURCE = 'UNAVAILABLE:IBKR_NO_UVOL_DVOL_INDEX_CONTRACT'


def classify_breadth_status(value, prior_values, quote_age_seconds=None):
    """Fail closed when an AD quote is missing, old, or repeated three times."""
    try:
        current = float(value)
    except (TypeError, ValueError):
        return 'MISSING_STREAM', 0
    if not math.isfinite(current):
        return 'MISSING_STREAM', 0
    repeat_count = 1
    for prior in prior_values or []:
        try:
            if float(prior) != current:
                break
        except (TypeError, ValueError):
            break
        repeat_count += 1
    try:
        quote_is_old = (
            quote_age_seconds is not None
            and float(quote_age_seconds) > AD_MAX_QUOTE_AGE_SECONDS)
    except (TypeError, ValueError):
        quote_is_old = False
    status = (
        'STALE_VALUE'
        if repeat_count >= AD_REPEAT_THRESHOLD or quote_is_old else 'OK')
    return status, repeat_count

# 日志防干扰设置
logging.getLogger('ib_insync').setLevel(logging.CRITICAL)

# 挂载 Supabase
supabase: Client = create_client(cfg.SUPABASE_URL, cfg.SUPABASE_KEY)

# ================= 🛡️ 架构级资源管理与重试机制 =================

@contextmanager
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
            except: pass

def safe_db_insert(table_name, data):
    """【容灾模块】原生指数退避重试，绝不阻断主流程"""
    for attempt in range(DB_MAX_RETRIES):
        try:
            response = supabase.table(table_name).insert(data).execute()
            return True
        except Exception as e:
            if attempt < DB_MAX_RETRIES - 1:
                sleep_time = 2 ** attempt 
                print(f"⚠️ 数据库写入异常，{sleep_time}秒后重试...")
                import time
                time.sleep(sleep_time) 
            else:
                print(f"❌ 数据库写入最终失败: {e}")
                return False

# ================= 核心哨兵系统 =================
class GlobalSentinel:
    def __init__(self):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        self.log_file = os.path.join(script_dir, 'sniper.log')
        self.ib = IB()
        self.opt_ctx = {}
        self.spot_ctx = {}
        self.context_quality = "MISSING"
        self.ib_errors = []
        self.ib.errorEvent += self._capture_ib_error

    def _capture_ib_error(self, req_id, error_code, error_string,
                          contract=None):
        """Persist actionable IB errors instead of suppressing their cause."""
        if error_code in IB_INFORMATION_CODES:
            return
        error = {
            'req_id': req_id,
            'code': error_code,
            'symbol': getattr(contract, 'symbol', None) if contract else None,
            'message': error_string,
        }
        self.ib_errors.append(error)
        self.ib_errors = self.ib_errors[-50:]
        self.log(
            f"⚠️ IB错误 code={error_code} "
            f"symbol={error['symbol'] or 'NA'}: {error_string}")

    def is_trading_day(self):
        try:
            today_str = datetime.now(NY_TZ).strftime('%Y-%m-%d')
            nyse = mcal.get_calendar('NYSE')
            schedule = nyse.schedule(start_date=today_str, end_date=today_str)
            return not schedule.empty
        except:
            return True

    def log(self, msg):
        full_msg = f"[{datetime.now(NY_TZ).strftime('%H:%M:%S')}] {msg}"
        print(full_msg)

    def send_email(self, subject, body):
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['Subject'], msg['From'], msg['To'] = Header(subject, 'utf-8'), cfg.SENDER_EMAIL, cfg.RECEIVER_EMAIL
    
        try:
            server = smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=10)
            server.login(cfg.SENDER_EMAIL, cfg.APP_PASSWORD)
            server.sendmail(cfg.SENDER_EMAIL, [cfg.RECEIVER_EMAIL], msg.as_string())
            server.quit()
            print("✅ 战报邮件已通过 Gmail 火速送达 QQ 邮箱！")
            return True
        except Exception as e:
            print(f"❌ 邮件异常: {e}")
            return False

    def send_alert(self, subject, body):
        self.send_email(subject, body)
        try:
            url = f"https://api.day.app/{cfg.BARK_KEY}/{urllib.parse.quote(subject)}/{urllib.parse.quote(body)}?group=Sentinel&sound=alarm"
            requests.get(url, timeout=5)
        except: pass

    def send_summary(self):
        # ✅ 新增：在盘后汇总入口处设立休市日拦截岗哨
        if not self.is_trading_day():
            print("🛑 拦截：今天是美股休市日，汇总邮件任务取消。")
            return

        try:
            print("正在发送盘后汇总邮件...")
            if not os.path.exists(self.log_file):
                print(f"日志文件不存在: {self.log_file}")
                return
                
            with open(self.log_file, 'r', encoding='utf-8') as f: 
                lines = f.readlines()
                
            if not lines: 
                print("日志为空，无内容需发送。")
                return
            
            date_str = datetime.now(NY_TZ).strftime('%Y-%m-%d')
            
            # 🛡️ 第一重防线：降噪黑名单
            noise_keywords = [
                "正在装载 Supabase", 
                "分析完成。警报状态: False", 
                "盘中数据切片已成功推入"
                # ✅ 修复：移除了 "运行崩溃"，杜绝掩耳盗铃，确保崩溃日志能进入次日复盘
            ]
            
            filtered_content = ""
            is_today = True  # 默认放行，直到遇到明确的“旧日期”
            
            for line in lines:
                # 1. 垃圾降噪：包含黑名单词汇直接丢弃
                if any(keyword in line for keyword in noise_keywords):
                    continue
                    
                # 2. 🛡️ 第二重防线：日期隔离墙
                # 检查这行是否带有完整的日期时间戳，例如 [2026-05-12 10:30:00]
                if line.startswith('['):
                    if len(line) >= 11 and line[1:3] == '20' and line[5] == '-':
                        if date_str in line:
                            is_today = True   # 是今天的警报，开门放行
                        else:
                            is_today = False  # 发现前几天的旧账，立刻关门拦截
                
                # 3. 只收集属于今天的实质性警报
                if is_today:
                    filtered_content += line
                    
            # 剔除连续的空行，让排版更紧凑
            filtered_content = "\n".join([ll for ll in filtered_content.split('\n') if ll.strip()])
                    
            if not filtered_content.strip():
                print("今日无实质性 A+ 级刺客警报，正在清理后台历史废弃日志...")
                open(self.log_file, 'w').close()
                return
            
            if self.send_email(f"【盘后清点】美股日内刺客雷达 ({date_str})", filtered_content):
                open(self.log_file, 'w').close() 
                print("✅ 汇总邮件发送并归档完毕。")
            else:
                print("⚠️ 邮件发送异常，保留本地日志文件以便排查。")
                
        except Exception as e: 
            print(f"❌ 执行汇总任务异常: {e}")

    def load_sentinel_context(self):
        self.log("正在装载 Supabase 宏观与期权全局视野...")
        try:
            today_str = datetime.now(NY_TZ).strftime('%Y-%m-%d')
            # 期权上下文必须显式核对交易日。可以展示旧行供诊断，但旧行不得参与触发。
            res_o = supabase.table('stock_options_pre_market').select('*').eq('ticker', 'SPY').order('date', desc=True).limit(1).execute()
            if not res_o.data:
                res_o = supabase.table('stock_options_daily').select('*').eq('ticker', 'SPY').order('date', desc=True).limit(1).execute()
            if res_o.data:
                self.opt_ctx = res_o.data[0]
                if self.opt_ctx.get('ticker') != 'SPY':
                    self.context_quality = "TICKER_MISMATCH"
                elif str(self.opt_ctx.get('date')) != today_str:
                    self.context_quality = "STALE"
                elif self.opt_ctx.get('gamma_curve_version') != 'gamma_curve_v2':
                    self.context_quality = "LEGACY_METHOD"
                else:
                    self.context_quality = "OK"

            # POC 属于盘后现货域，不能再从盘前期权表读取。盘中使用最近一次已完成盘后截面。
            res_s = (supabase.table('stock_spot_post_close').select('*')
                     .eq('ticker', 'SPY').lte('date', today_str)
                     .order('date', desc=True).limit(1).execute())
            if res_s.data:
                self.spot_ctx = res_s.data[0]
        except Exception as e:
            self.log(f"⚠️ 装载全局视野失败: {e}")

    def connect_with_retry(self):
        """Reconnect the IB session before giving up on a scheduled sample."""
        for attempt in range(1, IB_CONNECT_RETRIES + 1):
            try:
                if self.ib.isConnected():
                    return True
                self.ib.connect(
                    '127.0.0.1', 4001, clientId=888,
                    readonly=True, timeout=10)
                self.ib.reqMarketDataType(1)
                return True
            except Exception as exc:
                self.log(
                    f"⚠️ IB连接失败 {attempt}/{IB_CONNECT_RETRIES}: "
                    f"{type(exc).__name__}: {exc}")
                try:
                    self.ib.disconnect()
                except Exception:
                    pass
                if attempt < IB_CONNECT_RETRIES:
                    time.sleep(2 ** (attempt - 1))
        return False

    def recent_add_values(self, today_str, limit=2):
        try:
            response = (supabase.table('intraday_logs')
                        .select('add_raw,add_val,record_time')
                        .gte('record_time', f'{today_str} 00:00:00')
                        .lte('record_time', f'{today_str} 23:59:59')
                        .order('record_time', desc=True).limit(limit).execute())
            return [
                row.get('add_raw')
                if row.get('add_raw') is not None else row.get('add_val')
                for row in (response.data or [])
            ]
        except Exception as exc:
            self.log(f"⚠️ AD重复值历史读取失败: {type(exc).__name__}: {exc}")
            return []

    def post_close_breadth_backup(self, today_str):
        """Use the independently collected close breadth only as a labeled proxy."""
        try:
            response = (supabase.table('market_history')
                        .select('date,pct_adv,breadth_sample_size,trin_as_of')
                        .eq('date', today_str).order('date', desc=True)
                        .limit(1).execute())
            if not response.data:
                return None
            row = response.data[0]
            value = float(row.get('pct_adv'))
            if not math.isfinite(value):
                return None
            return {
                'pct_adv': value,
                'sample_size': int(row.get('breadth_sample_size') or 0),
                'as_of': row.get('trin_as_of'),
                'source': 'TradingViewScanner:top500_nyse_nasdaq',
                'quality': 'EOD_PROXY_NOT_NYSE_AD',
            }
        except Exception as exc:
            self.log(f"⚠️ 备用广度读取失败: {type(exc).__name__}: {exc}")
            return None

    @staticmethod
    def ticker_timestamp(ticker):
        quote_time = getattr(ticker, 'time', None)
        if not isinstance(quote_time, datetime):
            return None, None
        if quote_time.tzinfo is None:
            quote_time = quote_time.replace(tzinfo=timezone.utc)
        age = max(
            0.0,
            (datetime.now(timezone.utc)
             - quote_time.astimezone(timezone.utc)).total_seconds())
        return quote_time.isoformat(), age

    def backfill_missing_checkpoints(self, bars, today_str):
        """Backfill missed 15-minute SPY checkpoints from IBKR 5-minute bars."""
        if not bars:
            return 0
        try:
            existing = (supabase.table('intraday_logs').select('record_time')
                        .gte('record_time', f'{today_str} 00:00:00')
                        .lte('record_time', f'{today_str} 23:59:59')
                        .execute()).data or []
            existing_keys = {
                pd.Timestamp(row['record_time']).strftime('%H:%M')
                for row in existing if row.get('record_time')
            }
            frame = pd.DataFrame([{
                'date': getattr(bar, 'date', None),
                'close': getattr(bar, 'close', None),
                'volume': getattr(bar, 'volume', None),
            } for bar in bars])
            frame['date'] = pd.to_datetime(frame['date'], errors='coerce')
            if frame['date'].dt.tz is None:
                frame['date'] = frame['date'].dt.tz_localize(NY_TZ)
            else:
                frame['date'] = frame['date'].dt.tz_convert(NY_TZ)
            frame['close'] = pd.to_numeric(frame['close'], errors='coerce')
            frame['volume'] = pd.to_numeric(frame['volume'], errors='coerce')
            frame = frame.dropna().sort_values('date')
            frame = frame[(frame['close'] > 0) & (frame['volume'] > 0)]
            if frame.empty:
                return 0

            now = datetime.now(NY_TZ)
            end = now.replace(
                minute=(now.minute // 15) * 15, second=0, microsecond=0)
            checkpoint = NY_TZ.localize(datetime.strptime(
                f'{today_str} 09:45', '%Y-%m-%d %H:%M'))
            inserted = 0
            while checkpoint <= end:
                key = checkpoint.strftime('%H:%M')
                if key not in existing_keys:
                    observed = frame[frame['date'] <= checkpoint]
                    if not observed.empty:
                        vwap = float(
                            (observed['close'] * observed['volume']).sum()
                            / observed['volume'].sum())
                        lagged = observed[observed['date'] <= checkpoint - timedelta(minutes=30)]
                        lagged_vwap = (
                            float((lagged['close'] * lagged['volume']).sum()
                                  / lagged['volume'].sum())
                            if not lagged.empty else vwap)
                        payload = {
                            'record_time': checkpoint.strftime('%Y-%m-%d %H:%M:%S'),
                            'spy_px': float(observed.iloc[-1]['close']),
                            'vwap_now': vwap,
                            'vwap_30m': lagged_vwap,
                            'add_val': None,
                            'add_raw': None,
                            'add_status': 'MISSING_HISTORICAL_BREADTH',
                            'vol_ratio': None,
                            'vol_ratio_status': UD_UNSUPPORTED_STATUS,
                            'context_quality': 'HISTORICAL_BACKFILL',
                            'context_metadata': {
                                'backfill_source': 'IBKR:SPY:5min:TRADES',
                                'backfill_quality': 'HISTORICAL_BACKFILL',
                                'breadth_source': None,
                            },
                        }
                        if safe_db_insert('intraday_logs', payload):
                            inserted += 1
                checkpoint += timedelta(minutes=15)
            if inserted:
                self.log(f"✅ 已用IBKR 5分钟历史数据回补 {inserted} 个盘中检查点。")
            return inserted
        except Exception as exc:
            self.log(f"⚠️ 盘中历史回补失败: {type(exc).__name__}: {exc}")
            return 0

    def get_robust_index_val(self, contract, ticker, *, allow_zero=False,
                             positive_only=False, allow_bid_ask=False,
                             allow_history=True):
        """Read a quote without turning a legitimate zero into missing data."""
        symbol = getattr(contract, 'symbol', 'UNKNOWN')
        if ticker is None:
            self.log(f"⚠️ {symbol} 未建立有效行情订阅，返回缺失")
            return None

        def valid_number(value, *, zero_allowed=None):
            try:
                number = float(value)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(number):
                return None
            if positive_only:
                return number if number > 0 else None
            if zero_allowed is None:
                zero_allowed = allow_zero
            if not zero_allowed and number == 0:
                return None
            return number

        attributes = ['last', 'close']
        if allow_bid_ask:
            attributes.extend(['bid', 'ask'])
        for attr in attributes:
            # AD-NYSE/TICK-NYSE can legitimately print zero as a live last
            # tick.  Their prior close is structurally zero, so close=0 is not
            # evidence that the current stream is healthy.
            zero_allowed = allow_zero and attr == 'last'
            value = valid_number(
                getattr(ticker, attr, None), zero_allowed=zero_allowed)
            if value is not None:
                return value

        if callable(getattr(ticker, 'marketPrice', None)):
            try:
                value = valid_number(ticker.marketPrice())
                if value is not None:
                    return value
            except Exception:
                pass

        if allow_history:
            try:
                bars = self.ib.reqHistoricalData(
                    contract,
                    endDateTime='',
                    durationStr='900 S',
                    barSizeSetting='1 min',
                    whatToShow='TRADES',
                    useRTH=False,
                )
                if bars:
                    value = valid_number(bars[-1].close)
                    if value is not None:
                        return value
            except Exception as exc:
                self.log(
                    f"⚠️ {symbol} 历史行情兜底失败: "
                    f"{type(exc).__name__}: {exc}")

        self.log(f"⚠️ {symbol} 行情不可用，返回缺失")
        return None

    def run_scan(self):
        if not self.is_trading_day():
            self.log("🛑 拦截：今天是美股休市日，哨兵休眠。")
            return

        self.ib_errors = []
        self.load_sentinel_context()
        
        def optional_float(val):
            try:
                number = float(val)
                return number if math.isfinite(number) else None
            except (TypeError, ValueError):
                return None

        def display_number(value, digits=2, prefix=''):
            return f"{prefix}{value:.{digits}f}" if value is not None else "NA"

        def display_signed(value, digits=0):
            return f"{value:+.{digits}f}" if value is not None else "NA"

        zgl = optional_float(
            self.opt_ctx.get('gamma_flip_all', self.opt_ctx.get('zgl_price')))
        gamma_flip_alert_enabled = bool(
            getattr(cfg, 'ENABLE_GAMMA_FLIP_ALERT', False)
            and self.context_quality == "OK"
            and self.opt_ctx.get('gamma_flip_quality') == 'OK'
            and zgl is not None)
        poc = optional_float(self.spot_ctx.get('poc_price'))
        pcr = optional_float(self.opt_ctx.get('oi_pcr'))
        exp_move_pct = optional_float(self.opt_ctx.get('expected_move_pct'))
        exp_move = exp_move_pct / 100.0 if exp_move_pct is not None else None
        prev_close = optional_float(
            self.opt_ctx.get('previous_close', self.opt_ctx.get('current_price')))

        gamma_zeroes = self.opt_ctx.get('gamma_zeroes') or {}
        all_zeroes = gamma_zeroes.get('ALL', []) if isinstance(gamma_zeroes, dict) else []
        gamma_expirations = self.opt_ctx.get('gamma_expirations') or {}
        all_expirations = (
            gamma_expirations.get('ALL', [])
            if isinstance(gamma_expirations, dict) else []
        )
        gamma_max_dte = int(optional_float(
            self.opt_ctx.get('gamma_max_dte')) or 60)
        context_metadata = {
            "ticker": self.opt_ctx.get('ticker'),
            "pre_market_date": self.opt_ctx.get('date'),
            "pre_market_as_of": self.opt_ctx.get('quote_as_of') or self.opt_ctx.get('as_of_time'),
            "pre_market_context_quality": self.context_quality,
            "spot_reference": self.opt_ctx.get('premarket_reference_price', self.opt_ctx.get('current_price')),
            "spot_reference_source": self.opt_ctx.get('premarket_price_source'),
            "gamma_scope": f"sampled_expirations_le_{gamma_max_dte}d",
            "gamma_max_dte": gamma_max_dte,
            "gamma_curve_version": self.opt_ctx.get('gamma_curve_version') or 'LEGACY_UNKNOWN',
            "gamma_sign_model": self.opt_ctx.get('gamma_sign_model'),
            "gamma_flip_quality": self.opt_ctx.get('gamma_flip_quality'),
            "gamma_expirations": all_expirations,
            "gamma_zero_count": len(all_zeroes),
            "gamma_zeroes": all_zeroes,
            "pcr_source_date": self.opt_ctx.get('oi_source_date'),
            "poc_date": self.spot_ctx.get('date'),
            "poc_as_of": self.spot_ctx.get('as_of_time'),
            "poc_method": self.spot_ctx.get('poc_method'),
            "poc_window": self.spot_ctx.get('poc_window'),
            "poc_source_date": self.spot_ctx.get('poc_source_date'),
            "trin_closing_auction_inclusion": "UNVERIFIED",
        }

        try:
            if not self.connect_with_retry():
                self.log("❌ IB连续重连失败，本次扫描停止且不写入伪造数据。")
                return

            contracts = {
                'SPY': Stock('SPY', 'SMART', 'USD'),
            }
            contracts.update({
                key: Index(symbol, exchange)
                for key, (symbol, exchange) in
                IBKR_BREADTH_CONTRACTS.items()
            })
            self.ib.qualifyContracts(*contracts.values())

            contract_status = {}
            qualified_contracts = {}
            for key, contract in contracts.items():
                con_id = int(getattr(contract, 'conId', 0) or 0)
                qualified = con_id > 0
                contract_status[key] = {
                    'requested_symbol': getattr(contract, 'symbol', None),
                    'con_id': con_id or None,
                    'qualified': qualified,
                }
                if qualified:
                    qualified_contracts[key] = contract
                else:
                    self.log(
                        f"⚠️ {key}合约资格失败："
                        f"{getattr(contract, 'symbol', 'UNKNOWN')} conId=0")

            if 'SPY' not in qualified_contracts:
                self.log("⚠️ SPY 合约资格失败，跳过本次扫描。")
                return

            with market_data_subscription(
                    self.ib, qualified_contracts.values(), 8) as tickers:
                t_dict = {
                    getattr(t.contract, 'conId', None): t for t in tickers
                    if getattr(t, 'contract', None) is not None
                }

                def ticker_for(key):
                    contract = qualified_contracts.get(key)
                    return t_dict.get(getattr(contract, 'conId', None))

                spy_px = self.get_robust_index_val(
                    qualified_contracts['SPY'], ticker_for('SPY'),
                    positive_only=True, allow_bid_ask=True)
                if spy_px is None:
                    self.log("⚠️ SPY 现价获取失败，跳过本次扫描。")
                    return

                ad_contract = qualified_contracts.get('AD')
                ad_ticker = ticker_for('AD')
                add_val = (
                    self.get_robust_index_val(
                        ad_contract, ad_ticker, allow_zero=True,
                        allow_history=False)
                    if ad_contract is not None else None
                )
                add_as_of, add_age_seconds = self.ticker_timestamp(ad_ticker)
                trin_contract = qualified_contracts.get('TRIN')
                trin_raw = (
                    self.get_robust_index_val(
                        trin_contract, ticker_for('TRIN'), positive_only=True)
                    if trin_contract is not None else None
                )
                trin_val = trin_raw if trin_raw is not None and trin_raw > 0 else None
                tick_contract = qualified_contracts.get('TICK')
                tick_now = (
                    self.get_robust_index_val(
                        tick_contract, ticker_for('TICK'), allow_zero=True)
                    if tick_contract is not None else None
                )

            # IBKR没有UVOL/DVOL指数合约。保持NULL，禁止用1.00伪造中性。
            uvol_val = None
            dvol_val = None
            vol_ratio = None
            vol_ratio_status = UD_UNSUPPORTED_STATUS
            add_raw = add_val
            prior_add_values = self.recent_add_values(
                datetime.now(NY_TZ).strftime('%Y-%m-%d'))
            add_status, repeat_count = classify_breadth_status(
                add_raw, prior_add_values, add_age_seconds)
            if add_status != 'OK':
                add_val = None
            today_str = datetime.now(NY_TZ).strftime('%Y-%m-%d')
            breadth_backup = (
                self.post_close_breadth_backup(today_str)
                if add_status != 'OK' else None)
            context_metadata.update({
                "add_raw": add_raw,
                "add_status": add_status,
                "add_source": "IBKR:AD-NYSE",
                "add_as_of": add_as_of,
                "add_age_seconds": add_age_seconds,
                "add_repeat_count": repeat_count,
                "breadth_backup": breadth_backup,
                "uvol_raw": None,
                "dvol_raw": None,
                "vol_ratio_status": vol_ratio_status,
                "ud_source": UD_UNSUPPORTED_SOURCE,
                "ud_contract_validation": "IBKR_ERROR_200_CONFIRMED_2026-07-24",
                "ib_contracts": contract_status,
            })
            
            # 计算 15 分钟累积 TICK
            ctick_15m_avg = tick_now
            if tick_contract is not None:
                try:
                    tick_bars = self.ib.reqHistoricalData(
                        tick_contract, endDateTime='', durationStr='900 S',
                        barSizeSetting='1 min', whatToShow='TRADES',
                        useRTH=False)
                    if tick_bars:
                        ctick_15m_avg = (
                            sum([b.close for b in tick_bars]) /
                            len(tick_bars))
                except Exception as exc:
                    self.log(
                        f"⚠️ TICK-NYSE 15分钟历史均值失败: "
                        f"{type(exc).__name__}: {exc}")

            # 计算 VWAP
            vwap_now, vwap_30m = spy_px, spy_px
            intra_bars = []
            try:
                intra_bars = self.ib.reqHistoricalData(contracts['SPY'], endDateTime='', durationStr='1 D', barSizeSetting='5 mins', whatToShow='TRADES', useRTH=True)
                if intra_bars:
                    df = pd.DataFrame([{'v': b.volume, 'p': b.close} for b in intra_bars])
                    if df['v'].sum() > 0: vwap_now = (df['p'] * df['v']).sum() / df['v'].sum()
                    df_30 = df.iloc[:-6] if len(df) >= 6 else df.iloc[:1]
                    if not df_30.empty and df_30['v'].sum() > 0: vwap_30m = (df_30['p'] * df_30['v']).sum() / df_30['v'].sum()
            except: pass

            sma20 = spy_px
            try:
                spy_bars = self.ib.reqHistoricalData(contracts['SPY'], endDateTime='', durationStr='1 M', barSizeSetting='1 day', whatToShow='TRADES', useRTH=True)
                if spy_bars and len(spy_bars)>=20: sma20 = sum([b.close for b in spy_bars[-20:]])/20
            except: pass

            fire = False
            title = ""
            
            lower_bound = (
                prev_close * (1 - exp_move)
                if prev_close is not None and prev_close > 0 and exp_move is not None
                and self.context_quality == "OK" else None
            )

            # 判断警报
            if (lower_bound is not None and trin_val is not None
                    and spy_px <= lower_bound * 1.002 and trin_val >= 1.5):
                if (vol_ratio is not None and vol_ratio >= 2.0
                        and ctick_15m_avg is not None and ctick_15m_avg > 0):
                    fire = True; title = "🥇【深海核爆】极值恐慌底反转！散户止损，机构扫货，绝佳做多点！"
            elif (
                gamma_flip_alert_enabled and zgl is not None and zgl > 0
                and spy_px < zgl and spy_px < vwap_now
            ):
                if (vol_ratio is not None and vol_ratio <= 0.25
                        and ctick_15m_avg is not None
                        and ctick_15m_avg < -300):
                    fire = True
                    title = "🥈【结构确认】Gamma Flip观察位失守且广度、成交同步恶化"
            elif vol_ratio is not None and vol_ratio >= 4.0 and spy_px > vwap_now:
                fire = True; title = "🥉【趋势碾压】绝对单边做多日！严禁做空，顺势做多后持仓！"
            elif vol_ratio is not None and vol_ratio <= 0.20 and spy_px < vwap_now:
                fire = True; title = "🥉【趋势碾压】绝对单边暴跌日！严禁抄底，顺势做空后持仓！"

            # 格式化绝对纽约时间
            ny_timestamp_str = datetime.now(NY_TZ).strftime('%Y-%m-%d %H:%M:%S')

            r = f"[{ny_timestamp_str}] \n"
            if fire:
                r += f"🚨 触发警报: {title}\n"
            r += f"=========================\n"
            r += f"【全局战略坐标】\n"
            zgl_mode = "已校准硬触发" if gamma_flip_alert_enabled else "仅观察"
            r += (
                f"SPY ≤{gamma_max_dte}日采样主Gamma Flip: {display_number(zgl, prefix='$')} "
                f"({zgl_mode}, Flip状态={context_metadata['gamma_flip_quality'] or 'MISSING'}, "
                f"上下文={self.context_quality})\n"
            )
            r += (
                f"Gamma血缘: 截面={context_metadata['pre_market_date'] or 'NA'} "
                f"| 现价参考={display_number(optional_float(context_metadata['spot_reference']), prefix='$')} "
                f"| 模型={context_metadata['gamma_sign_model'] or 'NA'} "
                f"| 到期数={len(all_expirations)} | 零点数={len(all_zeroes)}\n"
            )
            r += (
                f"最近完整盘后6M日线收盘价分箱POC代理: {display_number(poc, prefix='$')} "
                f"(ticker=SPY, 行日期={context_metadata['poc_date'] or 'NA'}, "
                f"源日期={context_metadata['poc_source_date'] or 'NA'}, "
                f"方法={context_metadata['poc_method'] or 'NA'})\n"
            )
            r += (
                f"SPY Put/Call持仓结构比: {display_number(pcr)} "
                f"(OI日期={context_metadata['pcr_source_date'] or 'NA'})\n"
            )
            r += f"【日内高频刺客】\n"
            r += f"SPY 现价: ${spy_px:.2f} (VWAP: ${vwap_now:.2f})\n"
            r += (
                f"NYSE净上涨−下跌家数 (AD-NYSE): "
                f"{display_signed(add_val)} ({add_status}; 原始={display_signed(add_raw)}; "
                f"更新时间={add_as_of or 'NA'}; 连续相同={repeat_count})\n"
            )
            if breadth_backup:
                r += (
                    f"备用收盘广度: 上涨家数 {breadth_backup['pct_adv']:.1f}% "
                    f"({breadth_backup['quality']}, 样本={breadth_backup['sample_size']}, "
                    f"源={breadth_backup['source']})\n"
                )
            r += (
                f"NYSE上涨/下跌成交量比 (U/D): NA "
                f"({vol_ratio_status}; IBKR无UVOL/DVOL指数合约)\n"
            )
            r += f"NYSE TRIN: {display_number(trin_val)} (IBKR TRIN-NYSE, 截面={ny_timestamp_str}, 收盘竞价=UNVERIFIED)\n"
            r += f"15m累积暗流 (CTICK): {display_signed(ctick_15m_avg)}\n"

            print(r)

            self.log(f"分析完成。警报状态: {fire}")
            
            if fire:
                self.log("💥 满足A+级共振，已发射全球预警！")
                self.send_alert(title, r)

            # ✅ 写入纯粹的 NY 字符串时间，屏蔽 Supabase 时区错乱
            context_metadata["ib_errors"] = self.ib_errors[-20:]
            db_payload = {
                "record_time": ny_timestamp_str,
                "spy_px": spy_px, "sma20": sma20, "vwap_now": vwap_now, "vwap_30m": vwap_30m,
                "add_val": add_val, "uvol": uvol_val, "dvol": dvol_val, "vol_ratio": vol_ratio,
                "add_raw": add_raw,
                "add_status": add_status,
                "add_as_of": add_as_of,
                "add_age_seconds": add_age_seconds,
                "add_repeat_count": repeat_count,
                "breadth_pct_adv": (
                    breadth_backup.get('pct_adv') if breadth_backup else None),
                "breadth_sample_size": (
                    breadth_backup.get('sample_size') if breadth_backup else None),
                "breadth_source": (
                    breadth_backup.get('source') if breadth_backup else None),
                "breadth_as_of": (
                    breadth_backup.get('as_of') if breadth_backup else None),
                "trin": trin_val, "tick_now": tick_now, "ctick_15m": ctick_15m_avg,
                "alert_triggered": title if fire else None,
                "vol_ratio_status": vol_ratio_status,
                "trin_scope": "NYSE",
                "trin_source": "IBKR:TRIN-NYSE",
                "context_quality": self.context_quality,
                "context_metadata": context_metadata,
            }
            
            if safe_db_insert('intraday_logs', db_payload):
                self.log("✅ 盘中数据切片已成功推入 Supabase。")
                self.backfill_missing_checkpoints(
                    intra_bars, datetime.now(NY_TZ).strftime('%Y-%m-%d'))
            else:
                self.log("⚠️ 数据入库最终失败。")

        except Exception as e: 
            self.log(f"❌ 运行崩溃: {e}")
        finally: 
            self.ib.disconnect()

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == 'summary': 
        GlobalSentinel().send_summary()
    else: 
        GlobalSentinel().run_scan()
