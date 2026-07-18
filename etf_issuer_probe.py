# -*- coding: utf-8 -*-
"""Non-production forward probe for official U.S. ETF issuer pages.

The probe archives immutable raw responses and best-effort field observations.
It deliberately does not write to Supabase, calculate fund flow, or feed any
score/alert. Automated scheduling requires a separate licensing review.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
import json
import logging
from pathlib import Path
import re
import time
from typing import Dict, Iterable, Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


logger = logging.getLogger(__name__)

PROBE_VERSION = "etf_issuer_probe_v1"
DEFAULT_OUTPUT_DIR = Path(".probe_data/etf_issuer")
REQUIRED_FIELDS = ("nav", "shares_outstanding")
ALLOWED_HOSTS = frozenset({
    "www.ssga.com",
    "www.ishares.com",
    "investor.vanguard.com",
    "www.invesco.com",
    "www.proshares.com",
    "prod.proshares.com",
})

TARGETS = (
    {
        "ticker": "SPY", "issuer": "State Street", "asset_class": "equity",
        "url": ("https://www.ssga.com/us/en/individual/etfs/"
                "state-street-spdr-sp-500-etf-trust-spy"),
    },
    {
        "ticker": "IVV", "issuer": "iShares", "asset_class": "equity",
        "url": ("https://www.ishares.com/us/products/239726/"
                "ivv-ishares-core-sp-500-etf"),
    },
    {
        "ticker": "VOO", "issuer": "Vanguard", "asset_class": "equity",
        "url": ("https://investor.vanguard.com/investment-products/"
                "etfs/profile/voo"),
    },
    {
        "ticker": "QQQ", "issuer": "Invesco", "asset_class": "equity",
        "url": ("https://www.invesco.com/us/financial-products/etfs/"
                "product-detail?audienceType=Investor&productId=QQQ"),
    },
    {
        "ticker": "VTI", "issuer": "Vanguard", "asset_class": "equity",
        "url": ("https://investor.vanguard.com/investment-products/"
                "etfs/profile/vti"),
    },
    {
        "ticker": "IWM", "issuer": "iShares", "asset_class": "equity",
        "url": ("https://www.ishares.com/us/products/239710/"
                "ishares-russell-2000-etf"),
    },
    {
        "ticker": "HYG", "issuer": "iShares", "asset_class": "fixed_income",
        "url": ("https://www.ishares.com/us/products/239565/"
                "ishares-iboxx-high-yield-corporate-bond-etf"),
    },
    {
        "ticker": "LQD", "issuer": "iShares", "asset_class": "fixed_income",
        "url": ("https://www.ishares.com/us/products/239566/"
                "ishares-iboxx-investment-grade-corporate-bond-etf"),
    },
    {
        "ticker": "XLF", "issuer": "State Street", "asset_class": "equity",
        "url": ("https://www.ssga.com/us/en/individual/etfs/"
                "state-street-financial-select-sector-spdr-etf-xlf"),
    },
    {
        "ticker": "TQQQ", "issuer": "ProShares", "asset_class": "leveraged",
        "url": "https://www.proshares.com/our-etfs/leveraged-and-inverse/tqqq",
    },
)

_MONTH_DATE = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},?\s+\d{4}"
)
_SLASH_DATE = r"\d{1,2}/\d{1,2}/\d{4}"
_DATE = rf"(?:{_MONTH_DATE}|{_SLASH_DATE})"
_NUMBER = r"[+-]?[0-9][0-9,]*(?:\.[0-9]+)?"
_UNIT = r"(?:T|B|BN|M|MM|K)?"


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._ignored_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"style", "noscript"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag):
        if tag.lower() in {"style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data):
        if not self._ignored_depth and data.strip():
            self.parts.append(data.strip())


def visible_text(payload: bytes, encoding: Optional[str] = None) -> str:
    """Return whitespace-normalized text while retaining script JSON payloads."""
    decoded = payload.decode(encoding or "utf-8", errors="replace")
    parser = _VisibleText()
    parser.feed(decoded)
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def _parse_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    cleaned = re.sub(r"\s+", " ", value.replace(",", "")).strip()
    for fmt in ("%b %d %Y", "%B %d %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_amount(value: Optional[str], unit: Optional[str] = None) -> Optional[float]:
    if not value:
        return None
    try:
        number = float(value.replace(",", "").replace("$", ""))
    except ValueError:
        return None
    multipliers = {
        "": 1.0, "K": 1_000.0, "M": 1_000_000.0, "MM": 1_000_000.0,
        "B": 1_000_000_000.0, "BN": 1_000_000_000.0, "T": 1_000_000_000_000.0,
    }
    normalized_unit = (unit or "").upper()
    scaled = number * multipliers.get(normalized_unit, 1.0)
    if normalized_unit and abs(scaled - round(scaled)) < 1e-6:
        return int(round(scaled))
    return scaled


def _first_match(text: str, patterns: Iterable[str]) -> Optional[Dict[str, object]]:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        groups = match.groupdict()
        value = _parse_amount(groups.get("value"), groups.get("unit"))
        if value is None:
            continue
        return {"value": value, "source_date": _parse_date(groups.get("date"))}
    return None


def extract_page_fields(target: Dict[str, str], payload: bytes,
                        encoding: Optional[str] = None) -> Dict[str, Dict[str, object]]:
    """Best-effort extraction; missing data is expected and remains fail-closed."""
    text = visible_text(payload, encoding)
    ticker = re.escape(target["ticker"])

    nav = _first_match(text, (
        rf"\bNAV\s+as of\s+(?P<date>{_DATE}).{{0,160}}?\$+\s*(?P<value>{_NUMBER})",
        rf"Fund Net Asset Value as of\s+(?P<date>{_DATE}).{{0,1000}}?\bNAV\b.{{0,300}}?\$\s*(?P<value>{_NUMBER})",
        rf"\bPrice\s+as of\s+(?P<date>{_DATE}).{{0,500}}?\bNAV\b.{{0,120}}?\$\s*(?P<value>{_NUMBER})",
        rf"\b{ticker}\b\s+(?P<date>{_SLASH_DATE})\s+\$\s*(?P<value>{_NUMBER})",
    ))
    shares = _first_match(text, (
        rf"(?<!of )(?<!fund )Shares Outstanding.{{0,100}}?(?P<value>{_NUMBER})\s*(?P<unit>{_UNIT})\b.{{0,80}}?(?:as of\s+(?P<date>{_DATE}))?",
    ))
    aum = _first_match(text, (
        rf"Net Assets of Fund.{{0,80}}?\$\s*(?P<value>{_NUMBER})\s*(?P<unit>{_UNIT})\b.{{0,80}}?(?:as of\s+(?P<date>{_DATE}))?",
        rf"Assets Under Management.{{0,120}}?\$\s*(?P<value>{_NUMBER})\s*(?P<unit>{_UNIT})\b.{{0,80}}?(?:as of\s+(?P<date>{_DATE}))?",
        rf"\bNet Assets\b.{{0,80}}?\$\s*(?P<value>{_NUMBER})\s*(?P<unit>{_UNIT})\b",
        rf"Share class total net assets.{{0,80}}?\$\s*(?P<value>{_NUMBER})\s*(?P<unit>{_UNIT})\b",
    ))

    fields = {name: value for name, value in (
        ("nav", nav), ("shares_outstanding", shares), ("aum", aum),
    ) if value is not None}

    # SSGA presents all three values under one dated NAV section. Inherit that
    # date only inside this best-effort observation; the raw page remains the
    # source of truth and the inferred date is explicitly marked.
    if target["issuer"] == "State Street" and nav and nav.get("source_date"):
        for name in ("shares_outstanding", "aum"):
            if name in fields and not fields[name].get("source_date"):
                fields[name]["source_date"] = nav["source_date"]
                fields[name]["date_inferred_from_section"] = True
    return fields


def assess_snapshot(fields: Dict[str, Dict[str, object]],
                    previous_fields: Optional[Dict[str, Dict[str, object]]] = None,
                    corporate_action_confirmed: bool = False,
                    share_jump_threshold: float = 0.20) -> Dict[str, object]:
    """Apply research-only and corporate-action fail-closed gates."""
    missing = [name for name in REQUIRED_FIELDS if name not in fields]
    if missing:
        return {
            "status": "INCOMPLETE_SOURCE_SNAPSHOT",
            "eligible_for_flow": False,
            "reasons": [f"missing:{name}" for name in missing],
        }

    dates = [fields[name].get("source_date") for name in REQUIRED_FIELDS]
    if any(value is None for value in dates) or len(set(dates)) != 1:
        return {
            "status": "INCOMPLETE_SOURCE_SNAPSHOT",
            "eligible_for_flow": False,
            "reasons": ["required_field_source_dates_missing_or_mismatched"],
        }

    nav = fields["nav"].get("value")
    current_shares = fields["shares_outstanding"].get("value")
    if not nav or not current_shares or float(nav) <= 0 or float(current_shares) <= 0:
        return {
            "status": "INCOMPLETE_SOURCE_SNAPSHOT",
            "eligible_for_flow": False,
            "reasons": ["required_field_value_invalid"],
        }

    aum = fields.get("aum", {}).get("value")
    if aum and float(aum) > 0:
        implied_aum = float(nav) * float(current_shares)
        identity_gap = float(aum) / implied_aum - 1.0
        if abs(identity_gap) > 0.05:
            return {
                "status": "INCONSISTENT_AUM_IDENTITY",
                "eligible_for_flow": False,
                "aum_identity_gap_pct": round(identity_gap * 100, 6),
                "reasons": ["shares_times_nav_does_not_match_aum"],
            }

    previous_shares = (previous_fields or {}).get("shares_outstanding", {}).get("value")
    if previous_shares and current_shares:
        change = float(current_shares) / float(previous_shares) - 1.0
        if abs(change) >= share_jump_threshold and not corporate_action_confirmed:
            return {
                "status": "SUSPECT_CORPORATE_ACTION",
                "eligible_for_flow": False,
                "share_change_pct": round(change * 100, 6),
                "reasons": ["unconfirmed_large_share_jump"],
            }

    return {
        "status": "RESEARCH_ONLY",
        "eligible_for_flow": False,
        "reasons": ["issuer_automation_license_not_confirmed"],
    }


def validate_target(target: Dict[str, str]) -> None:
    host = (urlparse(target["url"]).hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise ValueError(f"issuer host is not allowlisted: {host or '<missing>'}")
    if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", target["ticker"]):
        raise ValueError(f"invalid ticker: {target['ticker']!r}")


def _extension(content_type: str) -> str:
    lowered = (content_type or "").lower()
    if "html" in lowered:
        return ".html"
    if "json" in lowered:
        return ".json"
    if "csv" in lowered:
        return ".csv"
    if "spreadsheet" in lowered or "excel" in lowered:
        return ".xlsx"
    return ".bin"


def _previous_metadata(output_root: Path, ticker: str) -> Optional[Dict[str, object]]:
    candidates = sorted((output_root / ticker).glob("*/metadata.json"), reverse=True)
    for path in candidates:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


def archive_payload(target: Dict[str, str], payload: bytes,
                    response_headers: Optional[Dict[str, str]], http_status: int,
                    output_root: Path, fetched_at: Optional[datetime] = None,
                    encoding: Optional[str] = None,
                    response_url: Optional[str] = None,
                    redirect_chain: Optional[Iterable[str]] = None) -> Path:
    """Archive one immutable response and its non-production assessment."""
    validate_target(target)
    fetched = fetched_at or datetime.now(timezone.utc)
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    stamp = fetched.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = output_root / target["ticker"] / stamp
    run_dir.mkdir(parents=True, exist_ok=False)

    headers = {str(k).lower(): str(v) for k, v in (response_headers or {}).items()}
    content_type = headers.get("content-type", "application/octet-stream")
    raw_path = run_dir / f"raw{_extension(content_type)}"
    raw_path.write_bytes(payload)

    fields = extract_page_fields(target, payload, encoding) if http_status == 200 else {}
    previous = _previous_metadata(output_root, target["ticker"])
    assessment = assess_snapshot(fields, (previous or {}).get("fields"))
    if http_status != 200:
        assessment = {
            "status": "HTTP_ERROR", "eligible_for_flow": False,
            "reasons": [f"http_status:{http_status}"],
        }

    metadata = {
        "probe_version": PROBE_VERSION,
        "mode": "NON_PRODUCTION_RESEARCH_ONLY",
        "ticker": target["ticker"],
        "issuer": target["issuer"],
        "asset_class": target["asset_class"],
        "source_url": target["url"],
        "response_url": response_url or target["url"],
        "redirect_chain": list(redirect_chain or []),
        "fetched_at": fetched.astimezone(timezone.utc).isoformat(),
        "http_status": int(http_status),
        "content_type": content_type,
        "etag": headers.get("etag"),
        "last_modified": headers.get("last-modified"),
        "sha256": sha256(payload).hexdigest(),
        "bytes": len(payload),
        "fields": fields,
        "field_coverage": {
            name: name in fields for name in ("nav", "shares_outstanding", "aum")
        },
        "assessment": assessment,
        "production_writes": False,
    }
    (run_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return run_dir


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=2, connect=2, read=2, status=2, backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(("GET",)), respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({
        "User-Agent": "dashboard-etf-source-poc/0.1 (non-production research probe)",
        "Accept": "text/html,application/xhtml+xml,application/json,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;q=0.9,*/*;q=0.5",
    })
    return session


def run_probe(targets: Iterable[Dict[str, str]], output_root: Path,
              timeout: int = 20, delay_seconds: float = 1.5,
              session: Optional[requests.Session] = None) -> int:
    selected = list(targets)
    client = session or build_session()
    failures = 0
    for index, target in enumerate(selected):
        validate_target(target)
        try:
            response = client.get(target["url"], timeout=timeout)
            run_dir = archive_payload(
                target, response.content, dict(response.headers),
                response.status_code, output_root, encoding=response.encoding,
                response_url=response.url,
                redirect_chain=[item.url for item in response.history],
            )
            metadata = json.loads((run_dir / "metadata.json").read_text("utf-8"))
            status = metadata["assessment"]["status"]
            print(f"{target['ticker']}: HTTP {response.status_code}, {status}, {run_dir}")
            if response.status_code != 200:
                failures += 1
        except (OSError, requests.RequestException, ValueError) as exc:
            logger.error("%s probe failed: %s", target["ticker"], exc)
            failures += 1
        if index < len(selected) - 1 and delay_seconds > 0:
            time.sleep(delay_seconds)
    return failures


def _selected_targets(tickers: Optional[Iterable[str]]) -> list:
    if not tickers:
        return list(TARGETS)
    requested = {value.upper() for value in tickers}
    selected = [target for target in TARGETS if target["ticker"] in requested]
    missing = sorted(requested - {target["ticker"] for target in selected})
    if missing:
        raise ValueError(f"unknown probe ticker(s): {', '.join(missing)}")
    return selected


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", nargs="+", help="subset of the ten POC tickers")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--delay-seconds", type=float, default=1.5)
    parser.add_argument("--list-targets", action="store_true")
    parser.add_argument(
        "--acknowledge-research-use", action="store_true",
        help="required to fetch; confirms this is an isolated non-production probe",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        selected = _selected_targets(args.tickers)
    except ValueError as exc:
        parser.error(str(exc))
    if args.list_targets:
        for target in selected:
            print(f"{target['ticker']}\t{target['issuer']}\t{target['url']}")
        return 0
    if not args.acknowledge_research_use:
        parser.error("--acknowledge-research-use is required before any network fetch")
    if args.timeout <= 0 or args.delay_seconds < 0:
        parser.error("timeout must be positive and delay must be non-negative")
    return 1 if run_probe(
        selected, args.output_dir, args.timeout, args.delay_seconds) else 0


if __name__ == "__main__":
    raise SystemExit(main())
