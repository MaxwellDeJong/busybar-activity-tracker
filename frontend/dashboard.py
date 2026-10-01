"""Busy Bar — interactive activity dashboard.

Run with:  streamlit run dashboard.py

All four views are live: Day (timeline + per-activity totals), Heatmap (overview
with click-to-drill), and Week / Month (stacked daily bars + a week aggregate
table). The heavy lifting (loading, filtering, shaping) lives in dashboard_data;
figures in dashboard_viz; palette + Plotly theming in dashboard_theme. A sidebar
Light/Dark radio drives the chart theme.
"""
from __future__ import annotations

import datetime as dt
import os

import streamlit as st

import dashboard_data as dd
import dashboard_day as day_view
import dashboard_theme as theme
import dashboard_viz as viz

st.set_page_config(page_title="Busy Bar", page_icon="📊", layout="wide")

VIEWS = ["Day", "Week", "Month", "Heatmap"]

# Charts own their own theming (dashboard_theme), so Streamlit must not re-skin them.
# The modebar is hidden — this is a read/click dashboard, not a Plotly editor; cells
# and bars stay clickable for drill-down without it.
PLOTLY_CONFIG = {"displayModeBar": False, "scrollZoom": False}

# Layout-only CSS (no colors — Streamlit's own theme owns those): tighten
# the default padding, centre the content, and give the stat tiles a quiet, uniform
# label/value rhythm so they read as a designed row rather than raw st.metric output.
_CSS = """
<style>
  [data-testid="stMainBlockContainer"] {
    max-width: 1180px;
    padding-top: 2.6rem;
    padding-bottom: 4rem;
  }
  h1 { font-weight: 660; letter-spacing: -0.021em; }
  [data-testid="stMetric"] { padding: 0.7rem 1rem 0.8rem; border-radius: 0.6rem; }
  [data-testid="stMetricLabel"] p {
    font-size: 0.72rem; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.05em; opacity: 0.62;
  }
  [data-testid="stMetricValue"] { font-size: 1.55rem; font-weight: 600; }
  [data-testid="stMetricDelta"] { font-size: 0.8rem; }
  /* Section labels above each chart: smaller and calmer than a default subheader. */
  h3 { font-size: 0.95rem; font-weight: 600; letter-spacing: 0.01em; margin: 0.4rem 0 0.2rem; }
  section[data-testid="stSidebar"] h2 {
    font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em; opacity: 0.6;
  }
  @media (max-width: 640px) {
    [data-testid="stMainBlockContainer"] { padding: 3.6rem 1rem 3rem; }
    .st-key-masthead h1 { font-size: 1.35rem; padding: 0; }
    .st-key-masthead [data-testid="stCaptionContainer"] { display: none; }
  }

  /* ---- Day view (markup from dashboard_day) ------------------------------
     Tones are mixed from currentColor, so the page follows whichever light/dark
     theme Streamlit is showing; only the activity colors are fixed. */
  .st-key-day_nav { flex-wrap: nowrap; gap: 0.5rem; }
  .st-key-day_nav button { min-height: 2.75rem; }
  .st-key-day_prev button, .st-key-day_next button { width: 2.75rem; padding: 0; }
  .st-key-day_nav kbd { display: none; }   /* ←/→ still work; the hint is noise on a phone */
  .st-key-day_nav [data-testid="stDateInput"] { flex: 1 1 auto; min-width: 0; }
  .st-key-day_nav input { font-size: 16px; min-height: 2.6rem; }  /* 16px: no iOS focus zoom */
  @media (min-width: 641px) { .st-key-day_nav [data-testid="stDateInput"] { flex: 0 1 13rem; } }

  .bb-day {
    --fg2: color-mix(in srgb, currentColor 66%, transparent);
    --fg3: color-mix(in srgb, currentColor 48%, transparent);
    --line: color-mix(in srgb, currentColor 11%, transparent);
    --wash: color-mix(in srgb, currentColor 3.5%, transparent);
    font-variant-numeric: tabular-nums;
    -webkit-tap-highlight-color: transparent;
  }
  .bb-hero { padding: 0.5rem 0.15rem 1.2rem; }
  .bb-eyebrow {
    font-size: 0.72rem; font-weight: 650; letter-spacing: 0.09em;
    text-transform: uppercase; color: var(--fg3);
  }
  .bb-date { font-size: 1.15rem; font-weight: 600; margin-top: 0.15rem; }
  .bb-total {
    font-size: 3.4rem; font-weight: 700; letter-spacing: -0.04em; line-height: 1;
    margin-top: 1rem;
  }
  .bb-unit {
    font-size: 0.42em; font-weight: 600; letter-spacing: 0; color: var(--fg3);
    margin-left: 0.08em;
  }
  .bb-facts {
    display: flex; flex-wrap: wrap; gap: 0.2rem 0.5rem; margin-top: 0.7rem;
    color: var(--fg2); font-size: 0.92rem;
  }
  .bb-facts span + span::before { content: "·"; margin-right: 0.5rem; color: var(--fg3); }

  .bb-card {
    border: 1px solid var(--line); border-radius: 16px; background: var(--wash);
    padding: 1rem 1.1rem 1.1rem; margin-bottom: 0.9rem;
  }
  .bb-label {
    font-size: 0.72rem; font-weight: 650; letter-spacing: 0.08em;
    text-transform: uppercase; color: var(--fg3); margin-bottom: 0.9rem;
  }

  .bb-track {
    position: relative; height: 3rem; border-radius: 10px;
    background: color-mix(in srgb, currentColor 5%, transparent);
  }
  .bb-grid { position: absolute; top: 0; bottom: 0; width: 1px; background: var(--line); }
  .bb-seg {
    position: absolute; top: 0; bottom: 0; min-width: 3px;
    border: 0; padding: 0; border-radius: 5px; cursor: pointer;
    transition: filter 0.15s, transform 0.15s;
  }
  .bb-seg:hover, .bb-seg:focus-visible { filter: brightness(1.15); transform: scaleY(1.08); outline: none; }
  .bb-now {
    position: absolute; top: -5px; bottom: -5px; width: 2px; margin-left: -1px;
    background: currentColor; border-radius: 1px; pointer-events: none;
  }
  .bb-now::before {
    content: ""; position: absolute; top: -3px; left: -3px;
    width: 8px; height: 8px; border-radius: 50%; background: currentColor;
  }
  .bb-ticks { position: relative; height: 1.1rem; margin-top: 0.45rem; }
  .bb-tick {
    position: absolute; transform: translateX(-50%); white-space: nowrap;
    font-size: 0.72rem; color: var(--fg3);
  }

  .bb-day ul.bb-acts { list-style: none; margin: 0; padding: 0; display: grid; gap: 1rem; }
  .bb-act { margin: 0; }
  .bb-act-head { display: flex; align-items: center; gap: 0.6rem; font-size: 0.97rem; }
  .bb-dot { width: 10px; height: 10px; border-radius: 3px; flex: none; }
  .bb-act-name {
    flex: 1; min-width: 0; font-weight: 550;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .bb-act-time { font-weight: 650; }
  .bb-act-bar {
    height: 6px; border-radius: 3px; margin: 0.5rem 0 0.3rem; overflow: hidden;
    background: color-mix(in srgb, currentColor 7%, transparent);
  }
  .bb-act-bar span { display: block; height: 100%; border-radius: 3px; }
  .bb-act-meta { font-size: 0.78rem; color: var(--fg3); }

  .bb-day ol.bb-sessions { list-style: none; margin: 0 -0.4rem; padding: 0; }
  .bb-sess {
    position: relative; display: grid; grid-template-columns: 3.3rem 1fr auto;
    align-items: center; gap: 0.75rem; margin: 0;
    padding: 0.6rem 0.5rem 0.6rem 1.05rem; border-radius: 10px;
  }
  .bb-sess::before {
    content: ""; position: absolute; left: 0.4rem; top: 0.6rem; bottom: 0.6rem;
    width: 4px; border-radius: 2px; background: var(--c);
  }
  .bb-sess + .bb-sess::after {
    content: ""; position: absolute; top: 0; left: 1.05rem; right: 0.5rem;
    border-top: 1px solid var(--line);
  }
  .bb-sess-time { display: flex; flex-direction: column; font-weight: 600; font-size: 0.92rem; line-height: 1.25; }
  .bb-sess-end { font-weight: 400; font-size: 0.78rem; color: var(--fg3); }
  .bb-sess-name {
    font-size: 0.97rem; min-width: 0;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .bb-sess-dur { font-weight: 600; font-size: 0.92rem; color: var(--fg2); }
  .bb-gap {
    display: flex; align-items: center; gap: 0.6rem; margin: 0;
    padding: 0.3rem 0.5rem 0.3rem 1.05rem; font-size: 0.75rem; color: var(--fg3);
  }
  .bb-gap::before { content: ""; flex: 0 0 3.3rem; border-top: 1px dashed var(--line); }
  .bb-gap::after { content: ""; flex: 1; border-top: 1px dashed var(--line); }
  .bb-flash { animation: bb-flash 1.8s ease-out; }
  @keyframes bb-flash {
    from { background: color-mix(in srgb, var(--c) 24%, transparent); }
    to { background: transparent; }
  }

  .bb-empty { text-align: center; padding: 2.6rem 1.2rem; }
  .bb-empty-title { font-weight: 650; font-size: 1.05rem; }
  .bb-empty-sub { color: var(--fg2); font-size: 0.92rem; margin-top: 0.35rem; }
  .bb-foot { font-size: 0.75rem; color: var(--fg3); margin: 0.6rem 0.15rem 0; }

  @media (min-width: 860px) {
    .bb-total { font-size: 4.2rem; }
    .bb-cols { display: grid; grid-template-columns: 5fr 7fr; gap: 0.9rem; align-items: start; }
    .bb-cols .bb-card { margin-bottom: 0; }
    .bb-foot { margin-top: 1rem; }
  }
</style>
"""

