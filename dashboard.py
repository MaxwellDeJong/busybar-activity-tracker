"""Busy Bar — interactive activity dashboard.

Run with:  streamlit run dashboard.py

All four views are live: Day (timeline + per-activity totals), Heatmap (overview
with click-to-drill), and Week / Month (stacked daily bars + a week aggregate
table). The heavy lifting (loading, filtering, shaping) lives in dashboard_data;
figures in dashboard_viz. Only Polish (milestone 6) remains.
"""
from __future__ import annotations

import datetime as dt
import os

import streamlit as st

import dashboard_data as dd
import dashboard_theme as theme
import dashboard_viz as viz

st.set_page_config(page_title="Busy Bar", page_icon="📊", layout="wide")

VIEWS = ["Day", "Week", "Month", "Heatmap"]


# --------------------------------------------------------------------------- #
# Data loading (cached, invalidated when the log file changes)                #
# --------------------------------------------------------------------------- #
@st.cache_data(show_spinner=False)
def load_prepared(path, _mtime):
    """Cached prepared frame. `_mtime` is part of the cache key so editing the log
    (or hitting Refresh) invalidates it; the value itself is unused."""
    return dd.load_prepared(path)


def get_data(path):
    mtime = os.path.getmtime(path) if os.path.exists(path) else 0.0
    return load_prepared(path, mtime)


def today_logical():
    return dd.logical_day(dt.datetime.now().astimezone())


# --------------------------------------------------------------------------- #
# Day view                                                                    #
# --------------------------------------------------------------------------- #
def render_day(df, color_map, mode):
    if "day" not in st.session_state:
        st.session_state.day = today_logical()

    # Navigation row.
    c_prev, c_today, c_next, c_pick = st.columns([1, 1, 1, 4])
    if c_prev.button("◀ Prev", width="stretch"):
        st.session_state.day -= dt.timedelta(days=1)
        st.rerun()
    if c_today.button("Today", width="stretch"):
        st.session_state.day = today_logical()
        st.rerun()
    if c_next.button("Next ▶", width="stretch"):
        st.session_state.day += dt.timedelta(days=1)
        st.rerun()
    picked = c_pick.date_input("Day", value=st.session_state.day, label_visibility="collapsed")
    if picked != st.session_state.day:
        st.session_state.day = picked
        st.rerun()

    day = st.session_state.day
    tzname = dt.datetime.now().astimezone().tzname() or "local"
    st.caption(f"**{day:%a %-m/%-d/%Y}** · day = 03:00→03:00 {tzname} · sessions <1m hidden")

    sessions = dd.day_sessions(df, day)
    if sessions.empty:
        st.info("No sessions of one minute or longer for this day.")
        return

    # Headline stats.
    total_s = float(sessions["duration_s"].sum())
    m1, m2, m3 = st.columns(3)
    m1.metric("Productive total", dd.fmt_duration(total_s))
    m2.metric("Sessions", len(sessions))
    m3.metric("Activities", sessions["activity"].nunique())

    # Timeline (primary) + totals (secondary).
    st.subheader("Timeline")
    st.plotly_chart(viz.build_day_timeline(sessions, day, color_map, mode),
                    width="stretch", theme=None)
    st.subheader("Total per activity")
    st.plotly_chart(viz.build_activity_totals(sessions, color_map, mode),
                    width="stretch", theme=None)


# --------------------------------------------------------------------------- #
# Week / Month views (shared stacked-daily-bar form)                          #
# --------------------------------------------------------------------------- #
def _tzname():
    return dt.datetime.now().astimezone().tzname() or "local"


def _drill_from_bar(event, key, lo, hi):
    """A clicked stacked-bar segment -> open its day in the Day view.

    The segment's logical day rides in customdata[0]; a session-state token guards
    against re-processing a replayed selection (which would fight the nav)."""
    points = _selected_points(event)
    if not points:
        return
    cd = points[0].get("customdata")
    iso = cd[0] if isinstance(cd, (list, tuple)) and cd else None
    if not iso:
        return
    token = (key, iso)
    if st.session_state.get("_bar_token") == token:
        return
    st.session_state._bar_token = token
    clicked = dt.date.fromisoformat(iso)
    if lo <= clicked <= hi:
        st.session_state.day = clicked
        st.session_state.view = "Day"
        st.rerun()


