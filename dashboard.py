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
            "Day view is available now — pick it in the sidebar.")


# --------------------------------------------------------------------------- #
# App shell                                                                   #
# --------------------------------------------------------------------------- #
def main():
    st.title("Busy Bar — Activity Dashboard")

    with st.sidebar:
        st.header("View")
        view = st.radio("View", VIEWS, label_visibility="collapsed")
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
    else:
        render_placeholder(view)


if __name__ == "__main__":
    main()