# Day-view touch handling, installed once per page (st.html re-runs the script on
# every rerun; the window flag keeps listeners from stacking):
#   * a horizontal swipe anywhere on the page presses the ‹ / › nav buttons;
#   * tapping a ribbon segment scrolls to and highlights its session row;
#   * the date field gets inputmode=none so tapping it opens only the calendar,
#     not the on-screen keyboard, and the icon-only ‹ / › buttons get labels.
_DAY_JS = """
<script>
(() => {
  if (window.__bbDayInstalled) return;
  window.__bbDayInstalled = true;
  const SKIP = 'input, textarea, [data-baseweb="popover"], [data-testid="stSidebar"], .js-plotly-plot';
  let start = null;
  document.addEventListener('touchstart', (e) => {
    start = null;
    if (e.touches.length !== 1 || e.target.closest(SKIP)) return;
    if (!document.querySelector('.st-key-day_nav')) return;
    const t = e.touches[0];
    start = { x: t.clientX, y: t.clientY, at: Date.now() };
  }, { passive: true });
  document.addEventListener('touchend', (e) => {
    if (!start) return;
    const t = e.changedTouches[0];
    const dx = t.clientX - start.x, dy = t.clientY - start.y;
    const quick = Date.now() - start.at < 700;
    start = null;
    if (!quick || Math.abs(dx) < 60 || Math.abs(dx) < 2 * Math.abs(dy)) return;
    const btn = document.querySelector(dx < 0 ? '.st-key-day_next button' : '.st-key-day_prev button');
    if (btn && !btn.disabled) btn.click();
  }, { passive: true });
  document.addEventListener('click', (e) => {
    const seg = e.target.closest('.bb-seg');
    if (!seg) return;
    const row = document.getElementById('bb-session-' + seg.dataset.bbSession);
    if (!row) return;
    row.scrollIntoView({ behavior: 'smooth', block: 'center' });
    row.classList.remove('bb-flash');
    void row.offsetWidth;                      // restart the animation
    row.classList.add('bb-flash');
  });
  // Icon-only st.buttons render aria-label="", so name them for screen readers.
  const LABELS = { day_prev: 'Previous day', day_next: 'Next day' };
  const fixNav = () => {
    document.querySelectorAll('.st-key-day_nav input:not([inputmode="none"])')
      .forEach((el) => el.setAttribute('inputmode', 'none'));
    for (const [key, label] of Object.entries(LABELS)) {
      document.querySelectorAll(`.st-key-${key} button:not([aria-label="${label}"])`)
        .forEach((el) => el.setAttribute('aria-label', label));
    }
  };
  new MutationObserver(fixNav).observe(document.body, { childList: true, subtree: true });
  fixNav();
})();
</script>
"""




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
def _set_day(day):
    # Never navigate past today: future days are always empty.
    st.session_state.day = min(day, today_logical())