def _totals_table(pivot, sessions):
    """The week-level analogue of activity_summary.py Table 2: per-activity totals
    (descending) plus session counts and a TOTAL row, as a column dict for
    st.dataframe."""
    totals = pivot.sum(axis=0).sort_values(ascending=False)
    counts = sessions.groupby("activity").size()
    grand = float(totals.sum())
    return {
        "activity": list(totals.index) + ["TOTAL"],
        "sessions": [int(counts.get(a, 0)) for a in totals.index] + [len(sessions)],
        "total": [dd.fmt_duration(v * 60.0) for v in totals.values]
                 + [dd.fmt_duration(grand * 60.0)],
    }


def _scoped_pivot(df, start, end):
    """range_by_activity sliced to [start, end] with all-zero activity columns
    dropped, so only activities actually present in the window are stacked/legended.
    Returns (pivot, total_minutes)."""
    pivot = dd.range_by_activity(df, start, end)
    if not pivot.empty:
        pivot = pivot.loc[:, pivot.sum(axis=0) > 0]
    total = 0.0 if pivot.empty else float(pivot.to_numpy().sum())
    return pivot, total


def render_week(df, color_map, mode):
    if "week" not in st.session_state:
        st.session_state.week = dd.week_bounds(today_logical())[0]

    c_prev, c_this, c_next, c_pick = st.columns([1, 1, 1, 4])
    if c_prev.button("◀ Prev", width="stretch", key="wk_prev"):
        st.session_state.week -= dt.timedelta(days=7)
        st.rerun()
    if c_this.button("This week", width="stretch", key="wk_this"):
        st.session_state.week = dd.week_bounds(today_logical())[0]
        st.rerun()
    if c_next.button("Next ▶", width="stretch", key="wk_next"):
        st.session_state.week += dt.timedelta(days=7)
        st.rerun()
    picked = c_pick.date_input("Week", value=st.session_state.week,
                               label_visibility="collapsed", key="wk_pick")
    snapped = dd.week_bounds(picked)[0]
    if snapped != st.session_state.week:
        st.session_state.week = snapped
        st.rerun()

    monday = st.session_state.week
    sunday = monday + dt.timedelta(days=6)
    st.caption(f"**Week of {monday:%a %-m/%-d/%Y}** · day = 03:00→03:00 {_tzname()} "
               "· sessions <1m hidden · **click a bar to open that day**")

    pivot, total_min = _scoped_pivot(df, monday, sunday)
    if pivot.empty or total_min == 0:
        st.info("No sessions of one minute or longer this week.")
        return

    active_days = int((pivot.sum(axis=1) > 0).sum())
    avg_min = total_min / active_days if active_days else 0.0    # avg over active days
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Productive total", dd.fmt_duration(total_min * 60.0))
    m2.metric("Avg / active day", dd.fmt_duration(avg_min * 60.0) if active_days else "—")
    m3.metric("Active days", f"{active_days} / 7")
    m4.metric("Activities", pivot.shape[1])

    st.subheader("Daily activity")
    event = st.plotly_chart(
        viz.build_stacked_daily(pivot, color_map, mode, xlabel_fmt="%a %-m/%-d"),
        width="stretch", theme=None, key="week_bars",
        on_select="rerun", selection_mode="points",
    )
    _drill_from_bar(event, "week", monday, sunday)

    week_sessions = dd.range_sessions(df, monday, sunday)
    c_bar, c_tbl = st.columns([3, 2])
    with c_bar:
        st.subheader("Total per activity")
        st.plotly_chart(viz.build_activity_totals(week_sessions, color_map, mode),
                        width="stretch", theme=None)
    with c_tbl:
        st.subheader("Breakdown")
        st.dataframe(_totals_table(pivot, week_sessions),
                     hide_index=True, width="stretch")


