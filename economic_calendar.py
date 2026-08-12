"""Verified US macro-event context for pre-market quality gates."""

from datetime import date, datetime, time, timedelta
import json
import os
from pathlib import Path
import re

import pytz
import requests


FRED_RELEASE_DATES_URL = (
    "https://api.stlouisfed.org/fred/release/dates")
FOMC_CALENDAR_URL = (
    "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm")
NY_TZ = pytz.timezone("America/New_York")
DEFAULT_LOOKAHEAD_DAYS = 60
DEFAULT_CACHE_MAX_AGE_HOURS = 7 * 24

# FRED release ids link back to the originating BLS/BEA release. Times are the
# agencies' regular Eastern release times and remain explicit rather than being
# inferred from a date-only API response.
MAJOR_RELEASES = {
    10: {
        "name": "美国CPI",
        "time_et": "08:30",
        "risk": "HIGH",
        "publisher": "U.S. Bureau of Labor Statistics",
        "source_url": "https://www.bls.gov/schedule/news_release/cpi.htm",
    },
    46: {
        "name": "美国PPI",
        "time_et": "08:30",
        "risk": "HIGH",
        "publisher": "U.S. Bureau of Labor Statistics",
        "source_url": "https://www.bls.gov/schedule/news_release/ppi.htm",
    },
    50: {
        "name": "美国非农就业报告",
        "time_et": "08:30",
        "risk": "HIGH",
        "publisher": "U.S. Bureau of Labor Statistics",
        "source_url": "https://www.bls.gov/schedule/news_release/empsit.htm",
    },
    53: {
        "name": "美国GDP",
        "time_et": "08:30",
        "risk": "HIGH",
        "publisher": "U.S. Bureau of Economic Analysis",
        "source_url": "https://www.bea.gov/news/schedule",
    },
    54: {
        "name": "美国个人收入与PCE",
        "time_et": "08:30",
        "risk": "HIGH",
        "publisher": "U.S. Bureau of Economic Analysis",
        "source_url": "https://www.bea.gov/news/schedule",
    },
}

MONTHS = {
    name: number for number, name in enumerate((
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ), start=1)
}


def _parse_date(value):
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def _event_datetime(event_date, time_et):
    hour, minute = (int(part) for part in str(time_et).split(":", 1))
    return NY_TZ.localize(datetime.combine(_parse_date(event_date), time(hour, minute)))


def _trading_day_distance(start_date, end_date):
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if end <= start:
        return 0
    try:
        import pandas_market_calendars as mcal

        schedule = mcal.get_calendar("NYSE").schedule(
            start_date=start.isoformat(), end_date=end.isoformat())
        return sum(index.date() > start for index in schedule.index)
    except (ImportError, AttributeError, TypeError):
        current = start + timedelta(days=1)
        count = 0
        while current <= end:
            if current.weekday() < 5:
                count += 1
            current += timedelta(days=1)
        return count


def _normalize_configured_events(events):
    normalized = []
    for item in events or []:
        if not isinstance(item, dict) or not item.get("date"):
            continue
        try:
            event_at = _event_datetime(
                item["date"], item.get("time_et", "08:30"))
        except (TypeError, ValueError):
            continue
        normalized.append({
            "name": str(item.get("name") or "已配置重大事件"),
            "event_at": event_at.isoformat(),
            "risk": str(item.get("risk") or "HIGH").upper(),
            "publisher": str(item.get("publisher") or "REVIEWED_CONFIG"),
            "source": "REVIEWED_CONFIG",
            "source_url": item.get("source_url"),
        })
    return normalized


def _fetch_fred_events(api_key, start_date, end_date, session=None,
                       timeout=15):
    if not api_key:
        raise ValueError("FRED_API_KEY_MISSING")
    client = session or requests.Session()
    events = []
    for release_id, definition in MAJOR_RELEASES.items():
        response = client.get(
            FRED_RELEASE_DATES_URL,
            params={
                "release_id": release_id,
                "api_key": api_key,
                "file_type": "json",
                "include_release_dates_with_no_data": "true",
                "limit": 1000,
                "sort_order": "desc",
            },
            headers={"User-Agent": "market-dashboard/1.0"},
            timeout=timeout,
        )
        response.raise_for_status()
        for row in response.json().get("release_dates", []):
            try:
                row_date = _parse_date(row["date"])
                if not start_date <= row_date <= end_date:
                    continue
                event_at = _event_datetime(row_date, definition["time_et"])
            except (KeyError, TypeError, ValueError):
                continue
            events.append({
                "name": definition["name"],
                "event_at": event_at.isoformat(),
                "risk": definition["risk"],
                "publisher": definition["publisher"],
                "source": f"FRED_RELEASE_CALENDAR:{release_id}",
                "source_url": definition["source_url"],
            })
    return events


def _fetch_fomc_events(start_date, end_date, session=None, timeout=15):
    client = session or requests.Session()
    response = client.get(
        FOMC_CALENDAR_URL,
        headers={"User-Agent": "market-dashboard/1.0"},
        timeout=timeout,
    )
    response.raise_for_status()
    html = response.text
    events = []
    for year in range(start_date.year, end_date.year + 1):
        section_match = re.search(
            rf'>{year} FOMC Meetings</a>.*?'
            r'(?=<div class="panel panel-default">|\Z)',
            html, flags=re.DOTALL)
        if not section_match:
            raise ValueError(f"FOMC_YEAR_SECTION_MISSING:{year}")
        rows = re.findall(
            r'fomc-meeting__month[^>]*><strong>([^<]+)</strong></div>.*?'
            r'fomc-meeting__date[^>]*>([^<]+)</div>',
            section_match.group(0), flags=re.DOTALL)
        if not rows:
            raise ValueError(f"FOMC_MEETING_ROWS_MISSING:{year}")
        for month_text, day_text in rows:
            month_name = month_text.strip().split("/")[-1]
            days = [int(value) for value in re.findall(r'\d+', day_text)]
            if month_name not in MONTHS or not days:
                continue
            try:
                event_date = date(year, MONTHS[month_name], days[-1])
            except ValueError:
                continue
            if not start_date <= event_date <= end_date:
                continue
            event_at = _event_datetime(event_date, "14:00")
            events.append({
                "name": "美联储FOMC利率决议",
                "event_at": event_at.isoformat(),
                "risk": "HIGH",
                "publisher": "Board of Governors of the Federal Reserve System",
                "source": "FEDERAL_RESERVE_FOMC_CALENDAR",
                "source_url": FOMC_CALENDAR_URL,
            })
    return events


