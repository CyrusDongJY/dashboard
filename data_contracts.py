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


def gap_acceptance(previous_close, open_price, close_price, atr20=None,
                   min_gap_pct=0.15, min_gap_atr=0.10):
    """Measure whether the close retained or extended an eligible opening gap."""
    result = {
        'gap_pct': None, 'acceptance_ratio': None, 'state': 'MISSING',
        'threshold_pct': None, 'quality': 'MISSING_PRICE',
    }
    try:
        previous = float(previous_close)
        opened = float(open_price)
        closed = float(close_price)
    except (TypeError, ValueError):
        return result
    if not all(math.isfinite(value) and value > 0
               for value in (previous, opened, closed)):
        return result

    atr_threshold = 0.0
    try:
        atr = float(atr20)
        if math.isfinite(atr) and atr > 0:
            atr_threshold = min_gap_atr * atr / previous * 100.0
    except (TypeError, ValueError):
        pass
    threshold = max(float(min_gap_pct), atr_threshold)
    gap = opened - previous
    gap_pct = gap / previous * 100.0
    result.update({'gap_pct': gap_pct, 'threshold_pct': threshold})
    if abs(gap_pct) < threshold:
        result.update({'state': 'NO_MATERIAL_GAP', 'quality': 'SMALL_GAP'})
        return result

    ratio = (closed - previous) / gap
    if ratio >= 1.0:
        state = 'EXPANDED'
    elif ratio >= 0.0:
        state = 'PARTIALLY_RETAINED'
    else:
        state = 'REVERSED'
    result.update({
        'acceptance_ratio': ratio,
        'state': state,
        'quality': 'OK',
    })
    return result


def vwap_acceptance(bars, expected_samples=78, minimum_coverage=0.80):
    """Compute RTH time/volume acceptance above contemporaneous cumulative VWAP."""
    result = {
        'time_acceptance_pct': None,
        'volume_acceptance_pct': None,
        'last_vwap': None,
        'sample_count': 0,
        'coverage_pct': 0.0,
        'quality': 'MISSING_BARS',
    }
    if bars is None:
        return result
    frame = pd.DataFrame(bars).copy()
    required = {'close', 'volume'}
    if frame.empty or not required.issubset(frame.columns):
        return result
    for column in ('close', 'volume', 'high', 'low'):
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors='coerce')
    frame = frame.dropna(subset=['close', 'volume'])
    frame = frame[(frame['close'] > 0) & (frame['volume'] > 0)]
    if frame.empty:
        return result

    if {'high', 'low'}.issubset(frame.columns):
        typical = (frame['high'] + frame['low'] + frame['close']) / 3.0
        typical = typical.where(typical.notna(), frame['close'])
    else:
        typical = frame['close']
    cumulative_volume = frame['volume'].cumsum()
    cumulative_vwap = (typical * frame['volume']).cumsum() / cumulative_volume
    above = typical > cumulative_vwap
    samples = int(len(frame))
    coverage = min(1.0, samples / max(int(expected_samples), 1))
    quality = 'OK' if coverage >= minimum_coverage else 'LOW_COVERAGE'
    result.update({
        'time_acceptance_pct': float(above.mean() * 100.0),
        'volume_acceptance_pct': float(
            frame.loc[above, 'volume'].sum() / frame['volume'].sum() * 100.0),
        'last_vwap': float(cumulative_vwap.iloc[-1]),
        'sample_count': samples,
        'coverage_pct': coverage * 100.0,
        'quality': quality,
    })
    return result


def classify_hyg_tlt(hyg_return_pct, tlt_return_pct, ratio_return_pct):
    """Return a graded, attribution-aware credit-vs-duration label."""
    try:
        hyg = float(hyg_return_pct)
        tlt = float(tlt_return_pct)
        ratio = float(ratio_return_pct)
    except (TypeError, ValueError):
        return 'DATA_MISSING'
    if not all(math.isfinite(value) for value in (hyg, tlt, ratio)):
        return 'DATA_MISSING'
    if ratio >= 2.0:
        if hyg > 0 >= tlt:
            return 'CREDIT_STRONG_RATE_DRIVEN'
        if hyg > 0 and tlt > 0:
            return 'BROAD_RISK_SUPPORT'
        return 'RATIO_UP_WITH_BOTH_WEAK'
    if ratio <= -2.0:
        if hyg < 0 <= tlt:
            return 'CREDIT_STRESS_FLIGHT_TO_DURATION'
        return 'CREDIT_RELATIVE_WEAKNESS'
    return 'MIXED_NEUTRAL'


def concentration_attribution(returns_pct, cap_weights, equal_weights,
                              group_by_symbol):
    """Attribute cap-minus-equal return spread using mutually exclusive groups."""
    symbols = set(returns_pct) & set(cap_weights) & set(equal_weights)
    result = {
        'quality': 'MISSING_POINT_IN_TIME_WEIGHTS',
        'spread_pct': None,
        'group_contributions_pct': {},
        'symbol_count': len(symbols),
    }
    if not symbols or symbols != set(returns_pct):
        return result
    contributions = {}
    try:
        for symbol in sorted(symbols):
            value = ((float(cap_weights[symbol]) - float(equal_weights[symbol]))
                     * float(returns_pct[symbol]))
            if not math.isfinite(value):
                return result
            group = group_by_symbol.get(symbol)
            if not group:
                return result
            contributions[group] = contributions.get(group, 0.0) + value
    except (TypeError, ValueError):
        return result
    result.update({
        'quality': 'OK',
        'spread_pct': sum(contributions.values()),
        'group_contributions_pct': contributions,
    })
    return result
