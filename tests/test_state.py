import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hc.model import CRITICAL, WARNING, Result  # noqa: E402
from hc.state import update_and_decide  # noqa: E402

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
SIX_H = timedelta(hours=6)


def fresh():
    return {"checks": {}, "alerted": [], "last_alert_at": None}


def run(state, now, *results):
    return update_and_decide(state, list(results), now, SIX_H)


def bad(name, sev=CRITICAL):
    return Result(name, False, "boom", severity=sev)


def good(name):
    return Result(name, True)


class DecideTests(unittest.TestCase):
    def test_all_green_is_silent(self):
        self.assertEqual(run(fresh(), T0, good("a")).kind, "none")

    def test_first_failure_alerts_once_then_stays_quiet(self):
        s = fresh()
        self.assertEqual(run(s, T0, bad("a")).kind, "alert")
        self.assertEqual(run(s, T0 + timedelta(hours=1), bad("a")).kind, "none")
        self.assertEqual(run(s, T0 + timedelta(hours=2), bad("a")).kind, "none")

    def test_reminder_after_repeat_window(self):
        s = fresh()
        run(s, T0, bad("a"))
        self.assertEqual(run(s, T0 + timedelta(hours=6), bad("a")).kind, "reminder")
        self.assertEqual(run(s, T0 + timedelta(hours=7), bad("a")).kind, "none")

    def test_new_failure_alerts_immediately_even_inside_window(self):
        s = fresh()
        run(s, T0, bad("a"))
        d = run(s, T0 + timedelta(hours=1), bad("a"), bad("b"))
        self.assertEqual((d.kind, d.new), ("alert", ["b"]))

    def test_recovery_sent_once(self):
        s = fresh()
        run(s, T0, bad("a"))
        d = run(s, T0 + timedelta(hours=1), good("a"))
        self.assertEqual((d.kind, d.resolved, d.has_critical), ("recovery", ["a"], True))
        self.assertEqual(run(s, T0 + timedelta(hours=2), good("a")).kind, "none")

    def test_warning_only_alert_does_not_make_recovery_critical(self):
        s = fresh()
        run(s, T0, bad("a", WARNING))
        self.assertFalse(run(s, T0 + timedelta(hours=1), good("a")).has_critical)

    def test_partially_recovered_then_refail_counts_as_new(self):
        s = fresh()
        run(s, T0, bad("a"), bad("b"))
        self.assertEqual(run(s, T0 + timedelta(hours=1), good("a"), bad("b")).kind, "none")
        d = run(s, T0 + timedelta(hours=2), bad("a"), bad("b"))
        self.assertEqual((d.kind, d.new), ("alert", ["a"]))

    def test_skipped_checks_do_not_clear_or_create_failures(self):
        s = fresh()
        run(s, T0, bad("a"))
        skipped = Result("a", True, skipped=True)
        d = run(s, T0 + timedelta(hours=1), skipped)
        self.assertEqual(d.kind, "none")
        self.assertIn("a", s["checks"])

    def test_failing_since_survives_across_runs(self):
        s = fresh()
        run(s, T0, bad("a"))
        since = s["checks"]["a"]["since"]
        run(s, T0 + timedelta(hours=1), bad("a"))
        self.assertEqual(s["checks"]["a"]["since"], since)
        self.assertEqual(s["checks"]["a"]["runs"], 2)


if __name__ == "__main__":
    unittest.main()
