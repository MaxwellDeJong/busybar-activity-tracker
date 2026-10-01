"""Busy Bar — interactive activity dashboard.

Run with:  streamlit run dashboard.py

Four views, switched from a tab row at the top: Day, Week, Month and Heatmap. All
are hand-built HTML laid out for a phone first (see dashboard_html); Day lives in
dashboard_day, the rest in dashboard_period, and loading/filtering/shaping in
dashboard_data. Tapping a day in any multi-day view opens it in the Day view, and
a horizontal swipe steps to the previous / next period.
"""
from __future__ import annotations

import datetime as dt
import functools
import os

import streamlit as st

import dashboard_data as dd
import dashboard_day as day_view
import dashboard_html as h
import dashboard_period as period
import dashboard_theme as theme

st.set_page_config(page_title="Busy Bar", page_icon="📊", layout="wide")

VIEWS = ["Day", "Week", "Month", "Heatmap"]

# How often an open Day page re-renders to keep a running timer current.
LIVE_REFRESH_S = 30

# Heatmap range presets: short tab label -> (days back, or None for all, eyebrow).
RANGE_PRESETS = {
    "3 mo": (90, "Last 3 months"), "6 mo": (182, "Last 6 months"),
    "1 yr": (365, "Last 12 months"), "All": (None, "All time"),
}

