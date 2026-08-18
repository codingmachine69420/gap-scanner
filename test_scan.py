"""Tests for scan.py's scheduling logic: the sleep-to-target, the
should_run() guard, and the fast/range mode split (including the
09:45 segment/reversal fields). Deliberately scoped to the pure,
testable pieces — main() itself does live network I/O and isn't
covered here.
"""
from __future__ import annotations

import json
import unittest
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import scan


def _bar(day: date, time_str: str, o: float, h: float, l: float, c: float,
         v: int = 0) -> dict:
    """Build a bar dict in the shape scan.py expects, timestamped in ET
    (fixed -04:00 EDT offset — all test dates below are August, so no DST
    edge case applies)."""
    return {"t": f"{day.isoformat()}T{time_str}-04:00",
            "o": o, "h": h, "l": l, "c": c, "v": v}


class TargetDatetimeTests(unittest.TestCase):
    def test_target_is_today_at_09_32_30_et(self):
        now = datetime(2026, 8, 18, 8, 0, 0, tzinfo=scan.ET)
        target = scan.target_datetime(now)
        self.assertEqual(target, datetime(2026, 8, 18, 9, 32, 30, tzinfo=scan.ET))


class SecondsUntilTargetTests(unittest.TestCase):
    def test_positive_wait_before_target(self):
        now = datetime(2026, 8, 18, 9, 0, 0, tzinfo=scan.ET)
        wait = scan.seconds_until_target(now)
        self.assertAlmostEqual(wait, 32 * 60 + 30, delta=1)

    def test_negative_wait_after_target(self):
        # This is the exact bug scenario: a 10:13 ET start against a 09:32:30
        # target must yield a negative wait (start immediately), not a guard
        # rejection.
        now = datetime(2026, 8, 18, 10, 13, 0, tzinfo=scan.ET)
        wait = scan.seconds_until_target(now)
        self.assertLess(wait, 0)

    def test_zero_at_exact_target(self):
        now = datetime(2026, 8, 18, 9, 32, 30, tzinfo=scan.ET)
        wait = scan.seconds_until_target(now)
        self.assertEqual(wait, 0)


class ShouldRunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._orig_data_dir = scan.DATA_DIR
        scan.DATA_DIR = Path(self._tmp.name)

    def tearDown(self):
        scan.DATA_DIR = self._orig_data_dir
        self._tmp.cleanup()

    def _write_latest(self, session_date: str) -> None:
        (scan.DATA_DIR / "latest.json").write_text(
            json.dumps({"session_date": session_date})
        )

    def test_weekend_exits(self):
        now = datetime(2026, 8, 15, 9, 40, 0, tzinfo=scan.ET)  # Saturday
        self.assertFalse(scan.should_run(now, []))

    def test_holiday_exits(self):
        now = datetime(2026, 9, 7, 9, 40, 0, tzinfo=scan.ET)  # Labor Day 2026
        self.assertFalse(scan.should_run(now, []))

    def test_before_guard_window_exits(self):
        now = datetime(2026, 8, 18, 8, 0, 0, tzinfo=scan.ET)
        self.assertFalse(scan.should_run(now, []))

    def test_after_guard_window_exits(self):
        now = datetime(2026, 8, 18, 14, 30, 0, tzinfo=scan.ET)
        self.assertFalse(scan.should_run(now, []))

    def test_within_guard_window_and_no_prior_data_proceeds(self):
        now = datetime(2026, 8, 18, 9, 40, 0, tzinfo=scan.ET)
        self.assertTrue(scan.should_run(now, []))

    def test_late_start_within_window_still_proceeds(self):
        # Regression for the reported bug: 10:13 and 11:12 ET starts against
        # a cron set for 09:52 must no longer be rejected outright — they're
        # still inside the wide guard window.
        now = datetime(2026, 8, 18, 11, 12, 0, tzinfo=scan.ET)
        self.assertTrue(scan.should_run(now, []))

    def test_idempotent_when_latest_json_matches_today(self):
        now = datetime(2026, 8, 18, 9, 40, 0, tzinfo=scan.ET)
        self._write_latest("2026-08-18")
        self.assertFalse(scan.should_run(now, []))

    def test_proceeds_when_latest_json_is_stale(self):
        now = datetime(2026, 8, 18, 9, 40, 0, tzinfo=scan.ET)
        self._write_latest("2026-08-14")
        self.assertTrue(scan.should_run(now, []))

    def test_non_dict_json_is_treated_as_no_prior_data(self):
        # Regression: a corrupted/malformed-but-valid-JSON output file (e.g.
        # a bare list) must not crash should_run() -- treat it as "no prior
        # data" and proceed.
        now = datetime(2026, 8, 18, 9, 40, 0, tzinfo=scan.ET)
        (scan.DATA_DIR / "latest.json").write_text(json.dumps(["not", "a", "dict"]))
        self.assertTrue(scan.should_run(now, []))


