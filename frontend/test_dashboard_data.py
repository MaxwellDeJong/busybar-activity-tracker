"""Unit tests for the dashboard data pipeline.

Run with:  python3 -m unittest test_dashboard_data      (or: python3 test_dashboard_data.py)

TZ is pinned to UTC so `logical_day` assertions (which depend on the *local*
interpretation of the UTC timestamps) are deterministic on any machine.
"""
import datetime as dt
import os
import time
import unittest

# Pin the local timezone to UTC before dashboard_data converts any timestamps.
os.environ["TZ"] = "UTC"
time.tzset()

import dashboard_data as dd  # noqa: E402


# A small, hand-checkable fixture log. Times are UTC == local (TZ=UTC), so:
#   * anything at/after 03:00 belongs to that calendar day,
#   * the 02:30 session belongs to the *previous* logical day (3 AM cutoff).
FIXTURE_LINES = [
    '{"activity":"development","phase":"focus","start":"2026-07-21T16:00:00Z","duration_s":120}',
    '{"activity":"development","phase":"focus","start":"2026-07-21T17:00:00Z","duration_s":180}',
    '{"activity":"work","phase":"work","start":"2026-07-21T18:00:00Z","duration_s":300}',
    '{"activity":"work","phase":"rest","start":"2026-07-21T18:05:00Z","duration_s":600}',   # dropped: rest
    '{"activity":"exercise","phase":"focus","start":"2026-07-21T15:00:00Z","duration_s":30}',  # dropped: <60s
    '{"activity":"reading_night","phase":"focus","start":"2026-07-21T02:30:00Z","duration_s":600}',  # -> 7/20
    '{ not valid json',   # malformed: skipped with a warning
    '',                    # blank: skipped
]

D21 = dt.date(2026, 7, 21)
D20 = dt.date(2026, 7, 20)
D22 = dt.date(2026, 7, 22)


def write_fixture(tmpdir):
    path = os.path.join(tmpdir, "fixture.jsonl")
    with open(path, "w") as fh:
        fh.write("\n".join(FIXTURE_LINES) + "\n")
    return path


class HelperTests(unittest.TestCase):
    def test_parse_day_arg(self):
        self.assertEqual(dd.parse_day_arg("7/21/26"), D21)
        self.assertEqual(dd.parse_day_arg("7/21/2026"), D21)
        self.assertEqual(dd.parse_day_arg(" 1/2/26 "), dt.date(2026, 1, 2))
        with self.assertRaises(ValueError):
            dd.parse_day_arg("2026-07-21")

    def test_logical_day_cutoff(self):
        # 02:59 local belongs to the previous day; 03:00 to the current day.
        before = dt.datetime(2026, 7, 21, 2, 59)
        after = dt.datetime(2026, 7, 21, 3, 0)
        self.assertEqual(dd.logical_day(before), D20)
        self.assertEqual(dd.logical_day(after), D21)

    def test_fmt_duration(self):
        self.assertEqual(dd.fmt_duration(0), "0s")
        self.assertEqual(dd.fmt_duration(59), "59s")
        self.assertEqual(dd.fmt_duration(90), "1m30s")
        self.assertEqual(dd.fmt_duration(3661), "1h1m1s")


class LoadTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.path = write_fixture(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_records_skips_malformed_and_blank(self):
        records = dd.load_records(self.path)
        self.assertEqual(len(records), 6)  # 8 lines - 1 malformed - 1 blank

    def test_load_records_derived_fields(self):
        records = dd.load_records(self.path)
        first = records[0]
        self.assertIsInstance(first["start_local"], dt.datetime)
        self.assertIsInstance(first["duration_s"], float)
        self.assertEqual(first["start_local"].hour, 16)  # UTC == local here

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            dd.load_records("/no/such/log.jsonl")


class PipelineTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.path = write_fixture(self.tmp.name)
        self.df = dd.load_prepared(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_prepare_filters_rest_and_short(self):
        # 6 loaded - 1 rest - 1 sub-minute = 4 rows
        self.assertEqual(len(self.df), 4)
        self.assertNotIn("rest", set(self.df["phase"]))
        self.assertTrue((self.df["duration_s"] >= dd.MIN_DURATION_S).all())

    def test_prepare_derived_columns(self):
        self.assertIn("duration_min", self.df.columns)
        self.assertIn("logical_day", self.df.columns)
        row = self.df[self.df["duration_s"] == 120.0].iloc[0]
        self.assertAlmostEqual(row["duration_min"], 2.0)

    def test_logical_day_assignment(self):
        days = set(self.df["logical_day"])
        self.assertEqual(days, {D20, D21})  # 02:30 session lands on 7/20

    def test_daily_by_activity(self):
        pivot = dd.daily_by_activity(self.df)
        self.assertAlmostEqual(pivot.loc[D21, "development"], 5.0)  # (120+180)/60
        self.assertAlmostEqual(pivot.loc[D21, "work"], 5.0)         # 300/60
        self.assertAlmostEqual(pivot.loc[D20, "reading_night"], 10.0)
        # gap cells are zero, not NaN
        self.assertAlmostEqual(pivot.loc[D20, "development"], 0.0)

    def test_daily_productive(self):
        prod = dd.daily_productive(self.df)
        self.assertAlmostEqual(prod.loc[D21], 10.0)  # 5 + 5
        self.assertAlmostEqual(prod.loc[D20], 10.0)  # reading_night

    def test_day_sessions_sorted_and_filtered(self):
        sessions = dd.day_sessions(self.df, D21)
        self.assertEqual(len(sessions), 3)
        starts = list(sessions["start_local"])
        self.assertEqual(starts, sorted(starts))
        # accepts a string day argument too
        self.assertEqual(len(dd.day_sessions(self.df, "7/21/26")), 3)

    def test_range_by_activity_reindexes_gaps(self):
        rng = dd.range_by_activity(self.df, D20, D22)
        self.assertEqual(list(rng.index), [D20, D21, D22])  # 7/22 present though empty
        self.assertAlmostEqual(rng.loc[D22].sum(), 0.0)     # gap day all zeros
        self.assertAlmostEqual(rng.loc[D21, "work"], 5.0)

    def test_week_bounds(self):
        # 7/21/26 is a Tuesday -> Mon 7/20 .. Sun 7/26.
        self.assertEqual(dd.week_bounds(D21), (D20, dt.date(2026, 7, 26)))
        self.assertEqual(dd.week_bounds(D20)[0], D20)          # Monday maps to itself
        self.assertEqual(dd.week_bounds("7/26/26"), (D20, dt.date(2026, 7, 26)))
        mon, sun = dd.week_bounds(D21)
        self.assertEqual((sun - mon).days, 6)

    def test_month_bounds(self):
        self.assertEqual(dd.month_bounds(D21), (dt.date(2026, 7, 1), dt.date(2026, 7, 31)))
        # December rolls the year over when finding the next month.
        self.assertEqual(dd.month_bounds(dt.date(2026, 12, 15)),
                         (dt.date(2026, 12, 1), dt.date(2026, 12, 31)))
        # February 2028 is a leap year -> 29 days.
        self.assertEqual(dd.month_bounds(dt.date(2028, 2, 10))[1], dt.date(2028, 2, 29))

    def test_range_sessions(self):
        wk = dd.range_sessions(self.df, D20, D22)
        self.assertEqual(len(wk), 4)                          # every kept session
        starts = list(wk["start_local"])
        self.assertEqual(starts, sorted(starts))              # sorted by start
        self.assertEqual(len(dd.range_sessions(self.df, D21, D21)), 3)  # single day
        self.assertTrue(dd.range_sessions(self.df, D22, D22).empty)     # empty day


class EmptyFrameTests(unittest.TestCase):
    def test_prepare_empty(self):
        import pandas as pd
        empty = dd.prepare(pd.DataFrame())
        self.assertEqual(len(empty), 0)
        for col in ("activity", "duration_min", "logical_day"):
            self.assertIn(col, empty.columns)

    def test_aggregations_empty(self):
        import pandas as pd
        empty = dd.prepare(pd.DataFrame())
        self.assertTrue(dd.daily_by_activity(empty).empty)
        self.assertTrue(dd.daily_productive(empty).empty)
        self.assertTrue(dd.day_sessions(empty, D21).empty)


class RealLogSanityTests(unittest.TestCase):
    """Cross-check the pipeline against the committed activity_summary numbers
    for 7/21/26: productive total = sum of all non-rest sessions >= 60s."""

    def test_matches_cli_productive_total(self):
        if not os.path.exists(dd.DEFAULT_LOG):
            self.skipTest("no real activity_log.jsonl present")
        df = dd.load_prepared(dd.DEFAULT_LOG)
        prod = dd.daily_productive(df)
        if D21 not in prod.index:
            self.skipTest("no 7/21/26 data in the real log")
        # Independently recompute from raw records: non-rest, >=60s, logical day 7/21.
        raw = dd.load_records(dd.DEFAULT_LOG)
        expect_min = sum(
            r["duration_s"] / 60.0 for r in raw
            if r.get("phase") != "rest"
            and r["duration_s"] >= dd.MIN_DURATION_S
            and dd.logical_day(r["start_local"]) == D21
        )
        self.assertAlmostEqual(prod.loc[D21], expect_min, places=6)


class PeriodViewTests(unittest.TestCase):
    """Week / Month / Heatmap shaping. Fixture: 7/20 (Mon) has 10m, 7/21 has
    dev 2m + 3m and work 5m; 'today' is Wed 7/22, so Thu..Sun are future."""

    def setUp(self):
        import tempfile
        import dashboard_theme as theme
        self.tmp = tempfile.TemporaryDirectory()
        self.df = dd.load_prepared(write_fixture(self.tmp.name))
        self.colors = theme.activity_colors(self.df["activity"].unique())

    def tearDown(self):
        self.tmp.cleanup()

    def period(self, start, end):
        import dashboard_period as period
        return period.summarize_period(dd.range_sessions(self.df, start, end), start, end,
                                       self.colors, D22)

    def heatmap(self, start, end, activity=None):
        import dashboard_period as period
        return period.summarize_heatmap(dd.range_sessions(self.df, start, end), start, end,
                                        self.colors, D22, activity=activity)

    def test_week_summary(self):
        s = self.period(D20, D20 + dt.timedelta(days=6))
        self.assertEqual(len(s["days"]), 7)
        self.assertAlmostEqual(s["total_s"], 1200.0)
        self.assertEqual((s["active_days"], s["elapsed_days"]), (2, 3))
        self.assertEqual(sum(d["future"] for d in s["days"]), 4)
        tue = s["days"][1]
        # Segments stack in alphabetical order with their palette colors.
        self.assertEqual([(a, v) for a, v, _ in tue["segments"]],
                         [("development", 300.0), ("work", 300.0)])
        self.assertEqual(tue["segments"][1][2], self.colors["work"])

    def test_week_html_only_past_days_drill(self):
        import dashboard_period as period
        s = self.period(D20, D20 + dt.timedelta(days=6))
        page = period.render_week_html(s, D22, "UTC")
        self.assertEqual(page.count("data-bb-day="), 3)
        self.assertEqual(page.count(" disabled "), 4)
        self.assertIn("This week", page)
        self.assertIn("July 20 – 26", page)

    def test_week_labels_across_years(self):
        import dashboard_period as period
        eyebrow, title = period.week_labels(dt.date(2025, 12, 29), dt.date(2026, 1, 10))
        self.assertEqual(eyebrow, "Last week")
        self.assertEqual(title, "Dec 29, 2025 – Jan 4, 2026")

    def test_month_calendar_pads_to_weekday(self):
        import dashboard_period as period
        first, last = dd.month_bounds(D21)
        s = self.period(first, last)
        page = period.render_month_html(s, D22, "UTC")
        # 7/1/2026 is a Wednesday: two blank cells before it.
        self.assertEqual(page.count('class="bb-cal-pad"'), first.weekday())
        self.assertEqual(page.count("bb-cal-day"), 31)
        self.assertEqual(page.count("data-bb-day="), 22)    # 7/1 .. 7/22 (today)
        self.assertIn("This month", page)

    def test_heatmap_levels_and_streaks(self):
        import dashboard_period as period
        s = self.heatmap(dt.date(2026, 7, 1), D22)
        self.assertEqual(s["values"], {D20: 600.0, D21: 600.0})
        self.assertEqual((s["active_days"], s["peak"]), (2, 600.0))
        # Today (7/22) is untracked so far; the streak still counts 7/20-7/21.
        self.assertEqual((s["streak"], s["longest"]), (2, 2))
        self.assertEqual(s["months"], [(2026, 7)])
        self.assertEqual([period.level(v, 600.0) for v in (0, 1, 150, 151, 600)],
                         [0, 1, 1, 2, 4])

    def test_heatmap_single_activity(self):
        s = self.heatmap(dt.date(2026, 7, 1), D22, activity="work")
        self.assertEqual(s["values"], {D21: 300.0})
        self.assertEqual(s["color"], self.colors["work"])
        # The breakdown still covers every activity in range.
        self.assertEqual(len(s["activities"]), 3)

    def test_heatmap_months_newest_first_across_years(self):
        s = self.heatmap(dt.date(2025, 11, 15), dt.date(2026, 2, 3))
        self.assertEqual(s["months"], [(2026, 2), (2026, 1), (2025, 12), (2025, 11)])

    def test_heatmap_html_out_of_range_days_are_inert(self):
        import dashboard_period as period
        start = D20
        s = self.heatmap(start, D22)
        page = period.render_heatmap_html(s, D22, "UTC", "Last 3 days", None)
        self.assertEqual(page.count("data-bb-day="), 3)     # 7/20 .. 7/22
        self.assertEqual(page.count("is-out"), 31 - 3)
        self.assertIn("lv-4", page)


class DayViewTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        import dashboard_theme as theme
        self.tmp = tempfile.TemporaryDirectory()
        self.df = dd.load_prepared(write_fixture(self.tmp.name))
        self.colors = theme.activity_colors(self.df["activity"].unique())

    def tearDown(self):
        self.tmp.cleanup()

    def summarize(self, day=D21, now=None):
        import dashboard_day as day_view
        return day_view.summarize_day(dd.day_sessions(self.df, day), day, self.colors, now=now)

    def test_summary_totals_and_breakdown(self):
        s = self.summarize()
        # dev 16:00 (2m) + 17:00 (3m), work 18:00 (5m).
        self.assertEqual(s["count"], 3)
        self.assertAlmostEqual(s["total_s"], 600.0)
        self.assertEqual([a["activity"] for a in s["activities"]], ["development", "work"])
        self.assertAlmostEqual(s["activities"][0]["share"], 0.5)
        self.assertEqual(s["activities"][0]["count"], 2)
        self.assertEqual(s["first_start"], dt.datetime(2026, 7, 21, 16, 0))
        self.assertEqual(s["last_end"], dt.datetime(2026, 7, 21, 18, 5))

    def test_items_interleave_gaps(self):
        kinds = [(it["kind"], round(it.get("seconds", 0) / 60)) for it in self.summarize()["items"]]
        # 16:02 -> 17:00 and 17:03 -> 18:00 are both breaks over MIN_GAP_S.
        self.assertEqual(kinds, [("session", 0), ("gap", 58), ("session", 0),
                                 ("gap", 57), ("session", 0)])

    def test_ribbon_zooms_to_worked_hours(self):
        s = self.summarize()
        # 16:00..19:00 is 3 h under the 6 h minimum: 1 h earlier, 2 h later.
        self.assertEqual(s["window"], (dt.datetime(2026, 7, 21, 15), dt.datetime(2026, 7, 21, 21)))
        self.assertEqual([t.hour for t in s["ticks"]], list(range(15, 22)))

    def test_ribbon_extends_to_now_and_clamps_to_day(self):
        import dashboard_day as day_view
        now = dt.datetime(2026, 7, 22, 1, 30)            # still logical 7/21
        s = self.summarize(now=now)
        self.assertEqual(s["window"][1], dt.datetime(2026, 7, 22, 2))
        # A session right after the 03:00 cutoff can't pull the window before it.
        lo, hi = day_view._ribbon_window(D21, dt.datetime(2026, 7, 21, 3, 10),
                                         dt.datetime(2026, 7, 21, 3, 40))
        self.assertEqual((lo, hi), (dt.datetime(2026, 7, 21, 3), dt.datetime(2026, 7, 21, 9)))

    def test_empty_day(self):
        import dashboard_day as day_view
        self.assertIsNone(self.summarize(day=D22))
        page = day_view.render_day_html(None, D22, D22, "UTC")
        self.assertIn("Nothing tracked", page)

    def test_html_escapes_and_prettifies_names(self):
        import pandas as pd
        import dashboard_day as day_view
        sessions = pd.DataFrame([{
            "activity": "<b>tech_reading</b>", "duration_s": 600.0,
            "start_local": dt.datetime(2026, 7, 21, 9, 0, tzinfo=dt.timezone.utc),
        }])
        s = day_view.summarize_day(sessions, D21, {})
        page = day_view.render_day_html(s, D21, D21, "UTC")
        self.assertNotIn("<b>", page)
        self.assertIn("&lt;b&gt;tech reading&lt;/b&gt;", page)
        self.assertEqual(day_view.pretty_name("tech_reading"), "Tech reading")

    def test_relative_labels(self):
        import dashboard_day as day_view
        today = dt.date(2026, 9, 30)
        label = lambda d: day_view._relative_label(d, today)   # noqa: E731
        self.assertEqual(label(today), "Today")
        self.assertEqual(label(dt.date(2026, 9, 29)), "Yesterday")
        self.assertEqual(label(dt.date(2026, 9, 26)), "4 days ago")
        self.assertEqual(label(dt.date(2026, 9, 21)), "Last week")
        self.assertEqual(label(dt.date(2026, 9, 2)), "4 weeks ago")
        self.assertEqual(label(dt.date(2026, 1, 5)), "8 months ago")


class LiveSessionTests(unittest.TestCase):
    """The recorder's open session (recorder_state.json) as the Day view sees it."""

    NOW = dt.datetime(2026, 7, 21, 18, 30, tzinfo=dt.timezone.utc)

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "recorder_state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def write_state(self, open_=None, version=1, overtime_ms=None, start=None):
        import json
        start = start or dt.datetime(2026, 7, 21, 18, 10, tzinfo=dt.timezone.utc)
        if open_ is None:
            open_ = {"key": "work", "phase": "work", "index": 0, "card_id": "test-card",
                     "start_ms": int(start.timestamp() * 1000), "partial": False}
        with open(self.path, "w") as fh:
            json.dump({"open": open_, "overtime_start_ms": overtime_ms, "observed_stop": True,
                       "version": version, "last_ts_ms": 0, "saved_at": 0}, fh)

    def load(self):
        return dd.load_live_session(self.path, now=self.NOW)

    def test_open_session(self):
        self.write_state()
        live = self.load()
        self.assertEqual(live["activity"], "work")       # unmapped card -> recorder's key
        self.assertEqual(live["logical_day"], D21)
        self.assertAlmostEqual(live["elapsed_s"], 20 * 60)
        self.assertFalse(live["overtime"])

    def test_flow_overtime_still_counts(self):
        self.write_state(overtime_ms=1)
        self.assertTrue(self.load()["overtime"])

    def test_nothing_running(self):
        self.write_state(open_=False)
        self.assertIsNone(self.load())
        self.assertIsNone(dd.load_live_session(os.path.join(self.tmp.name, "missing.json")))

    def test_rest_unknown_version_and_stale_are_ignored(self):
        self.write_state(open_={"key": "work", "phase": "rest", "start_ms": 0})
        self.assertIsNone(self.load())
        self.write_state(version=2)
        self.assertIsNone(self.load())
        self.write_state(start=self.NOW - dt.timedelta(hours=30))
        self.assertIsNone(self.load())

    def test_day_view_includes_live_session(self):
        import tempfile
        import dashboard_day as day_view
        import dashboard_theme as theme
        with tempfile.TemporaryDirectory() as tmp:
            df = dd.load_prepared(write_fixture(tmp))
        colors = theme.activity_colors(df["activity"].unique())
        self.write_state()
        live = self.load()
        now = self.NOW.replace(tzinfo=None)
        s = day_view.summarize_day(dd.day_sessions(df, D21), D21, colors, now=now, live=live)
        # 10m logged + 20m running; the live row is last and ends at now.
        self.assertAlmostEqual(s["total_s"], 1800.0)
        self.assertEqual(s["count"], 4)
        self.assertTrue(s["items"][-1]["live"])
        self.assertEqual(s["last_end"], now)
        work = next(a for a in s["activities"] if a["activity"] == "work")
        self.assertAlmostEqual(work["seconds"], 1500.0)
        page = day_view.render_day_html(s, D21, D21, "UTC", now=now)
        self.assertIn("bb-live", page)
        self.assertIn("running since 18:10", page)
        self.assertIn("16:00 – now", page)

    def test_live_session_alone_fills_an_empty_day(self):
        import dashboard_day as day_view
        self.write_state()
        now = self.NOW.replace(tzinfo=None)
        s = day_view.summarize_day(None, D21, {}, now=now, live=self.load())
        self.assertEqual(s["count"], 1)
        self.assertAlmostEqual(s["total_s"], 1200.0)


if __name__ == "__main__":
    unittest.main()