# One stylesheet for the shell and every view. View tones are mixed from
# currentColor, so pages follow whichever light/dark theme Streamlit is showing;
# only the activity colors are fixed.
_CSS = """
<style>
  [data-testid="stMainBlockContainer"] {
    max-width: 1180px;
    padding-top: 2.6rem;
    padding-bottom: 4rem;
  }
  h1 { font-weight: 660; letter-spacing: -0.021em; }
  .st-key-masthead { justify-content: space-between; row-gap: 0.4rem; margin-bottom: 0.3rem; }
  .st-key-masthead h1 { padding: 0; }
  @media (max-width: 640px) {
    [data-testid="stMainBlockContainer"] { padding: 3.6rem 1rem 3rem; }
    .st-key-masthead h1 { font-size: 1.35rem; }
    /* View tabs span the full width on a phone: four equal, thumb-sized targets. */
    /* Streamlit sizes these as flex: 0 0 fit-content, which beats a plain width. */
    div.st-key-view, div.st-key-hm_range { flex: 1 1 100%; width: 100%; }
    .st-key-view [role="radiogroup"], .st-key-hm_range [role="radiogroup"] {
      display: flex; width: 100%; max-width: none; flex-wrap: nowrap;
    }
    .st-key-view button, .st-key-hm_range button {
      flex: 1 1 0; min-width: 0; min-height: 2.6rem; padding-left: 0.25rem; padding-right: 0.25rem;
    }
  }

  /* ---- Period nav + shared page pieces (hero, cards, breakdown) + Day view ---- */
  .st-key-nav { flex-wrap: nowrap; gap: 0.5rem; }
  .st-key-nav button { min-height: 2.75rem; }
  .st-key-nav_prev button, .st-key-nav_next button { width: 2.75rem; padding: 0; }
  .st-key-nav kbd { display: none; }   /* ←/→ still work; the hint is noise on a phone */
  /* Streamlit sizes the wrapping element container, so size that, not the input. */
  .st-key-nav > [data-testid="stElementContainer"]:has([data-testid="stDateInput"]) {
    flex: 1 1 auto; min-width: 0;
  }
  .st-key-nav input { font-size: 16px; min-height: 2.6rem; }  /* 16px: no iOS focus zoom */
  @media (min-width: 641px) {
    .st-key-nav > [data-testid="stElementContainer"]:has([data-testid="stDateInput"]) { flex: 0 1 13rem; }
  }

  .bb-page {
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
  /* Every fact carries a leading "·" in a 1rem gutter; the inner row is shifted
     left by that gutter and the outer box clips it, so a separator that would
     start a wrapped line is hidden. */
  .bb-facts { overflow: hidden; margin-top: 0.7rem; color: var(--fg2); font-size: 0.92rem; }
  .bb-facts > div { display: flex; flex-wrap: wrap; row-gap: 0.2rem; margin-left: -1rem; }
  .bb-facts span::before {
    content: "·"; display: inline-block; width: 1rem; text-align: center; color: var(--fg3);
  }

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

  .bb-page ul.bb-acts { list-style: none; margin: 0; padding: 0; display: grid; gap: 1rem; }
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

  .bb-page ol.bb-sessions { list-style: none; margin: 0 -0.4rem; padding: 0; }
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

  /* A timer running right now: banner under the total, striped ribbon segment,
     and a session row that ends "now". */
  .bb-live {
    display: flex; align-items: center; gap: 0.65rem; margin: -0.3rem 0 1.1rem;
    padding: 0.7rem 0.95rem; border-radius: 14px; font-size: 0.92rem;
    background: color-mix(in srgb, var(--c) 12%, transparent);
    border: 1px solid color-mix(in srgb, var(--c) 35%, transparent);
  }
  .bb-pulse {
    flex: none; width: 10px; height: 10px; border-radius: 50%; background: var(--c);
    animation: bb-pulse 2s ease-out infinite;
  }
  @keyframes bb-pulse {
    0% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--c) 60%, transparent); }
    70%, 100% { box-shadow: 0 0 0 9px transparent; }
  }
  .bb-live-text { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .bb-live-text b { font-weight: 650; }
  .bb-live-dur { font-weight: 700; }
  .bb-seg.is-live {
    background-image: repeating-linear-gradient(-45deg, transparent 0 4px,
                      rgba(255, 255, 255, 0.3) 4px 8px);
  }
  .bb-sess.is-live { background: color-mix(in srgb, var(--c) 8%, transparent); }
  .bb-sess.is-live .bb-sess-end { color: inherit; font-weight: 700; }
  .bb-sess.is-live::before { animation: bb-blink 2s ease-in-out infinite; }
  @keyframes bb-blink { 50% { opacity: 0.35; } }
  @media (prefers-reduced-motion: reduce) {
    .bb-pulse, .bb-sess.is-live::before { animation: none; }
  }

  .bb-empty { text-align: center; padding: 2.6rem 1.2rem; }
  .bb-empty-title { font-weight: 650; font-size: 1.05rem; }
  .bb-empty-sub { color: var(--fg2); font-size: 0.92rem; margin-top: 0.35rem; }
  .bb-foot { font-size: 0.75rem; color: var(--fg3); margin: 0.6rem 0.15rem 0; }

  @media (min-width: 860px) {
    .bb-total { font-size: 4.2rem; }
    .bb-cols { display: grid; grid-template-columns: 5fr 7fr; gap: 0.9rem; align-items: start; }
    .bb-cols .bb-card { margin-bottom: 0; }
    /* Multi-day pages give the breakdown the full width: use it as two columns. */
    .bb-page:not(.bb-day) ul.bb-acts { grid-template-columns: 1fr 1fr; column-gap: 2.5rem; }
    .bb-foot { margin-top: 1rem; }
  }

  /* ---- Shared: card hint + tappable day cells ---- */
  .bb-label { display: flex; justify-content: space-between; align-items: baseline; gap: 1rem; }
  .bb-hint { font-size: 0.72rem; font-weight: 500; letter-spacing: 0; text-transform: none; }
  .bb-wcol, .bb-cal-day, .bb-hm-cell {
    appearance: none; -webkit-appearance: none; border: 0; margin: 0;
    font: inherit; color: inherit; cursor: pointer; touch-action: manipulation;
  }
  .bb-wcol:disabled, .bb-cal-day:disabled { cursor: default; }
  .bb-wcol:focus-visible, .bb-cal-day:focus-visible, .bb-hm-cell:focus-visible {
    outline: 2px solid currentColor; outline-offset: 2px;
  }

  /* ---- Week: seven stacked day columns ---- */
  .bb-week { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 0.3rem; }
  .bb-wcol {
    display: flex; flex-direction: column; align-items: center; gap: 0.35rem;
    padding: 0.4rem 0 0.45rem; border-radius: 12px; background: none; min-width: 0;
    transition: background 0.15s;
  }
  .bb-wcol:not(:disabled):active { background: var(--line); }
  @media (hover: hover) { .bb-wcol:not(:disabled):hover { background: var(--wash); } }
  .bb-wval { font-size: 0.72rem; font-weight: 650; color: var(--fg2); white-space: nowrap; }
  .bb-wtrack {
    display: flex; align-items: flex-end; width: 100%; max-width: 2.6rem; height: 9.5rem;
    border-radius: 8px; background: color-mix(in srgb, currentColor 5%, transparent);
  }
  .bb-wbar {
    display: flex; flex-direction: column-reverse; gap: 2px; width: 100%;
    border-radius: 8px; overflow: hidden;
  }
  .bb-wbar i, .bb-cal-strip i { display: block; flex-basis: 0; flex-shrink: 1; }
  .bb-wbar i { min-height: 2px; }
  .bb-wday { font-size: 0.75rem; font-weight: 600; }
  .bb-wdate {
    font-size: 0.72rem; color: var(--fg3); margin-top: -0.25rem;
    padding: 0 0.35rem; border-radius: 999px; border: 1.5px solid transparent;
  }
  .bb-wcol.is-today .bb-wdate { color: inherit; font-weight: 700; border-color: currentColor; }
  .bb-wcol.is-future { opacity: 0.35; }

  /* ---- Month: calendar grid ---- */
  .bb-cal-head, .bb-cal-grid { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 0.3rem; }
  .bb-cal-head {
    margin-bottom: 0.4rem; text-align: center;
    font-size: 0.68rem; font-weight: 650; color: var(--fg3);
  }
  .bb-cal-day {
    display: flex; flex-direction: column; align-items: stretch; gap: 0.2rem;
    min-width: 0; min-height: 3.7rem; padding: 0.35rem 0.3rem 0.4rem;
    border-radius: 10px; text-align: left;
    background: color-mix(in srgb, currentColor 5.5%, transparent);
    transition: background 0.15s;
  }
  .bb-cal-day:not(:disabled):active { background: var(--line); }
  @media (hover: hover) { .bb-cal-day:not(:disabled):hover { background: var(--line); } }
  .bb-cal-day.is-empty { background: none; box-shadow: inset 0 0 0 1px var(--line); }
  .bb-cal-day.is-today { box-shadow: inset 0 0 0 2px currentColor; }
  .bb-cal-day.is-future { opacity: 0.35; box-shadow: none; }
  .bb-cal-num { font-size: 0.75rem; font-weight: 600; color: var(--fg2); line-height: 1; }
  .bb-cal-day.is-today .bb-cal-num { color: inherit; font-weight: 750; }
  .bb-cal-val { margin-top: auto; font-size: 0.74rem; font-weight: 650; line-height: 1.1; white-space: nowrap; }
  .bb-cal-strip { display: flex; gap: 1px; height: 5px; border-radius: 3px; overflow: hidden; }
  .bb-cal-strip i { min-width: 2px; }
  @media (min-width: 641px) {
    .bb-cal-head, .bb-cal-grid { gap: 0.45rem; }
    .bb-cal-day { min-height: 5.2rem; padding: 0.5rem 0.55rem 0.55rem; }
    .bb-cal-num { font-size: 0.85rem; }
    .bb-cal-val { font-size: 0.95rem; }
    .bb-cal-strip { height: 6px; }
  }

  /* ---- Heatmap: month small multiples ---- */
  .bb-hm { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1.2rem 1rem; }
  @media (min-width: 641px) { .bb-hm { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
  @media (min-width: 860px) { .bb-hm { grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 1.4rem 1.6rem; } }
  .bb-hm-title { font-size: 0.8rem; font-weight: 650; margin-bottom: 0.45rem; }
  .bb-hm-grid { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 3px; }
  .bb-hm-cell {
    display: block; aspect-ratio: 1; padding: 0; border-radius: 4px;
    background: color-mix(in srgb, currentColor 7%, transparent);
  }
  .bb-hm-cell.is-pad { background: none; }
  .bb-hm-cell.is-out { background: color-mix(in srgb, currentColor 2.5%, transparent); }
  .bb-hm-cell.lv-1 { background: color-mix(in srgb, var(--c) 30%, transparent); }
  .bb-hm-cell.lv-2 { background: color-mix(in srgb, var(--c) 55%, transparent); }
  .bb-hm-cell.lv-3 { background: color-mix(in srgb, var(--c) 80%, transparent); }
  .bb-hm-cell.lv-4 { background: var(--c); }
  .bb-hm-cell.is-today { box-shadow: 0 0 0 1.5px currentColor; }
  .bb-hm-legend {
    display: flex; flex-wrap: wrap; align-items: center; gap: 4px; margin-top: 1.1rem;
    font-size: 0.72rem; color: var(--fg3);
  }
  .bb-hm-legend .bb-hm-cell { width: 12px; cursor: default; }
  .bb-hm-legend span:first-child { margin-right: 2px; }
  .bb-hm-legend span:last-child { margin-left: 2px; }
  @media (min-width: 641px) { div.st-key-hm_measure { max-width: 18rem; } }
</style>
"""

