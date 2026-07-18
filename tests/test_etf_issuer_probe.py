import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from etf_issuer_probe import (
    archive_payload,
    assess_snapshot,
    extract_page_fields,
    validate_target,
)


ISHARES_TARGET = {
    "ticker": "IVV", "issuer": "iShares", "asset_class": "equity",
    "url": "https://www.ishares.com/us/products/239726/ivv-ishares-core-sp-500-etf",
}

SSGA_TARGET = {
    "ticker": "SPY", "issuer": "State Street", "asset_class": "equity",
    "url": "https://www.ssga.com/us/en/individual/etfs/state-street-spdr-sp-500-etf-trust-spy",
}


class IssuerFieldExtractionTests(unittest.TestCase):
    def test_extracts_ishares_nav_shares_and_aum_with_dates(self):
        page = b"""
        <html><body>
          <h2>NAV as of Jul 17, 2026</h2><span>$$746.73</span>
          <div>Net Assets of Fund $880,323,889,848 as of Jul 17, 2026</div>
          <div>Shares Outstanding 1,178,900,000 as of Jul 17, 2026</div>
        </body></html>
        """
        fields = extract_page_fields(ISHARES_TARGET, page)
        self.assertEqual(fields["nav"]["value"], 746.73)
        self.assertEqual(fields["nav"]["source_date"], "2026-07-17")
        self.assertEqual(fields["shares_outstanding"]["value"], 1_178_900_000)
        self.assertEqual(fields["shares_outstanding"]["source_date"], "2026-07-17")
        self.assertEqual(fields["aum"]["value"], 880_323_889_848)

    def test_ssga_section_date_is_explicitly_marked_as_inferred(self):
        page = b"""
        <html><body>
          <p>NAV is assets minus liabilities divided by the number of shares outstanding.</p>
          <h2>Fund Net Asset Value as of Jul 14 2026</h2>
          <div>NAV $751.76</div>
          <div>Shares Outstanding 1,050.18 M</div>
          <div>Assets Under Management $789,482.29 M</div>
        </body></html>
        """
        fields = extract_page_fields(SSGA_TARGET, page)
        self.assertEqual(fields["shares_outstanding"]["value"], 1_050_180_000)
        self.assertEqual(fields["shares_outstanding"]["source_date"], "2026-07-14")
        self.assertTrue(fields["shares_outstanding"]["date_inferred_from_section"])
        self.assertEqual(fields["aum"]["value"], 789_482_290_000)


class IssuerFailClosedTests(unittest.TestCase):
    def test_missing_shares_never_becomes_flow_eligible(self):
        assessment = assess_snapshot({
            "nav": {"value": 100.0, "source_date": "2026-07-17"},
        })
        self.assertEqual(assessment["status"], "INCOMPLETE_SOURCE_SNAPSHOT")
        self.assertFalse(assessment["eligible_for_flow"])

    def test_mismatched_source_dates_are_rejected(self):
        assessment = assess_snapshot({
            "nav": {"value": 100.0, "source_date": "2026-07-17"},
            "shares_outstanding": {
                "value": 1_000_000, "source_date": "2026-07-16",
            },
        })
        self.assertEqual(assessment["status"], "INCOMPLETE_SOURCE_SNAPSHOT")

    def test_large_unconfirmed_share_jump_is_suspect_corporate_action(self):
        previous = {
            "shares_outstanding": {
                "value": 1_000_000, "source_date": "2026-07-16",
            },
        }
        current = {
            "nav": {"value": 50.0, "source_date": "2026-07-17"},
            "shares_outstanding": {
                "value": 2_000_000, "source_date": "2026-07-17",
            },
        }
        assessment = assess_snapshot(current, previous)
        self.assertEqual(assessment["status"], "SUSPECT_CORPORATE_ACTION")
        self.assertFalse(assessment["eligible_for_flow"])

    def test_inconsistent_aum_identity_catches_parser_or_source_error(self):
        assessment = assess_snapshot({
            "nav": {"value": 100.0, "source_date": "2026-07-17"},
            "shares_outstanding": {
                "value": 100.0, "source_date": "2026-07-17",
            },
            "aum": {"value": 1_000_000_000, "source_date": "2026-07-17"},
        })
        self.assertEqual(assessment["status"], "INCONSISTENT_AUM_IDENTITY")
        self.assertFalse(assessment["eligible_for_flow"])

    def test_complete_page_remains_research_only(self):
        assessment = assess_snapshot({
            "nav": {"value": 100.0, "source_date": "2026-07-17"},
            "shares_outstanding": {
                "value": 1_000_000, "source_date": "2026-07-17",
            },
        })
        self.assertEqual(assessment["status"], "RESEARCH_ONLY")
        self.assertFalse(assessment["eligible_for_flow"])


class IssuerArchiveTests(unittest.TestCase):
    def test_archive_retains_raw_hash_lineage_and_no_production_write(self):
        page = b"<html><body>NAV as of Jul 17, 2026 $100.00</body></html>"
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = archive_payload(
                ISHARES_TARGET, page,
                {"Content-Type": "text/html", "ETag": '"abc"'}, 200,
                Path(temporary),
                fetched_at=datetime(2026, 7, 18, 1, 2, 3, tzinfo=timezone.utc),
            )
            metadata = json.loads((run_dir / "metadata.json").read_text("utf-8"))
            self.assertTrue((run_dir / "raw.html").exists())
            self.assertEqual(metadata["ticker"], "IVV")
            self.assertEqual(metadata["etag"], '"abc"')
            self.assertFalse(metadata["production_writes"])
            self.assertFalse(metadata["assessment"]["eligible_for_flow"])

    def test_non_official_host_is_rejected(self):
        target = dict(ISHARES_TARGET, url="https://example.com/ivv")
        with self.assertRaises(ValueError):
            validate_target(target)


if __name__ == "__main__":
    unittest.main()
