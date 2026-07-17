# -*- coding: utf-8 -*-
"""Pure, dependency-light data contracts shared by collection jobs."""
import math

import pandas as pd


ETF_FLOW_TICKERS = ('SPY', 'QQQ', 'HYG', 'JNK')


def compute_etf_share_metrics(history):
    """Compute 1/5/20-observation ETF share changes without filling gaps."""
    if history is None or history.empty:
        return {}
    frame = history.copy()
    required = {'Date', 'Ticker', 'Shares', 'Price'}
    if not required.issubset(frame.columns):
        return {}
    frame['Date'] = pd.to_datetime(frame['Date'], errors='coerce')
    frame['Shares'] = pd.to_numeric(frame['Shares'], errors='coerce')
    frame['Price'] = pd.to_numeric(frame['Price'], errors='coerce')
    frame = frame.dropna(subset=['Date', 'Ticker', 'Shares', 'Price'])
    result = {}
    for ticker in ETF_FLOW_TICKERS:
        rows = (frame[frame['Ticker'] == ticker].sort_values('Date')
                .drop_duplicates('Date', keep='last').reset_index(drop=True))
        if rows.empty:
            continue
        current = rows.iloc[-1]
        changes = {}
        for window in (1, 5, 20):
            baseline_index = len(rows) - 1 - window
            if baseline_index < 0:
                changes[f'{window}d'] = None
                continue
            baseline = rows.iloc[baseline_index]
            delta_shares = float(current['Shares'] - baseline['Shares'])
            baseline_shares = float(baseline['Shares'])
            changes[f'{window}d'] = {
                'share_change_m': round(delta_shares / 1e6, 4),
                'share_change_pct': (
                    round(delta_shares / baseline_shares * 100, 4)
                    if baseline_shares > 0 else None),
                'dollar_flow_m': round(
                    delta_shares * float(current['Price']) / 1e6, 2),
                'baseline_date': pd.Timestamp(
                    baseline['Date']).strftime('%Y-%m-%d'),
            }
        result[ticker] = {
            'shares_m': round(float(current['Shares']) / 1e6, 4),
            'price': round(float(current['Price']), 4),
            'as_of_date': pd.Timestamp(current['Date']).strftime('%Y-%m-%d'),
            'source': 'yfinance_info_unverified',
            'quality': 'CONTEXT_ONLY',
            'changes': changes,
        }
    return result


def calculate_iv_rank_percentile(values):
    """Return distinct IV Rank and empirical IV Percentile values."""
    clean = pd.to_numeric(pd.Series(values), errors='coerce').dropna()
    clean = clean[clean.map(lambda value: math.isfinite(float(value)) and value > 0)]
    if clean.empty:
        return {
            'current_iv': None, 'min_iv': None, 'max_iv': None,
            'iv_rank_pct': None, 'iv_percentile_pct': None,
            'sample_count': 0,
        }
    current = float(clean.iloc[-1])
    min_iv, max_iv = float(clean.min()), float(clean.max())
    rank = ((current - min_iv) / (max_iv - min_iv) * 100
            if max_iv > min_iv else 0.0)
    percentile = float((clean < current).mean() * 100)
    return {
        'current_iv': current,
        'min_iv': min_iv,
        'max_iv': max_iv,
        'iv_rank_pct': rank,
        'iv_percentile_pct': percentile,
        'sample_count': int(len(clean)),
    }


def up_down_volume_ratio(up_volume, down_volume):
    """Return a fail-closed U/D ratio and an explicit quality status."""
    try:
        up = float(up_volume)
        down = float(down_volume)
    except (TypeError, ValueError):
        return None, 'MISSING_UVOL_DVOL'
    if not (math.isfinite(up) and math.isfinite(down)) or up < 0 or down < 0:
        return None, 'MISSING_UVOL_DVOL'
    if down > 0:
        return up / down, 'OK'
    if up > 0:
        return 99.0, 'EXTREME_UP_NO_DVOL'
    return None, 'MISSING_UVOL_DVOL'