class ModeOutputPathTests(unittest.TestCase):
    def test_fast_mode_writes_latest_json(self):
        self.assertEqual(scan.mode_output_path("fast"), scan.DATA_DIR / "latest.json")

    def test_range_mode_writes_range_json(self):
        self.assertEqual(scan.mode_output_path("range"), scan.DATA_DIR / "range.json")


class SafetyCapCoversBackupCronsTests(unittest.TestCase):
    """Regression for a real bug caught in review: the backup crons
    (.github/workflows/scan.yml) legitimately compute long waits against
    their mode's target. MAX_SLEEP_SECONDS must stay above both, or the
    backup fails itself with the safety-cap error every day it's needed."""

    def test_fast_backup_cron_wait_is_within_cap(self):
        # 08:40 ET (EDT) backup, per scan.yml, against the 09:32:30 target.
        now = datetime(2026, 8, 18, 8, 40, 0, tzinfo=scan.ET)
        wait = scan.seconds_until_target(now, scan.MODE_TARGET_TIME["fast"])
        self.assertLessEqual(wait, scan.MAX_SLEEP_SECONDS)

    def test_range_backup_cron_wait_is_within_cap(self):
        # 09:15 ET (EDT) backup, per scan.yml, against the 10:02:00 target.
        now = datetime(2026, 8, 18, 9, 15, 0, tzinfo=scan.ET)
        wait = scan.seconds_until_target(now, scan.MODE_TARGET_TIME["range"])
        self.assertLessEqual(wait, scan.MAX_SLEEP_SECONDS)

    def test_range_job_woken_by_wrong_dst_cron_is_within_cap(self):
        # Every job fires on every schedule entry (no per-job cron
        # filtering), so the range job also receives the "range primary
        # (EDT)" cron. During EST season that cron fires at 08:35 ET --
        # 87 minutes before the 10:02:00 range target. Routine each winter,
        # not broken logic, so the cap must clear it.
        now = datetime(2026, 1, 15, 8, 35, 0, tzinfo=scan.ET)  # EST season
        wait = scan.seconds_until_target(now, scan.MODE_TARGET_TIME["range"])
        self.assertAlmostEqual(wait, 87 * 60, delta=1)
        self.assertLessEqual(wait, scan.MAX_SLEEP_SECONDS)


class ModeTargetTimeTests(unittest.TestCase):
    def test_fast_target_is_09_32_30(self):
        now = datetime(2026, 8, 18, 9, 0, 0, tzinfo=scan.ET)
        wait = scan.seconds_until_target(now, scan.MODE_TARGET_TIME["fast"])
        self.assertAlmostEqual(wait, 32 * 60 + 30, delta=1)

    def test_range_target_is_10_02_00(self):
        now = datetime(2026, 8, 18, 9, 0, 0, tzinfo=scan.ET)
        wait = scan.seconds_until_target(now, scan.MODE_TARGET_TIME["range"])
        self.assertAlmostEqual(wait, 62 * 60, delta=1)