def render_day(df, color_map):
    if "day" not in st.session_state:
        st.session_state.day = today_logical()
    today = today_logical()
    day = st.session_state.day

    # One row at every width (st.columns would stack this on a phone):
    # ‹ [date] › Today. Callbacks rather than st.rerun() so a tap costs one run.
    with st.container(horizontal=True, vertical_alignment="center", key="day_nav"):
        st.button("", icon=":material/chevron_left:", key="day_prev", help="Previous day",
                  shortcut="left",
                  on_click=_set_day, args=(day - dt.timedelta(days=1),))
        picked = st.date_input("Day", value=day, max_value=today,
                               label_visibility="collapsed", format="MM/DD/YYYY")
        st.button("", icon=":material/chevron_right:", key="day_next", help="Next day",
                  shortcut="right",
                  on_click=_set_day, args=(day + dt.timedelta(days=1),),
                  disabled=day >= today)
        st.button("Today", key="day_today", on_click=_set_day, args=(today,),
                  disabled=day == today)
    if picked != day:
        _set_day(picked)
        st.rerun()

    now = dt.datetime.now().astimezone().replace(tzinfo=None)
    summary = day_view.summarize_day(dd.day_sessions(df, day), day, color_map, now=now)
    st.html(day_view.render_day_html(summary, day, today, _tzname(), now=now))
    st.html(_DAY_JS, unsafe_allow_javascript=True)


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
        "total": [dd.fmt_duration(v * 60.0, "minute") for v in totals.values]
                 + [dd.fmt_duration(grand * 60.0, "minute")],
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