# Page-wide touch handling, installed once (st.html re-runs the script on every
# rerun; the window flag keeps listeners from stacking):
#   * a horizontal swipe anywhere on the page presses the ‹ / › nav buttons, so it
#     steps the day, week or month of whichever view is showing;
#   * tapping a Day-view ribbon segment scrolls to and highlights its session row;
#   * the date field gets inputmode=none so tapping it opens only the calendar,
#     not the on-screen keyboard, and the icon-only ‹ / › buttons get labels.
_PAGE_JS = """
<script>
(() => {
  if (window.__bbInstalled) return;
  window.__bbInstalled = true;
  const SKIP = 'input, textarea, [data-baseweb="popover"], [data-testid="stSidebar"]';
  let start = null;
  document.addEventListener('touchstart', (e) => {
    start = null;
    if (e.touches.length !== 1 || e.target.closest(SKIP)) return;
    if (!document.querySelector('.st-key-nav')) return;
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
    const btn = document.querySelector(dx < 0 ? '.st-key-nav_next button' : '.st-key-nav_prev button');
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
  const LABELS = { nav_prev: 'Previous', nav_next: 'Next' };
  const fixNav = () => {
    document.querySelectorAll('.st-key-nav input:not([inputmode="none"])')
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

# Multi-day pages mount through this component instead of st.html so a tap can
# reach Python: any element with data-bb-day sends its ISO date as the one-shot
# `drill` trigger (cleared after the run, so it never replays). Mounted unisolated
# so the global .bb-* styles apply.
_PAGE_COMPONENT_JS = """
export default function({ parentElement, data, setTriggerValue }) {
  parentElement.innerHTML = data.html;
  const onClick = (e) => {
    const el = e.target.closest('[data-bb-day]');
    if (el && !el.disabled) setTriggerValue('drill', el.dataset.bbDay);
  };
  parentElement.addEventListener('click', onClick);
  return () => parentElement.removeEventListener('click', onClick);
}
"""
_page_component = st.components.v2.component(
    "bb_page", js=_PAGE_COMPONENT_JS, isolate_styles=False)


# --------------------------------------------------------------------------- #
# Data loading (cached, invalidated when the log file changes)                #
# --------------------------------------------------------------------------- #
@st.cache_data(show_spinner=False)
def load_prepared(path, _mtime):
    """Cached prepared frame. `_mtime` is part of the cache key so any write to the
    log invalidates it on the next run; the value itself is unused."""
    return dd.load_prepared(path)


def get_data(path):
    mtime = os.path.getmtime(path) if os.path.exists(path) else 0.0
    return load_prepared(path, mtime)


def today_logical():
    return dd.logical_day(dt.datetime.now().astimezone())


def _tzname():
    return dt.datetime.now().astimezone().tzname() or "local"


# --------------------------------------------------------------------------- #
# Shared navigation + page mounting                                           #
# --------------------------------------------------------------------------- #
def _go(state_key, value, latest):
    # Never navigate past the current period: the future is always empty.
    st.session_state[state_key] = min(value, latest)


def _period_nav(state_key, value, prev, nxt, latest, snap, unit):
    """The ‹ [date] › Today row shared by Day / Week / Month.

    One row at every width (st.columns would stack it on a phone). ``prev`` /
    ``nxt`` are the neighbouring period starts, ``latest`` the current one, and
    ``snap`` maps a picked date to its period start. Callbacks rather than
    st.rerun() so a tap costs one run; the page script maps swipes onto ‹ / ›.
    """
    with st.container(horizontal=True, vertical_alignment="center", key="nav"):
        st.button("", icon=":material/chevron_left:", key="nav_prev",
                  help=f"Previous {unit}", shortcut="left",
                  on_click=_go, args=(state_key, prev, latest))
        picked = st.date_input(unit.title(), value=value, max_value=today_logical(),
                               label_visibility="collapsed", format="MM/DD/YYYY")
        st.button("", icon=":material/chevron_right:", key="nav_next",
                  help=f"Next {unit}", shortcut="right",
                  on_click=_go, args=(state_key, nxt, latest), disabled=value >= latest)
        st.button("Today", key="nav_today", on_click=_go, args=(state_key, latest, latest),
                  disabled=value == latest)
    if snap(picked) != value:
        _go(state_key, snap(picked), latest)
        st.rerun()


def _open_day(key):
    """Component callback: a tapped day cell -> that day in the Day view."""
    iso = (st.session_state.get(key) or {}).get("drill")
    if iso:
        st.session_state.day = min(dt.date.fromisoformat(iso), today_logical())
        st.session_state.view = "Day"


def _show_page(markup, key):
    # The component doesn't forward `args` to trigger callbacks; bind the key instead.
    _page_component(data={"html": markup}, key=key,
                    on_drill_change=functools.partial(_open_day, key))


# --------------------------------------------------------------------------- #
# Views                                                                       #
# --------------------------------------------------------------------------- #
def render_day(_df, _color_map):
    # The body loads its own data (it reruns alone as a fragment), so the frame
    # and colors the other views take are unused here.
    today = today_logical()
    st.session_state.setdefault("day", today)
    day = st.session_state.day
    one = dt.timedelta(days=1)
    _period_nav("day", day, day - one, day + one, today, lambda d: d, "day")

    # Today's page — or the day a timer started on, if it's still running past
    # the 03:00 cutoff — re-renders itself every LIVE_REFRESH_S, so the running
    # session's time keeps up and finished sessions appear without a reload.
    live = dd.load_live_session()
    ticking = day == today or (live is not None and live["logical_day"] == day)
    st.fragment(_day_body, run_every=LIVE_REFRESH_S if ticking else None)(day, today)


def _day_body(day, today):
    df = get_data(dd.DEFAULT_LOG)
    color_map = theme.activity_colors(df["activity"].unique())
    now_aware = dt.datetime.now().astimezone()
    live = dd.load_live_session(now=now_aware)
    if live is not None and live["logical_day"] != day:
        live = None
    now = now_aware.replace(tzinfo=None)
    summary = day_view.summarize_day(dd.day_sessions(df, day), day, color_map,
                                     now=now, live=live)
    st.html(day_view.render_day_html(summary, day, today, _tzname(), now=now))


def render_week(df, color_map):
    today = today_logical()
    this_week = dd.week_bounds(today)[0]
    st.session_state.setdefault("week", this_week)
    monday = st.session_state.week
    seven = dt.timedelta(days=7)
    _period_nav("week", monday, monday - seven, monday + seven, this_week,
                lambda d: dd.week_bounds(d)[0], "week")

    sunday = monday + dt.timedelta(days=6)
    s = period.summarize_period(dd.range_sessions(df, monday, sunday), monday, sunday,
                                color_map, today)
    _show_page(period.render_week_html(s, today, _tzname()), "week_page")


def render_month(df, color_map):
    today = today_logical()
    this_month = today.replace(day=1)
    st.session_state.setdefault("month", this_month)
    first = st.session_state.month
    _, last = dd.month_bounds(first)
    prev_first = dd.month_bounds(first - dt.timedelta(days=1))[0]
    _period_nav("month", first, prev_first, last + dt.timedelta(days=1), this_month,
                lambda d: d.replace(day=1), "month")

    s = period.summarize_period(dd.range_sessions(df, first, last), first, last,
                                color_map, today)
    _show_page(period.render_month_html(s, today, _tzname()), "month_page")


def render_heatmap(df, color_map):
    today = today_logical()
    activities = sorted(df["activity"].unique())
    preset = st.segmented_control("Range", list(RANGE_PRESETS), default="1 yr",
                                  required=True, key="hm_range",
                                  label_visibility="collapsed")
    measure = st.selectbox("Activity", [None] + activities, key="hm_measure",
                           label_visibility="collapsed",
                           format_func=lambda a: "All activities" if a is None
                           else h.pretty_name(a))

    days_back, range_label = RANGE_PRESETS[preset]
    start = (min(df["logical_day"]) if days_back is None
             else today - dt.timedelta(days=days_back - 1))
    s = period.summarize_heatmap(dd.range_sessions(df, start, today), start, today,
                                 color_map, today, activity=measure)
    measure_label = None if measure is None else h.pretty_name(measure)
    _show_page(period.render_heatmap_html(s, today, _tzname(), range_label, measure_label),
               "heatmap_page")


# --------------------------------------------------------------------------- #
# App shell                                                                   #
# --------------------------------------------------------------------------- #
def main():
    st.html(_CSS)
    st.html(_PAGE_JS, unsafe_allow_javascript=True)

    # Tabs live in the page, not the sidebar: on a phone the sidebar hides behind
    # a hamburger. Keyed + session-backed so a tapped day can switch to Day.
    st.session_state.setdefault("view", "Day")
    with st.container(horizontal=True, vertical_alignment="center", key="masthead"):
        st.title("Busy Bar")
        st.segmented_control("View", VIEWS, key="view", required=True,
                             label_visibility="collapsed")

    df = get_data(dd.DEFAULT_LOG)
    if df.empty:
        st.warning("No sessions found in the log yet.")
        return

    # Stable color map across every activity ever seen (so colors never shift by day).
    color_map = theme.activity_colors(df["activity"].unique())
    {"Day": render_day, "Week": render_week, "Month": render_month,
     "Heatmap": render_heatmap}[st.session_state.view](df, color_map)


if __name__ == "__main__":
    main()
