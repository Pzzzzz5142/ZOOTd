from datetime import datetime
import unittest

from maa_planner.supplies import regular_stage_availability


class SuppliesCalendarTests(unittest.TestCase):
    def check(self, when, event=None):
        if event is None:
            event = {"IsResourceCollection": False}
        return regular_stage_availability(
            "AP-5", datetime.fromisoformat(when),
            {"Official": {"resourceCollection": event}}, "Official")

    def test_four_am_reset_and_weekly_schedule(self):
        self.assertEqual(self.check("2026-09-12T03:59:59+08:00"), "closed")
        self.assertEqual(self.check("2026-09-12T04:00:00+08:00"), "open")
        for day, expected in ((7, "open"), (8, "closed"), (9, "closed"),
                              (10, "open"), (11, "closed"), (12, "open"), (13, "open")):
            self.assertEqual(self.check(f"2026-09-{day:02}T12:00:00+08:00"), expected)
        self.assertEqual(self.check("2026-09-11T20:00:00+00:00"), "open")

    def test_all_open_event_uses_wall_clock_and_inclusive_last_second(self):
        event = {"IsResourceCollection": True, "TimeZone": 8,
                 "UtcStartTime": "2026/09/11 16:00:00",
                 "UtcExpireTime": "2026/09/12 03:59:59"}
        self.assertEqual(self.check("2026-09-11T15:59:59+08:00", event), "closed")
        self.assertEqual(self.check("2026-09-11T16:00:00+08:00", event), "open")
        self.assertEqual(self.check("2026-09-12T03:59:59+08:00", event), "open")
        self.assertEqual(self.check("2026-09-18T12:00:00+08:00", event), "closed")

    def test_missing_or_invalid_calendar_keeps_screen_fallback(self):
        now = datetime.fromisoformat("2026-09-11T12:00:00+08:00")
        for payload in (None, {}, {"Official": {"resourceCollection": {}}}):
            self.assertEqual(regular_stage_availability("AP-5", now, payload, "Official"), "unknown")
        self.assertEqual(regular_stage_availability("1-7", now, None, "Official"), "open")
        self.assertEqual(regular_stage_availability("AP-5", now, None, "YoStarEN"), "unknown")


if __name__ == "__main__":
    unittest.main()