def render_week(df, color_map):
    if "week" not in st.session_state:
        st.session_state.week = dd.week_bounds(today_logical())[0]

    c_prev, c_this, c_next, c_pick = st.columns([1, 1, 1, 4])
    if c_prev.button("Prev", icon=":material/chevron_left:", width="stretch", key="wk_prev"):
        st.session_state.week -= dt.timedelta(days=7)
        st.rerun()
    if c_this.button("This week", icon=":material/today:", width="stretch", key="wk_this"):
        st.session_state.week = dd.week_bounds(today_logical())[0]
        st.rerun()
    if c_next.button("Next", icon=":material/chevron_right:", width="stretch", key="wk_next"):
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
    m1.metric("Productive total", dd.fmt_duration(total_min * 60.0, "minute"), border=True)
    m2.metric("Avg / active day", dd.fmt_duration(avg_min * 60.0, "minute") if active_days else "—",
              border=True)
    m3.metric("Active days", f"{active_days} / 7", border=True)
    m4.metric("Activities", pivot.shape[1], border=True)

    st.subheader("Daily activity")
    event = st.plotly_chart(
        viz.build_stacked_daily(pivot, color_map, xlabel_fmt="%a %-m/%-d"),
        width="stretch", theme=None, key="week_bars", config=PLOTLY_CONFIG,
        on_select="rerun", selection_mode="points",
    )
    _drill_from_bar(event, "week", monday, sunday)

    week_sessions = dd.range_sessions(df, monday, sunday)
    c_bar, c_tbl = st.columns([3, 2])
    with c_bar:
        st.subheader("Total per activity")
        st.plotly_chart(viz.build_activity_totals(week_sessions, color_map),
                        width="stretch", theme=None, config=PLOTLY_CONFIG)
    with c_tbl:
        st.subheader("Breakdown")
        st.dataframe(_totals_table(pivot, week_sessions),
                     hide_index=True, width="stretch")