def render_month(df, color_map, mode):
    if "month" not in st.session_state:
        st.session_state.month = today_logical().replace(day=1)

    c_prev, c_this, c_next, c_pick = st.columns([1, 1, 1, 4])
    if c_prev.button("◀ Prev", width="stretch", key="mo_prev"):
        prev_last = st.session_state.month - dt.timedelta(days=1)
        st.session_state.month = dd.month_bounds(prev_last)[0]
        st.rerun()
    if c_this.button("This month", width="stretch", key="mo_this"):
        st.session_state.month = today_logical().replace(day=1)
        st.rerun()
    if c_next.button("Next ▶", width="stretch", key="mo_next"):
        st.session_state.month = dd.month_bounds(st.session_state.month)[1] + dt.timedelta(days=1)
        st.rerun()
    picked = c_pick.date_input("Month", value=st.session_state.month,
                               label_visibility="collapsed", key="mo_pick")
    if picked.replace(day=1) != st.session_state.month:
        st.session_state.month = picked.replace(day=1)
        st.rerun()

    first, last = dd.month_bounds(st.session_state.month)
    st.caption(f"**{first:%B %Y}** · day = 03:00→03:00 {_tzname()} "
               "· sessions <1m hidden · **click a bar to open that day**")

    pivot, total_min = _scoped_pivot(df, first, last)
    if pivot.empty or total_min == 0:
        st.info("No sessions of one minute or longer this month.")
        return

    daily = pivot.sum(axis=1)
    active_days = int((daily > 0).sum())
    avg_min = total_min / active_days if active_days else 0.0    # avg over active days
    busiest = daily.idxmax()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Productive total", dd.fmt_duration(total_min * 60.0))
    m2.metric("Avg / active day", dd.fmt_duration(avg_min * 60.0) if active_days else "—")
    m3.metric("Active days", f"{active_days} / {daily.shape[0]}")
    m4.metric("Busiest day", f"{busiest:%-m/%-d}", dd.fmt_duration(daily.max() * 60.0))

    st.subheader("Daily activity")
    event = st.plotly_chart(
        viz.build_stacked_daily(pivot, color_map, mode, xlabel_fmt="%-d"),
        width="stretch", theme=None, key="month_bars",
        on_select="rerun", selection_mode="points",
    )
    _drill_from_bar(event, "month", first, last)


# --------------------------------------------------------------------------- #
# Heatmap (overview) view                                                     #
# --------------------------------------------------------------------------- #
RANGE_PRESETS = {"Last 90 days": 90, "Last 6 months": 182, "Last 12 months": 365, "All": None}
PRODUCTIVE = "Productive total"


def _selected_points(event):
    """Extract clicked points from a plotly on_select event, robust to shape."""
    try:
        return event["selection"]["points"] or []
    except (TypeError, KeyError):
        return []


def _range_bounds(df, preset):
    end = today_logical()
    if RANGE_PRESETS[preset] is None:                 # "All": from first logged day
        start = min(df["logical_day"])
    else:
        start = end - dt.timedelta(days=RANGE_PRESETS[preset] - 1)
    return start, end