def _write_cache(cache_path, events, fetched_at):
    if not cache_path:
        return
    path = Path(cache_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps({
        "fetched_at": fetched_at.isoformat(),
        "events": events,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _read_cache(cache_path, as_of, max_age_hours):
    if not cache_path:
        return []
    try:
        payload = json.loads(
            Path(cache_path).expanduser().read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(payload["fetched_at"])
        if fetched_at.tzinfo is None:
            fetched_at = NY_TZ.localize(fetched_at)
        age_hours = (as_of - fetched_at.astimezone(NY_TZ)).total_seconds() / 3600
        return payload.get("events", []) if 0 <= age_hours <= max_age_hours else []
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return []


def resolve_next_major_event(as_of, fred_api_key=None, configured_events=None,
                             cache_path=None, session=None,
                             lookahead_days=DEFAULT_LOOKAHEAD_DAYS,
                             cache_max_age_hours=DEFAULT_CACHE_MAX_AGE_HOURS):
    """Return the next verified event or an explicit unavailable state."""
    if as_of.tzinfo is None:
        as_of = NY_TZ.localize(as_of)
    else:
        as_of = as_of.astimezone(NY_TZ)
    start_date = as_of.date()
    end_date = start_date + timedelta(days=lookahead_days)
    configured = _normalize_configured_events(configured_events)

    source_status = "LIVE"
    source_error = None
    try:
        fetched = _fetch_fred_events(
            fred_api_key, start_date, end_date, session=session)
        fetched += _fetch_fomc_events(
            start_date, end_date, session=session)
        events = configured + fetched
        try:
            _write_cache(cache_path, events, as_of)
        except OSError:
            pass
    except (requests.RequestException, ValueError, TypeError) as exc:
        source_error = f"{type(exc).__name__}:{exc}"
        cached = _read_cache(cache_path, as_of, cache_max_age_hours)
        if cached:
            events = configured + cached
            source_status = "CACHED"
        elif configured:
            events = configured
            source_status = "CONFIG_ONLY"
        else:
            return {
                "event_status": "EVENT_CALENDAR_UNAVAILABLE",
                "source_status": "UNAVAILABLE",
                "source_error": source_error,
                "decision_eligible": False,
            }

    candidates = []
    for event in events:
        try:
            event_at = datetime.fromisoformat(event["event_at"])
            if event_at.tzinfo is None:
                event_at = NY_TZ.localize(event_at)
            event_at = event_at.astimezone(NY_TZ)
        except (KeyError, TypeError, ValueError):
            continue
        if as_of < event_at <= NY_TZ.localize(datetime.combine(
                end_date, time(23, 59, 59))):
            candidates.append((event_at, event))

    if not candidates:
        return {
            "event_status": "NO_MAJOR_EVENT_SCHEDULED",
            "source_status": source_status,
            "source_error": source_error,
            "decision_eligible": source_status in ("LIVE", "CACHED"),
        }

    event_at, event = min(candidates, key=lambda item: item[0])
    distance = _trading_day_distance(as_of.date(), event_at.date())
    risk = str(event.get("risk") or "HIGH").upper()
    if risk == "HIGH" and distance == 0:
        status = "HIGH_IMPACT_EVENT_DAY"
    elif risk == "HIGH" and distance == 1:
        status = "HIGH_IMPACT_NEXT_SESSION"
    else:
        status = "SCHEDULED"
    return {
        **event,
        "event_at": event_at.isoformat(),
        "trading_days": distance,
        "event_status": status,
        "source_status": source_status,
        "source_error": source_error,
        "decision_eligible": True,
    }


def format_economic_event_summary(context):
    context = context or {}
    status = context.get("event_status", "EVENT_CALENDAR_UNAVAILABLE")
    lines = ["\n【重大事件日历】"]
    if status == "EVENT_CALENDAR_UNAVAILABLE":
        lines.extend([
            "下一重大事件：无法核验",
            "日历状态：UNAVAILABLE（Expected Move事件分项不可用）",
        ])
        return "\n".join(lines) + "\n"
    if status == "NO_MAJOR_EVENT_SCHEDULED":
        lines.extend([
            "下一重大事件：未在查询窗口内发现已确认事件",
            f"日历状态：{context.get('source_status', 'UNAVAILABLE')}",
        ])
        return "\n".join(lines) + "\n"
    event_at = datetime.fromisoformat(context["event_at"]).astimezone(NY_TZ)
    distance = context.get("trading_days")
    lines.extend([
        f"下一重大事件：{context.get('name', '未命名事件')}",
        f"发布时间：{event_at.strftime('%Y-%m-%d %H:%M')} ET",
        f"距离事件：{distance}个交易日",
        f"事件风险等级：{context.get('risk', 'UNAVAILABLE')}",
        f"日历来源：{context.get('source', 'UNAVAILABLE')} "
        f"({context.get('source_status', 'UNAVAILABLE')})",
    ])
    return "\n".join(lines) + "\n"