def render_month(df, color_map):
    if "month" not in st.session_state:
        st.session_state.month = today_logical().replace(day=1)

    c_prev, c_this, c_next, c_pick = st.columns([1, 1, 1, 4])
    if c_prev.button("Prev", icon=":material/chevron_left:", width="stretch", key="mo_prev"):
        prev_last = st.session_state.month - dt.timedelta(days=1)
        st.session_state.month = dd.month_bounds(prev_last)[0]
        st.rerun()
    if c_this.button("This month", icon=":material/today:", width="stretch", key="mo_this"):
        st.session_state.month = today_logical().replace(day=1)
        st.rerun()
    if c_next.button("Next", icon=":material/chevron_right:", width="stretch", key="mo_next"):
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
    m1.metric("Productive total", dd.fmt_duration(total_min * 60.0, "minute"), border=True)
    m2.metric("Avg / active day", dd.fmt_duration(avg_min * 60.0, "minute") if active_days else "—",
              border=True)
    m3.metric("Active days", f"{active_days} / {daily.shape[0]}", border=True)
    m4.metric("Busiest day", f"{busiest:%-m/%-d}", dd.fmt_duration(daily.max() * 60.0, "minute"),
              border=True)

    st.subheader("Daily activity")
    event = st.plotly_chart(
        viz.build_stacked_daily(pivot, color_map, xlabel_fmt="%-d"),
        width="stretch", theme=None, key="month_bars", config=PLOTLY_CONFIG,
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


def render_heatmap(df):
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
    m1.metric("Total in range", dd.fmt_duration(total_min * 60, "minute"), border=True)
    m2.metric("Avg / active day", dd.fmt_duration(avg_min * 60, "minute") if active_days else "—",
              border=True)
    m3.metric("Active days", active_days, border=True)
    m4.metric("Busiest day", f"{busiest:%-m/%-d}" if busiest is not None else "—",
              dd.fmt_duration(values.max() * 60, "minute") if active_days else None, border=True)

    # Per-day hover: date, productive total, per-activity breakdown.
    hover = {}
    for day in values.index:
        parts = [f"<b>{day:%a %-m/%-d/%Y}</b>",
                 f"Productive: {dd.fmt_duration(float(productive.get(day, 0.0)) * 60, 'minute')}"]
        breakdown = sorted(
            ((a, pivot.loc[day, a]) for a in activities if a in pivot.columns and pivot.loc[day, a] > 0),
            key=lambda kv: -kv[1],
        )
        parts += [f"{a}: {dd.fmt_duration(v * 60, 'minute')}" for a, v in breakdown]
        if len(parts) == 2:
            parts.append("<i>no activity</i>")
        hover[day] = "<br>".join(parts)

    # Click-to-drill: capture a cell click, map it back to its logical day, and
    # switch to the Day view. The Day view's own date picker is the fallback.
    grid_start, _ = viz.calendar_grid(start, end)
    event = st.plotly_chart(
        viz.build_heatmap(values, hover, start, end, unit="hr"),
        width="stretch", theme=None, key="heatmap_select", config=PLOTLY_CONFIG,
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
    st.html(_CSS)

    # View is session-backed so a heatmap-cell click can switch it programmatically.
    st.session_state.setdefault("view", "Day")
    with st.sidebar:
        st.header("View")
        # No widget key: the index tracks session_state.view, and we store the
        # user's choice back — this lets a click set the view without a state clash.
        view = st.radio("View", VIEWS, index=VIEWS.index(st.session_state.view),
                        label_visibility="collapsed")
        st.session_state.view = view

        st.divider()
        st.header("Data")
        if st.button("Refresh", icon=":material/refresh:", width="stretch"):
            st.cache_data.clear()
            st.rerun()
        st.caption(f"Source `{os.path.basename(dd.DEFAULT_LOG)}`")

    with st.container(key="masthead"):
        st.title("Busy Bar")
        st.caption("A quiet look at where the time goes — heatmap down to the day.")

    df = get_data(dd.DEFAULT_LOG)
    if df.empty:
        st.warning("No sessions found in the log yet.")
        return

    # Stable color map across every activity ever seen (so colors never shift by day).
    color_map = theme.activity_colors(df["activity"].unique())

    if view == "Day":
        render_day(df, color_map)
    elif view == "Week":
        render_week(df, color_map)
    elif view == "Month":
        render_month(df, color_map)
    elif view == "Heatmap":
        render_heatmap(df)


if __name__ == "__main__":
    main()