def render_heatmap(df, mode):
    activities = sorted(df["activity"].unique())

    c_range, c_measure = st.columns([1, 1])
    preset = c_range.selectbox("Range", list(RANGE_PRESETS), index=2)   # default 12 months
    measure = c_measure.selectbox("Coloring", [PRODUCTIVE] + activities)

    start, end = _range_bounds(df, preset)
    pivot = dd.range_by_activity(df, start, end)       # day x activity, gaps = 0
    productive = pivot.sum(axis=1) if not pivot.empty else pivot

    # Selected measure per day (minutes).
    if measure == PRODUCTIVE:
        values = productive
    else:
        values = pivot[measure] if measure in pivot.columns else productive * 0.0

    st.caption(f"**{start:%-m/%-d/%Y} → {end:%-m/%-d/%Y}** · coloring: {measure} "
               "· each cell = one day (03:00→03:00) · **click a day to open it**")

    # Summary stats over the range. The average counts only days with activity.
    total_min = float(values.sum())
    active_days = int((values > 0).sum())
    avg_min = total_min / active_days if active_days else 0.0
    busiest = values.idxmax() if active_days else None
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total in range", dd.fmt_duration(total_min * 60))
    m2.metric("Avg / active day", dd.fmt_duration(avg_min * 60) if active_days else "—")
    m3.metric("Active days", active_days)
    m4.metric("Busiest day", f"{busiest:%-m/%-d}" if busiest is not None else "—",
              dd.fmt_duration(values.max() * 60) if active_days else None)

    # Per-day hover: date, productive total, per-activity breakdown.
    hover = {}
    for day in values.index:
        parts = [f"<b>{day:%a %-m/%-d/%Y}</b>",
                 f"Productive: {dd.fmt_duration(float(productive.get(day, 0.0)) * 60)}"]
        breakdown = sorted(
            ((a, pivot.loc[day, a]) for a in activities if a in pivot.columns and pivot.loc[day, a] > 0),
            key=lambda kv: -kv[1],
        )
        parts += [f"{a}: {dd.fmt_duration(v * 60)}" for a, v in breakdown]
        if len(parts) == 2:
            parts.append("<i>no activity</i>")
        hover[day] = "<br>".join(parts)

    # Click-to-drill: capture a cell click, map it back to its logical day, and
    # switch to the Day view. The Day view's own date picker is the fallback.
    grid_start, _ = viz.calendar_grid(start, end)
    event = st.plotly_chart(
        viz.build_heatmap(values, hover, start, end, mode, unit="hr"),
        width="stretch", theme=None, key="heatmap_select",
        on_select="rerun", selection_mode="points",
    )
    points = _selected_points(event)
    if points:
        p = points[0]
        token = (int(round(p["x"])), int(round(p["y"])))
        # Guard against re-processing a replayed selection (avoids a nav loop).
        if st.session_state.get("_hm_token") != token:
            st.session_state._hm_token = token
            clicked = viz.date_from_cell(grid_start, token[0], token[1])
            if start <= clicked <= end:
                st.session_state.day = clicked
                st.session_state.view = "Day"
                st.rerun()


# --------------------------------------------------------------------------- #
# App shell                                                                   #
# --------------------------------------------------------------------------- #
def main():
    st.title("Busy Bar — Activity Dashboard")

    # View is session-backed so a heatmap-cell click can switch it programmatically.
    st.session_state.setdefault("view", "Day")
    with st.sidebar:
        st.header("View")
        # No widget key: the index tracks session_state.view, and we store the
        # user's choice back — this lets a click set the view without a state clash.
        view = st.radio("View", VIEWS, index=VIEWS.index(st.session_state.view),
                        label_visibility="collapsed")
        st.session_state.view = view
        st.header("Appearance")
        mode = st.radio("Theme", ["light", "dark"], label_visibility="collapsed",
                        format_func=str.capitalize)
        st.divider()
        if st.button("↻ Refresh data", width="stretch"):
            st.cache_data.clear()
            st.rerun()
        st.caption(f"Source: `{os.path.basename(dd.DEFAULT_LOG)}`")

    df = get_data(dd.DEFAULT_LOG)
    if df.empty:
        st.warning("No sessions found in the log yet.")
        return

    # Stable color map across every activity ever seen (so colors never shift by day).
    color_map = theme.activity_colors(df["activity"].unique(), mode)

    if view == "Day":
        render_day(df, color_map, mode)
    elif view == "Week":
        render_week(df, color_map, mode)
    elif view == "Month":
        render_month(df, color_map, mode)
    elif view == "Heatmap":
        render_heatmap(df, mode)


if __name__ == "__main__":
    main()