class PerModeIdempotencyTests(unittest.TestCase):
    """A completed fast run must never block range from proceeding, and
    vice versa — each mode's idempotency check reads only its own file."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._orig_data_dir = scan.DATA_DIR
        scan.DATA_DIR = Path(self._tmp.name)

    def tearDown(self):
        scan.DATA_DIR = self._orig_data_dir
        self._tmp.cleanup()

    def test_fast_completed_today_does_not_block_range(self):
        now = datetime(2026, 8, 18, 10, 0, 0, tzinfo=scan.ET)
        (scan.DATA_DIR / "latest.json").write_text(
            json.dumps({"session_date": "2026-08-18"}))
        self.assertTrue(scan.should_run(now, [], mode="range"))

    def test_range_completed_today_does_not_block_fast(self):
        now = datetime(2026, 8, 18, 9, 40, 0, tzinfo=scan.ET)
        (scan.DATA_DIR / "range.json").write_text(
            json.dumps({"session_date": "2026-08-18"}))
        self.assertTrue(scan.should_run(now, [], mode="fast"))

    def test_range_completed_today_blocks_range(self):
        now = datetime(2026, 8, 18, 10, 0, 0, tzinfo=scan.ET)
        (scan.DATA_DIR / "range.json").write_text(
            json.dumps({"session_date": "2026-08-18"}))
        self.assertFalse(scan.should_run(now, [], mode="range"))


class IsReversalTests(unittest.TestCase):
    """is_reversal() is driven by move_since_alert (09:32:30 -> close, or
    the move_late fallback), at a 2% threshold (REVERSAL_THRESHOLD)."""

    def test_gap_up_then_alert_move_reverses_past_threshold(self):
        self.assertTrue(scan.is_reversal(move=-0.03, gap_pct=0.05))

    def test_gap_down_then_alert_move_reverses_past_threshold(self):
        self.assertTrue(scan.is_reversal(move=0.025, gap_pct=-0.05))

    def test_gap_up_then_alert_move_keeps_drifting_up_is_not_reversal(self):
        self.assertFalse(scan.is_reversal(move=0.03, gap_pct=0.05))

    def test_opposite_sign_below_threshold_is_noise_not_reversal(self):
        # 2% floor: a small opposite-sign wiggle must not be flagged.
        self.assertFalse(scan.is_reversal(move=-0.01, gap_pct=0.05))

    def test_missing_move_is_not_reversal(self):
        self.assertFalse(scan.is_reversal(move=None, gap_pct=0.05))


class AnalyseModeTests(unittest.TestCase):
    def setUp(self):
        self.prior = date(2026, 8, 17)
        self.session = date(2026, 8, 18)
        self.meta = {"ticker": "TEST", "name": "Test Corp"}

    def _prior_bar(self):
        return _bar(self.prior, "15:55:00", 100, 100.5, 99.5, 100.0)

    def test_fast_mode_has_no_segment_fields(self):
        bars = [self._prior_bar(),
                _bar(self.session, "09:30:00", 105, 106, 104.8, 105.5)]
        row = scan.analyse(self.meta, bars, self.session, self.prior)
        self.assertIsNotNone(row)
        self.assertNotIn("price_0932", row)
        self.assertNotIn("move_since_alert", row)
        self.assertNotIn("price_0945", row)
        self.assertNotIn("reversal", row)
        self.assertNotIn("reversal_basis", row)

    def test_range_mode_computes_segments_and_flags_reversal(self):
        bars = [
            self._prior_bar(),
            _bar(self.session, "09:30:00", 105, 106, 104.8, 105.2),  # gap ~+5%
            _bar(self.session, "09:32:00", 105.2, 105.6, 105.1, 105.5),  # pre-09:32:30
            _bar(self.session, "09:40:00", 105.5, 106.2, 105.3, 106),  # pre-09:45
            _bar(self.session, "09:50:00", 106, 106, 101.8, 102),  # post-09:45
            _bar(self.session, "09:59:00", 102, 102.2, 100.8, 101),  # final close
        ]
        row = scan.analyse(self.meta, bars, self.session, self.prior, mode="range")
        self.assertIsNotNone(row)
        self.assertEqual(row["price_0932"], 105.5)
        self.assertAlmostEqual(row["move_since_alert"], (101 / 105.5) - 1, places=5)
        self.assertEqual(row["price_0945"], 106.0)
        self.assertAlmostEqual(row["move_early"], (106 / 105) - 1, places=5)
        self.assertAlmostEqual(row["move_late"], (101 / 106) - 1, places=5)
        # move_since_alert = -0.0427 (opposite sign to +5% gap, past the 2%
        # threshold) -> reversal True, driven by move_since_alert.
        self.assertTrue(row["reversal"])
        self.assertEqual(row["reversal_basis"], "since_alert")

    def test_reversal_is_driven_by_move_since_alert_not_move_late(self):
        # Constructed so move_late alone WOULD flag a reversal (big opposite
        # move from 09:45), but move_since_alert (09:32:30 -> close) stays
        # under the 2% threshold -> must NOT flag. price_0932 exists here,
        # so since_alert takes priority over the move_late fallback.
        bars = [
            self._prior_bar(),
            _bar(self.session, "09:30:00", 105, 105, 103, 103),  # gap ~+5%, dips
            _bar(self.session, "09:32:00", 103, 103, 99.5, 100),  # pre-09:32:30 alert snapshot
            _bar(self.session, "09:40:00", 100, 108.5, 100, 108),  # pre-09:45, rallies
            _bar(self.session, "09:50:00", 108, 108, 99, 99),
            _bar(self.session, "09:59:00", 99, 100.5, 99, 100.2),  # final close
        ]
        row = scan.analyse(self.meta, bars, self.session, self.prior, mode="range")
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row["move_since_alert"], (100.2 / 100) - 1, places=5)
        self.assertAlmostEqual(row["move_late"], (100.2 / 108) - 1, places=5)
        self.assertFalse(row["reversal"])  # move_since_alert ~0.2%, under 2%
        self.assertEqual(row["reversal_basis"], "since_alert")

    def test_range_mode_with_no_bars_before_945_leaves_all_segments_null(self):
        bars = [
            self._prior_bar(),
            _bar(self.session, "09:50:00", 105, 105, 104, 104.5),
            _bar(self.session, "09:55:00", 104.5, 104.5, 104, 104),
        ]
        row = scan.analyse(self.meta, bars, self.session, self.prior, mode="range")
        self.assertIsNotNone(row)
        self.assertIsNone(row["price_0932"])
        self.assertIsNone(row["move_since_alert"])
        self.assertIsNone(row["price_0945"])
        self.assertIsNone(row["move_early"])
        self.assertIsNone(row["move_late"])
        self.assertFalse(row["reversal"])
        self.assertIsNone(row["reversal_basis"])

    def test_range_mode_bar_before_945_but_after_0932_leaves_only_alert_segment_null(self):
        # A bar at 09:40 qualifies for the 09:45 split but not the earlier
        # 09:32:30 split -> the two segments are independent. move_late here
        # is small (under 2%), so the fallback doesn't fire -- a separate
        # test below covers the fallback actually triggering.
        bars = [
            self._prior_bar(),
            _bar(self.session, "09:40:00", 105, 106.2, 104.8, 106),
            _bar(self.session, "09:50:00", 106, 106, 103.9, 104),
            _bar(self.session, "09:59:00", 104, 104.9, 103.6, 104.7),
        ]
        row = scan.analyse(self.meta, bars, self.session, self.prior, mode="range")
        self.assertIsNotNone(row)
        self.assertIsNone(row["price_0932"])
        self.assertIsNone(row["move_since_alert"])
        self.assertEqual(row["price_0945"], 106.0)
        self.assertIsNotNone(row["move_early"])
        self.assertFalse(row["reversal"])
        self.assertEqual(row["reversal_basis"], "late")

    def test_reversal_fallback_to_move_late_triggers_when_price_0932_null(self):
        # No bar before 09:32:30 (price_0932 null) but one before 09:45
        # (price_0945 exists) -> reversal must fall back to move_late, and
        # here it's a clear, threshold-clearing reversal.
        bars = [
            self._prior_bar(),
            _bar(self.session, "09:35:00", 105, 105.5, 104.8, 105.3),  # gap ~+5%
            _bar(self.session, "09:50:00", 105, 105, 99, 99),
            _bar(self.session, "09:59:00", 99, 99.2, 96, 97),  # final close
        ]
        row = scan.analyse(self.meta, bars, self.session, self.prior, mode="range")
        self.assertIsNotNone(row)
        self.assertIsNone(row["price_0932"])
        self.assertIsNone(row["move_since_alert"])
        self.assertEqual(row["price_0945"], 105.3)
        self.assertAlmostEqual(row["move_late"], (97 / 105.3) - 1, places=5)
        self.assertTrue(row["reversal"])
        self.assertEqual(row["reversal_basis"], "late")

    def test_range_mode_captures_full_09_30_to_10_00_window(self):
        # A bar just before 10:00 must still count; the fast-mode 09:50 cutoff
        # must not apply in range mode.
        bars = [
            self._prior_bar(),
            _bar(self.session, "09:30:00", 105, 106, 104.8, 105.5),
            _bar(self.session, "09:59:30", 105, 105.2, 104.9, 105.1),
        ]
        row = scan.analyse(self.meta, bars, self.session, self.prior, mode="range")
        self.assertIsNotNone(row)
        self.assertEqual(row["or_close"], 105.1)


if __name__ == "__main__":
    unittest.main()
