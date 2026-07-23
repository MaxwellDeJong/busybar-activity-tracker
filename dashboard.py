"""Busy Bar — interactive activity dashboard.

Run with:  streamlit run dashboard.py

Milestone 2 implements the Day view (time-of-day timeline + per-activity totals).
Week / Month / Heatmap are stubbed and land in later milestones. The heavy lifting
(loading, filtering, shaping) lives in dashboard_data; figures in dashboard_viz.
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


def render_placeholder(view):
    st.info(f"**{view}** view lands in a later milestone. "
            "Day and Heatmap views are available now — pick one in the sidebar.")


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
    elif view == "Heatmap":
        render_heatmap(df, mode)
    else:
        render_placeholder(view)


if __name__ == "__main__":
    main()
