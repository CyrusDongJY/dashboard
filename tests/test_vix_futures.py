import unittest
from datetime import date
from types import SimpleNamespace

import pandas as pd

from vix_futures import (
    aligned_curve, monthly_expiration, select_monthly_contracts,
    validated_curve,
)


def contract(expiry, con_id=1, **overrides):
    values = dict(
        secType='FUT', symbol='VIX', exchange='CFE', multiplier='1000',
        currency='USD', conId=con_id, localSymbol='VX', tradingClass='VX',
        lastTradeDateOrContractMonth=expiry)
    values.update(overrides)
    return SimpleNamespace(**values)


def bar(stamp, price, volume=10):
    return SimpleNamespace(date=pd.Timestamp(stamp), close=price, volume=volume)


class MonthlySelectionTests(unittest.TestCase):
    def setUp(self):
        self.sessions = set(pd.bdate_range('2025-01-01', '2028-01-01').date)

    def test_weekly_expirations_are_excluded_even_when_the_nearest(self):
        contracts = [contract('20261014', 1), contract('20261021', 2),
                     contract('20261028', 3), contract('20261118', 4)]
        selected = select_monthly_contracts(
            [SimpleNamespace(contract=c) for c in contracts],
            date(2026, 10, 9), self.sessions)
        self.assertEqual([c.conId for c in selected], [2, 4])

    def test_expiration_day_rolls_to_next_standard_month(self):
        contracts = [contract('20261021', 1), contract('20261118', 2),
                     contract('20261216', 3)]
        selected = select_monthly_contracts(
            [SimpleNamespace(contract=c) for c in contracts],
            date(2026, 10, 21), self.sessions)
        self.assertEqual([c.conId for c in selected], [2, 3])

    def test_spx_friday_holiday_moves_vx_to_tuesday(self):
        self.sessions.remove(date(2025, 4, 18))  # Good Friday
        self.assertEqual(monthly_expiration(2025, 3, self.sessions), date(2025, 3, 18))

    def test_mini_and_ambiguous_contracts_are_not_selected(self):
        mini = contract('20261021', 1, multiplier='100')
        self.assertEqual(select_monthly_contracts(
            [SimpleNamespace(contract=mini)], date(2026, 10, 9), self.sessions), [])
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            select_monthly_contracts([
                SimpleNamespace(contract=contract('20261021', 1)),
                SimpleNamespace(contract=contract('20261021', 2)),
            ], date(2026, 10, 9), self.sessions)


class AlignedPriceTests(unittest.TestCase):
    def setUp(self):
        self.as_of = date(2026, 10, 9)
        self.contracts = [contract('20261021', 1), contract('20261118', 2)]

    def curve(self, left, right):
        return aligned_curve(self.contracts, [left, right], self.as_of)

    def test_standard_month_pair_has_shared_timestamp_and_provenance(self):
        result = self.curve(
            [bar('2026-10-09T19:55:00Z', 17.1)],
            [bar('2026-10-09T19:55:00Z', 17.85)])
        self.assertEqual(result['vix_curve_quality'], 'OK')
        self.assertEqual(result['vix_curve_state'], 'CONTANGO')
        self.assertAlmostEqual(result['vix_contango_pct'], 4.3860, places=4)
        self.assertEqual(result['vix_curve_metadata']['price_as_of'], '2026-10-09T16:00:00-04:00')
        self.assertEqual(result['vix_curve_metadata']['contracts'][0]['dte'], 12)
        self.assertEqual(validated_curve(result)[0], 'CONTANGO')

    def test_unequal_dates_times_and_naive_times_do_not_form_a_curve(self):
        left = [bar('2026-10-09T19:55:00Z', 17.1)]
        for timestamp in ('2026-10-08T19:55:00Z', '2026-10-09T19:50:00Z', '2026-10-09 15:55'):
            with self.subTest(timestamp=timestamp):
                result = self.curve(left, [bar(timestamp, 17.85)])
                self.assertNotEqual(result['vix_curve_quality'], 'OK')
                self.assertIsNone(result['vix_contango_pct'])

    def test_current_day_but_stale_or_untraded_bars_are_rejected(self):
        for stamp, price, volume in (
            ('2026-10-09T18:00:00Z', 17, 5),
            ('2026-10-09T19:55:00Z', 17, 0),
            ('2026-10-09T19:55:00Z', float('nan'), 5),
            ('2026-10-09T19:55:00Z', float('inf'), 5),
            ('2026-10-09T19:55:00Z', 0, 5),
        ):
            with self.subTest(stamp=stamp, price=price, volume=volume):
                result = self.curve([bar(stamp, price, volume)], [bar(stamp, 18)])
                self.assertEqual(result['vix_curve_state'], 'UNAVAILABLE')

    def test_selects_latest_common_traded_bar_instead_of_mixing_last_bars(self):
        result = self.curve(
            [bar('2026-10-09T19:50:00Z', 17), bar('2026-10-09T19:55:00Z', 19)],
            [bar('2026-10-09T19:50:00Z', 18)])
        self.assertEqual(result['vix_m1'], 17)
        self.assertEqual(result['vix_curve_state'], 'CONTANGO')
        self.assertEqual(result['vix_curve_metadata']['price_as_of'], '2026-10-09T15:55:00-04:00')

    def test_legacy_weekly_rows_are_not_decision_eligible(self):
        self.assertEqual(validated_curve({'vix_m1': 17.65, 'vix_m2': 17.1}), ('UNAVAILABLE', None))
        self.assertEqual(validated_curve({
            'vix_m1': 17.65, 'vix_m2': 17.1,
            'vix_curve_quality': 'OK',
            'vix_curve_metadata': {'selection': 'WEEKLY'},
        }), ('UNAVAILABLE', None))


if __name__ == '__main__':
    unittest.main()
