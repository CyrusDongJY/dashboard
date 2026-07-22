import sys
import os
import math
import logging
import smtplib
from datetime import datetime, timezone
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
from data_contracts import up_down_volume_ratio

# ================= 🔐 安全挂载全局金库 =================
try:
    import market_config as cfg
except ImportError:
    print("❌ 致命错误：找不到 market_config.py 密钥文件！程序终止。")
    sys.exit(1)

# ================= 全局常量与环境配置 =================
NY_TZ = pytz.timezone('America/New_York')
DB_MAX_RETRIES = 3

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

    def get_robust_index_val(self, contract, ticker):
        """
        ✅ 终极双重抓取护盾：
        1. 流式探测：探测 last, close, marketPrice
        2. 历史兜底（重武器）：如果流式返回 0 或 NaN，立刻强行拉取 15 分钟 K 线图找数据
        """
        val = 0.0
        # --- 第一层护盾：流式提取 ---
        for attr in ['last', 'close', 'bid', 'ask']:
            v = getattr(ticker, attr, None)
            if v is not None and not math.isnan(v) and v != 0:
                val = float(v)
                break
        
        if val == 0.0 and callable(getattr(ticker, 'marketPrice', None)):
            try: 
                v = ticker.marketPrice()
                if not math.isnan(v) and v != 0: val = float(v)
            except: pass

        if val != 0.0:
            return val

        # --- 第二层护盾：重武器 (历史 K 线回溯) ---
        try:
            # 向物理服务器索要最近 15 分钟的 1分钟 K线
            bars = self.ib.reqHistoricalData(
                contract,
                endDateTime='',
                durationStr='900 S',
                barSizeSetting='1 min',
                whatToShow='TRADES',
                useRTH=False
            )
            if bars:
                return float(bars[-1].close)
        except Exception:
            self.log(
                f"⚠️ {getattr(contract, 'symbol', 'UNKNOWN')} 历史行情兜底失败")
            
        self.log(
            f"⚠️ {getattr(contract, 'symbol', 'UNKNOWN')} 行情不可用，返回缺失")
        return 0.0

    def run_scan(self):
        if not self.is_trading_day():
            self.log("🛑 拦截：今天是美股休市日，哨兵休眠。")
            return

        self.load_sentinel_context()
        
        def optional_float(val):
            try:
                number = float(val)
                return number if math.isfinite(number) else None
            except (TypeError, ValueError):
                return None

        def display_number(value, digits=2, prefix=''):
            return f"{prefix}{value:.{digits}f}" if value is not None else "NA"

        zgl = optional_float(
            self.opt_ctx.get('gamma_flip_all', self.opt_ctx.get('zgl_price')))
        gamma_flip_alert_enabled = bool(
            getattr(cfg, 'ENABLE_GAMMA_FLIP_ALERT', False)
            and self.context_quality == "OK" and zgl is not None)
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
            self.ib.connect('127.0.0.1', 4001, clientId=888, readonly=True)
            self.ib.reqMarketDataType(1)

            # ✅ 避开 IBKR 符号陷阱：直接抓取 ADV 和 DECL 进行相减，杜绝 ADD 无法识别的尴尬
            contracts = {
                'SPY': Stock('SPY', 'SMART', 'USD'),
                'TICK': Index('TICK-NYSE', 'NYSE'),
                'TRIN': Index('TRIN-NYSE', 'NYSE'),
                'ADV': Index('ADV-NYSE', 'NYSE'),   # 上涨家数
                'DECL': Index('DECL-NYSE', 'NYSE'), # 下跌家数
                'UVOL': Index('UVOL-NYSE', 'NYSE'),
                'DVOL': Index('DVOL-NYSE', 'NYSE')
            }
            self.ib.qualifyContracts(*contracts.values())
            
            with market_data_subscription(self.ib, contracts.values(), 4) as tickers:
                t_dict = {
                    getattr(t.contract, 'conId', None): t for t in tickers
                    if getattr(t, 'contract', None) is not None
                }

                def ticker_for(key):
                    return t_dict.get(getattr(contracts[key], 'conId', None))

                # 提取数据，全部过一遍 "重武器" 函数
                spy_px = self.get_robust_index_val(
                    contracts['SPY'], ticker_for('SPY'))
                if spy_px == 0: 
                    self.log("⚠️ SPY 现价获取失败，跳过本次扫描。")
                    return

                adv_val = self.get_robust_index_val(
                    contracts['ADV'], ticker_for('ADV'))
                decl_val = self.get_robust_index_val(
                    contracts['DECL'], ticker_for('DECL'))
                add_val = adv_val - decl_val  # ✅ 完美算出真实的 ADD
                
                uvol_val = self.get_robust_index_val(
                    contracts['UVOL'], ticker_for('UVOL'))
                dvol_val = self.get_robust_index_val(
                    contracts['DVOL'], ticker_for('DVOL'))
                trin_raw = self.get_robust_index_val(
                    contracts['TRIN'], ticker_for('TRIN'))
                trin_val = trin_raw if trin_raw > 0 else None
                tick_now = self.get_robust_index_val(
                    contracts['TICK'], ticker_for('TICK'))

            # 防除零错：计算量比
            vol_ratio, vol_ratio_status = up_down_volume_ratio(
                uvol_val, dvol_val)
            if vol_ratio is None:
                self.log(
                    "⚠️ NYSE U/D不可用：请核对UVOL-NYSE/DVOL-NYSE合约、"
                    "IBKR行情权限及指数历史数据能力")
            context_metadata.update({
                "uvol_raw": uvol_val if uvol_val > 0 else None,
                "dvol_raw": dvol_val if dvol_val > 0 else None,
                "vol_ratio_status": vol_ratio_status,
                "ud_source": "IBKR:UVOL-NYSE/DVOL-NYSE",
            })
            
            # 计算 15 分钟累积 TICK
            ctick_15m_avg = tick_now
            try:
                tick_bars = self.ib.reqHistoricalData(contracts['TICK'], endDateTime='', durationStr='900 S', barSizeSetting='1 min', whatToShow='TRADES', useRTH=False)
                if tick_bars: ctick_15m_avg = sum([b.close for b in tick_bars]) / len(tick_bars)
            except: pass

            # 计算 VWAP
            vwap_now, vwap_30m = spy_px, spy_px 
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
                if vol_ratio is not None and vol_ratio >= 2.0 and ctick_15m_avg > 0:
                    fire = True; title = "🥇【深海核爆】极值恐慌底反转！散户止损，机构扫货，绝佳做多点！"
            elif (
                gamma_flip_alert_enabled and zgl is not None and zgl > 0
                and spy_px < zgl and spy_px < vwap_now
            ):
                if vol_ratio is not None and vol_ratio <= 0.25 and ctick_15m_avg < -300:
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
                f"({zgl_mode}, 质量={self.context_quality})\n"
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
            r += f"NYSE上涨/下跌成交量比 (U/D): {display_number(vol_ratio)} ({vol_ratio_status})\n"
            r += f"NYSE TRIN: {display_number(trin_val)} (IBKR TRIN-NYSE, 截面={ny_timestamp_str}, 收盘竞价=UNVERIFIED)\n"
            r += f"15m累积暗流 (CTICK): {ctick_15m_avg:+.0f}\n"

            print(r)

            self.log(f"分析完成。警报状态: {fire}")
            
            if fire:
                self.log("💥 满足A+级共振，已发射全球预警！")
                self.send_alert(title, r)

            # ✅ 写入纯粹的 NY 字符串时间，屏蔽 Supabase 时区错乱
            db_payload = {
                "record_time": ny_timestamp_str,
                "spy_px": spy_px, "sma20": sma20, "vwap_now": vwap_now, "vwap_30m": vwap_30m,
                "add_val": add_val, "uvol": uvol_val, "dvol": dvol_val, "vol_ratio": vol_ratio,
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
