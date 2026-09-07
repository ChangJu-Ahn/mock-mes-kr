"""Unit tests for the seed scheduler (mes_core.schedule)."""

import os
import random
import unittest
from datetime import datetime, timedelta, timezone

from mes_core import schedule


class _Run:
    """Minimal stand-in for a planned process result."""

    def __init__(self, lot_key, step_code, eqp_id):
        self.lot_key = lot_key
        self.step_code = step_code
        self.eqp_id = eqp_id
        self.in_time = ""
        self.out_time = ""


ANCHOR = datetime(2026, 9, 4, 0, 0, 0, tzinfo=timezone.utc)


def _parse(s):
    return datetime.fromisoformat(s)


class ResolveAnchorTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("MES_ANCHOR", None)

    def tearDown(self):
        os.environ.pop("MES_ANCHOR", None)

    def test_absent_env_returns_utc_now(self):
        before = datetime.now(timezone.utc)
        got = schedule.resolve_anchor()
        after = datetime.now(timezone.utc)
        self.assertEqual(got.tzinfo, timezone.utc)
        self.assertEqual(got.microsecond, 0)
        self.assertLessEqual(before.replace(microsecond=0), got)
        self.assertLessEqual(got, after)

    def test_parses_offset_form(self):
        os.environ["MES_ANCHOR"] = "2026-09-04T00:00:00+00:00"
        self.assertEqual(schedule.resolve_anchor(), ANCHOR)

    def test_parses_z_suffix(self):
        os.environ["MES_ANCHOR"] = "2026-09-04T00:00:00Z"
        self.assertEqual(schedule.resolve_anchor(), ANCHOR)

    def test_naive_value_is_treated_as_utc(self):
        os.environ["MES_ANCHOR"] = "2026-09-04T00:00:00"
        self.assertEqual(schedule.resolve_anchor(), ANCHOR)

    def test_non_utc_offset_is_converted(self):
        os.environ["MES_ANCHOR"] = "2026-09-04T09:00:00+09:00"
        self.assertEqual(schedule.resolve_anchor(), ANCHOR)

    def test_blank_env_falls_back_to_now(self):
        os.environ["MES_ANCHOR"] = "   "
        self.assertEqual(schedule.resolve_anchor().tzinfo, timezone.utc)

    def test_garbage_raises(self):
        os.environ["MES_ANCHOR"] = "not-a-timestamp"
        with self.assertRaises(ValueError):
            schedule.resolve_anchor()


class IsoTests(unittest.TestCase):
    def test_matches_db_now_iso_format(self):
        self.assertEqual(schedule.iso(ANCHOR), "2026-09-04T00:00:00+00:00")

    def test_drops_microseconds(self):
        dt = ANCHOR.replace(microsecond=123456)
        self.assertEqual(schedule.iso(dt), "2026-09-04T00:00:00+00:00")


class AssignTimesTests(unittest.TestCase):
    def _runs(self):
        """Two lots, three steps each, contending for one etcher."""
        return [
            _Run(1, "DIFF", "EQP-DIFF01"),
            _Run(1, "ETCH", "EQP-ETCH01"),
            _Run(1, "METRO", None),
            _Run(2, "DIFF", "EQP-DIFF01"),
            _Run(2, "ETCH", "EQP-ETCH01"),
            _Run(2, "METRO", None),
        ]

    def test_last_run_ends_exactly_at_anchor(self):
        runs = self._runs()
        schedule.assign_times(runs, ANCHOR, random.Random(schedule.SCHEDULE_SEED))
        self.assertEqual(max(_parse(r.out_time) for r in runs), ANCHOR)

    def test_every_run_starts_before_it_ends(self):
        runs = self._runs()
        schedule.assign_times(runs, ANCHOR, random.Random(schedule.SCHEDULE_SEED))
        for r in runs:
            self.assertLess(_parse(r.in_time), _parse(r.out_time))

    def test_no_run_is_in_the_future(self):
        runs = self._runs()
        schedule.assign_times(runs, ANCHOR, random.Random(schedule.SCHEDULE_SEED))
        for r in runs:
            self.assertLessEqual(_parse(r.out_time), ANCHOR)

    def test_steps_within_a_lot_do_not_overlap(self):
        runs = self._runs()
        schedule.assign_times(runs, ANCHOR, random.Random(schedule.SCHEDULE_SEED))
        for lot in (1, 2):
            seq = [r for r in runs if r.lot_key == lot]
            for prev, cur in zip(seq, seq[1:]):
                self.assertLessEqual(_parse(prev.out_time), _parse(cur.in_time))

    def test_same_equipment_never_runs_two_lots_at_once(self):
        runs = self._runs()
        schedule.assign_times(runs, ANCHOR, random.Random(schedule.SCHEDULE_SEED))
        by_eqp = {}
        for r in runs:
            if r.eqp_id:
                by_eqp.setdefault(r.eqp_id, []).append(r)
        for eqp, rs in by_eqp.items():
            rs.sort(key=lambda r: _parse(r.in_time))
            for prev, cur in zip(rs, rs[1:]):
                self.assertLessEqual(
                    _parse(prev.out_time), _parse(cur.in_time),
                    f"{eqp} overlaps",
                )

    def test_equipmentless_steps_may_run_concurrently(self):
        """METRO has no equipment, so it is bounded only by its own lot."""
        runs = self._runs()
        schedule.assign_times(runs, ANCHOR, random.Random(schedule.SCHEDULE_SEED))
        metro = [r for r in runs if r.step_code == "METRO"]
        self.assertEqual(len(metro), 2)

    def test_returns_release_time_per_lot(self):
        runs = self._runs()
        rel = schedule.assign_times(runs, ANCHOR, random.Random(schedule.SCHEDULE_SEED))
        self.assertEqual(set(rel), {1, 2})
        for lot in (1, 2):
            first = min(_parse(r.in_time) for r in runs if r.lot_key == lot)
            self.assertLessEqual(rel[lot], first)

    def test_lots_are_released_at_the_configured_interval(self):
        runs = self._runs()
        rel = schedule.assign_times(runs, ANCHOR, random.Random(schedule.SCHEDULE_SEED))
        self.assertEqual(
            rel[2] - rel[1], timedelta(minutes=schedule.RELEASE_INTERVAL_MIN)
        )

    def test_is_deterministic(self):
        a, b = self._runs(), self._runs()
        schedule.assign_times(a, ANCHOR, random.Random(schedule.SCHEDULE_SEED))
        schedule.assign_times(b, ANCHOR, random.Random(schedule.SCHEDULE_SEED))
        self.assertEqual(
            [(r.in_time, r.out_time) for r in a],
            [(r.in_time, r.out_time) for r in b],
        )

    def test_durations_stay_within_the_configured_band(self):
        runs = self._runs()
        schedule.assign_times(runs, ANCHOR, random.Random(schedule.SCHEDULE_SEED))
        for r in runs:
            lo, hi = schedule.STEP_DURATION_MIN[r.step_code]
            mins = (_parse(r.out_time) - _parse(r.in_time)).total_seconds() / 60
            self.assertGreaterEqual(mins, lo)
            self.assertLessEqual(mins, hi)

    def test_empty_input_returns_empty_mapping(self):
        self.assertEqual(
            schedule.assign_times([], ANCHOR, random.Random(1)), {}
        )


if __name__ == "__main__":
    unittest.main()
