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


class HeatmapGridTests(unittest.TestCase):
    def test_calendar_grid_aligns_to_monday(self):
        import dashboard_viz as viz
        # 7/22/26 is a Wednesday -> grid starts on Monday 7/20; 7/20..8/2 = 2 weeks.
        grid_start, num_weeks = viz.calendar_grid(D22, dt.date(2026, 8, 2))
        self.assertEqual(grid_start, dt.date(2026, 7, 20))
        self.assertEqual(grid_start.weekday(), 0)
        self.assertEqual(num_weeks, 2)

    def test_date_from_cell_roundtrips(self):
        import dashboard_viz as viz
        start, end = D22, dt.date(2026, 9, 30)
        grid_start, num_weeks = viz.calendar_grid(start, end)
        # Every day in range maps to a (col, row) that inverts back to the same day.
        day = start
        while day <= end:
            col = (day - grid_start).days // 7
            row = day.weekday()
            self.assertEqual(viz.date_from_cell(grid_start, col, row), day)
            day += dt.timedelta(days=1)

    def test_build_heatmap_places_values(self):
        import pandas as pd
        import dashboard_viz as viz
        start, end = D20, dt.date(2026, 8, 2)   # Mon .. Sun, 2 weeks
        values = pd.Series({D20: 30.0, D22: 120.0})   # minutes; Mon wk0, Wed wk0
        hover = {D20: "mon", D22: "wed"}
        fig = viz.build_heatmap(values, hover, start, end)
        self.assertEqual(len(fig.data), 1)
        m = fig.data[0]
        # One clickable square marker per calendar day in range (14 days).
        self.assertEqual(m.mode, "markers")
        self.assertEqual(len(m.x), 14)
        cell = {(x, y): c for x, y, c in zip(m.x, m.y, m.marker.color)}
        # color is shown in hours (minutes / 60), positioned by (week col, weekday row)
        self.assertAlmostEqual(cell[(0, 0)], 0.5)   # Mon, week 0 -> 30 min = 0.5 h
        self.assertAlmostEqual(cell[(0, 2)], 2.0)   # Wed, week 0 -> 120 min = 2 h
        self.assertEqual(cell[(0, 1)], 0.0)         # Tue 7/21, in range, no value


class StackedBarTests(unittest.TestCase):
    def test_build_stacked_daily_places_segments(self):
        import pandas as pd
        import dashboard_viz as viz
        import dashboard_theme as theme
        # Two days, two activities; one gap-ish cell is zero.
        pivot = pd.DataFrame(
            {"development": [5.0, 0.0], "work": [5.0, 10.0]},
            index=[D20, D21],
        )
        colors = theme.activity_colors(pivot.columns)
        fig = viz.build_stacked_daily(pivot, colors, xlabel_fmt="%a %-m/%-d")
        # One trace per activity, stacked.
        self.assertEqual(fig.layout.barmode, "stack")
        self.assertEqual({t.name for t in fig.data}, {"development", "work"})
        work = next(t for t in fig.data if t.name == "work")
        self.assertEqual(list(work.y), [5.0, 10.0])
        # customdata[0] carries the ISO day for click-to-drill.
        self.assertEqual(work.customdata[0][0], D20.isoformat())
        self.assertEqual(work.customdata[1][0], D21.isoformat())
        # <=4 visible activities are direct-labeled on their peak day only.
        self.assertEqual(list(work.text), ["", "work"])


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


if __name__ == "__main__":
    unittest.main()
