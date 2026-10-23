"""Standard-monthly VX selection and synchronized, auditable curve prices."""

from datetime import date, datetime, time, timedelta
import math

import pandas as pd

from data_contracts import classify_vix_curve


NY_ZONE = 'America/New_York'
CURVE_VERSION = 'vx_monthly_16h_5min_v1'


def monthly_expiration(year, month, sessions):
    """CFE monthly settlement: 30 days before next month's third Friday.

    Exchange holidays on that Friday or Wednesday move settlement to the
    previous business day. sessions must cover both dates (NYSE calendar).
    """
    next_month = date(year + (month == 12), month % 12 + 1, 1)
    friday = next_month + timedelta(days=(4 - next_month.weekday()) % 7 + 14)
    wednesday = friday - timedelta(days=30)
    if friday not in sessions or wednesday not in sessions:
        wednesday -= timedelta(days=1)
        for _ in range(7):
            if wednesday in sessions:
                break
            wednesday -= timedelta(days=1)
        else:
            raise ValueError('Monthly VX expiry is outside the session calendar')
    return wednesday


def contract_expiration(contract):
    raw = str(getattr(contract, 'lastTradeDateOrContractMonth', ''))
    try:
        return datetime.strptime(raw[:8], '%Y%m%d').date()
    except ValueError:
        return None


def select_monthly_contracts(details, as_of, sessions):
    """Use exchange settlement dates, never 'the next two expirations'."""
    by_expiry = {}
    for detail in details:
        contract = detail.contract
        expiry = contract_expiration(contract)
        if expiry is None or expiry <= as_of:
            continue
        if (getattr(contract, 'secType', '') != 'FUT'
                or getattr(contract, 'symbol', '') != 'VIX'
                or getattr(contract, 'exchange', '') != 'CFE'
                or str(getattr(contract, 'multiplier', '')) != '1000'
                or getattr(contract, 'currency', '') != 'USD'
                or not getattr(contract, 'conId', 0)):
            continue
        if expiry != monthly_expiration(expiry.year, expiry.month, sessions):
            continue
        existing = by_expiry.get(expiry)
        if existing is not None and existing.conId != contract.conId:
            raise ValueError(f'Ambiguous monthly VX contracts at {expiry}')
        by_expiry[expiry] = contract
    return [by_expiry[expiry] for expiry in sorted(by_expiry)[:2]]


def contract_metadata(contract, as_of):
    expiry = contract_expiration(contract)
    return {
        'con_id': contract.conId,
        'local_symbol': contract.localSymbol,
        'trading_class': contract.tradingClass,
        'expiry': expiry.isoformat(),
        'dte': (expiry - as_of).days,
        'type': 'STANDARD_MONTHLY',
    }


def unavailable_curve(quality, metadata=None):
    return {
        'vix_m1': None, 'vix_m2': None, 'vix_contango_pct': None,
        'vix_curve_state': 'UNAVAILABLE',
        'vix_curve_quality': quality,
        'vix_curve_source_date': None,
        'vix_curve_metadata': {
            'version': CURVE_VERSION, 'selection': 'STANDARD_MONTHLY',
            **(metadata or {}),
        },
        'vix_curve_flat_abs_threshold': 0.10,
        'vix_curve_flat_pct_threshold': 0.50,
    }


def _usable_bars(bars, as_of):
    cutoff = pd.Timestamp(datetime.combine(as_of, time(16)), tz=NY_ZONE)
    result = {}
    for bar in bars:
        try:
            stamp = pd.Timestamp(bar.date)
            if stamp.tzinfo is None:
                # formatDate=2 returns aware UTC datetimes. Never guess a zone.
                continue
            stamp = stamp.tz_convert(NY_ZONE)
            price, volume = float(bar.close), float(bar.volume)
            if (not math.isfinite(price) or price <= 0
                    or not math.isfinite(volume) or volume <= 0):
                continue
            if not cutoff - pd.Timedelta(minutes=15) <= stamp <= cutoff - pd.Timedelta(minutes=5):
                continue
            if stamp.second or stamp.microsecond or stamp.minute % 5:
                continue
            result[stamp] = price
        except (ValueError, TypeError, AttributeError):
            continue
    return result


def aligned_curve(contracts, bars_by_leg, as_of):
    """Both legs must trade in the same 5-minute bar near the 16:00 cutoff."""
    metadata = {
        'contracts': [contract_metadata(c, as_of) for c in contracts],
        'requested_cutoff': f'{as_of.isoformat()}T16:00:00 America/New_York',
        'price_type': 'IBKR_5MIN_TRADES_CLOSE',
        'use_rth': False,
    }
    if len(contracts) != 2 or len(bars_by_leg) != 2:
        return unavailable_curve('MISSING_LEG', metadata)
    left, right = [_usable_bars(bars, as_of) for bars in bars_by_leg]
    common = sorted(left.keys() & right.keys())
    if not common:
        return unavailable_curve('NO_ALIGNED_CURRENT_DAY_TRADES', metadata)
    stamp = common[-1]
    m1, m2 = left[stamp], right[stamp]
    state, spread = classify_vix_curve(m1, m2)
    if state == 'UNAVAILABLE':
        return unavailable_curve('INVALID_PRICE', metadata)
    metadata.update({
        'version': CURVE_VERSION, 'selection': 'STANDARD_MONTHLY',
        'bar_start': stamp.isoformat(),
        'price_as_of': (stamp + pd.Timedelta(minutes=5)).isoformat(),
        'raw_m1': m1, 'raw_m2': m2,
    })
    return {
        'vix_m1': m1, 'vix_m2': m2,
        'vix_contango_pct': round(spread, 4),
        'vix_curve_state': state, 'vix_curve_quality': 'OK',
        'vix_curve_source_date': as_of.isoformat(),
        'vix_curve_metadata': metadata,
        'vix_curve_flat_abs_threshold': 0.10,
        'vix_curve_flat_pct_threshold': 0.50,
    }


def validated_curve(row):
    """Reject legacy unverified curves before any downstream trading signal."""
    metadata = row.get('vix_curve_metadata')
    if (row.get('vix_curve_quality') != 'OK'
            or not isinstance(metadata, dict)
            or metadata.get('selection') != 'STANDARD_MONTHLY'):
        return 'UNAVAILABLE', None
    return classify_vix_curve(row.get('vix_m1'), row.get('vix_m2'))
