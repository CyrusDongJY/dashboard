from datetime import datetime
import unittest

from economic_calendar import (
    NY_TZ,
    format_economic_event_summary,
    resolve_next_major_event,
)


class _Response:
    def __init__(self, payload=None, text=""):
        self.payload = payload
        self.text = text

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Session:
    def __init__(self, payloads, fomc_html):
        self.payloads = payloads
        self.fomc_html = fomc_html
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if "fomccalendars" in url:
            return _Response(text=self.fomc_html)
        release_id = kwargs["params"]["release_id"]
        return _Response({
            "release_dates": self.payloads.get(release_id, []),
        })


class EconomicCalendarTests(unittest.TestCase):
    def test_august_11_resolves_next_day_cpi_from_official_release_id(self):
        session = _Session({
            10: [
                {"release_id": 10, "release_name": "Consumer Price Index",
                 "date": "2026-08-12"},
            ],
            46: [
                {"release_id": 46, "release_name": "Producer Price Index",
                 "date": "2026-08-13"},
            ],
        }, """
        <div class="panel panel-default"><div class="panel-heading"><h4>
        <a id="2026">2026 FOMC Meetings</a></h4></div>
        <div class="row fomc-meeting">
        <div class="fomc-meeting__month"><strong>September</strong></div>
        <div class="fomc-meeting__date">15-16*</div>
        </div></div>
        <div class="panel panel-default"><div>next section</div></div>
        """)
        as_of = NY_TZ.localize(datetime(2026, 8, 11, 16, 30))
        result = resolve_next_major_event(
            as_of, fred_api_key="test-key", session=session,
            cache_path=None)

        self.assertEqual(result["name"], "美国CPI")
        self.assertEqual(result["event_at"], "2026-08-12T08:30:00-04:00")
        self.assertEqual(result["trading_days"], 1)
        self.assertEqual(result["risk"], "HIGH")
        self.assertEqual(result["event_status"], "HIGH_IMPACT_NEXT_SESSION")
        self.assertEqual(result["source_status"], "LIVE")
        self.assertEqual(len(session.calls), 6)
        for url, kwargs in session.calls:
            if "fomccalendars" in url:
                continue
            self.assertNotIn("realtime_start", kwargs["params"])
            self.assertNotIn("realtime_end", kwargs["params"])
        summary = format_economic_event_summary(result)
        self.assertIn("下一重大事件：美国CPI", summary)
        self.assertIn("发布时间：2026-08-12 08:30 ET", summary)
        self.assertIn("距离事件：1个交易日", summary)

    def test_missing_source_is_unavailable_not_unknown(self):
        as_of = NY_TZ.localize(datetime(2026, 8, 11, 8, 45))
        result = resolve_next_major_event(
            as_of, fred_api_key=None, cache_path=None)
        self.assertEqual(
            result["event_status"], "EVENT_CALENDAR_UNAVAILABLE")
        self.assertEqual(result["source_status"], "UNAVAILABLE")
        self.assertNotIn("UNKNOWN", format_economic_event_summary(result))


if __name__ == "__main__":
    unittest.main()
