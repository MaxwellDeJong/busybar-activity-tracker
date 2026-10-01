"""Day view: summary shaping + hand-built HTML (no Plotly, no Streamlit).

The Day view is the page most often opened on a phone, where Plotly's hover-driven
charts work poorly: a tap is needed to read anything, and a 24 h axis squeezed into
~360 px turns sessions into slivers. So this view is plain HTML/CSS rendered with
``st.html``: a hero total, a ribbon zoomed to the hours actually worked, a
per-activity breakdown, and a chronological session list that carries every detail
without hover. Styles live in dashboard.py's global CSS (``.bb-*`` classes) and
are derived from ``currentColor`` so the page follows Streamlit's light/dark theme.

`summarize_day` is pure data (unit-tested); `render_day_html` turns it into markup.
"""
from __future__ import annotations

import datetime as dt
import html

import dashboard_data as dd
import dashboard_theme as theme

# Breaks shorter than this are folded silently into the session list.
MIN_GAP_S = 10 * 60
# The ribbon never zooms in tighter than this, so a single short session still
# reads as part of a day rather than filling the whole track.
MIN_RIBBON_HOURS = 6


def pretty_name(activity):
    """'tech_reading' -> 'Tech reading' for display; the raw key stays the color key."""
    text = str(activity).replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else str(activity)


def _naive(ts):
    """Local Timestamp/datetime -> naive local datetime (the view's working clock)."""
    if hasattr(ts, "to_pydatetime"):
        ts = ts.to_pydatetime()
    return ts.replace(tzinfo=None)


def _ribbon_window(day, first_start, last_end, now=None):
    """Zoom the ribbon to whole hours around the worked span, clamped to the
    logical day [03:00, next 03:00) and at least MIN_RIBBON_HOURS wide.

    When ``now`` falls inside the logical day it is included, so today's ribbon
    also shows the idle stretch since the last session.
    """
    day_lo = dt.datetime.combine(day, dt.time(dd.DAY_CUTOFF_HOUR))
    day_hi = day_lo + dt.timedelta(days=1)
    lo, hi = first_start, last_end
    if now is not None and day_lo <= now < day_hi:
        hi = max(hi, now)
    lo = lo.replace(minute=0, second=0, microsecond=0)
    if hi.minute or hi.second or hi.microsecond:
        hi = hi.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
    # Widen symmetrically to the minimum span, then push back inside the day.
    short = dt.timedelta(hours=MIN_RIBBON_HOURS) - (hi - lo)
    if short > dt.timedelta(0):
        before = dt.timedelta(hours=int(short.total_seconds() // 7200))  # whole hours
        lo, hi = lo - before, hi + (short - before)
    if lo < day_lo:
        lo, hi = day_lo, min(day_hi, hi + (day_lo - lo))
    if hi > day_hi:
        lo, hi = max(day_lo, lo - (hi - day_hi)), day_hi
    return lo, hi


def _ticks(lo, hi):
    """Hour ticks across [lo, hi]: the smallest step in {1,2,3,4,6} giving at most
    six intervals, so labels never crowd a phone-width track."""
    hours = (hi - lo).total_seconds() / 3600
    step = next(s for s in (1, 2, 3, 4, 6) if hours / s <= 6)
    t = lo
    while t.hour % step:
        t += dt.timedelta(hours=1)
    out = []
    while t <= hi:
        out.append(t)
        t += dt.timedelta(hours=step)
    return out


def summarize_day(sessions, day, color_map, now=None):
    """Shape one logical day's sessions into what the Day view renders.

    ``sessions`` is `dashboard_data.day_sessions` output (sorted by start).
    ``now`` is a naive local datetime, used only to place the ribbon's now-marker.
    Returns None for an empty day, else a dict with ``total_s``, ``count``,
    ``first_start``/``last_end``, ``activities`` (desc by time, each with share),
    ``items`` (sessions interleaved with ``gap`` rows of at least MIN_GAP_S), and the
    ribbon window/ticks.
    """
    if sessions is None or sessions.empty:
        return None

    rows = []
    for _, r in sessions.iterrows():
        start = _naive(r["start_local"])
        end = start + dt.timedelta(seconds=float(r["duration_s"]))
        act = r["activity"]
        rows.append({"activity": act, "start": start, "end": end,
                     "duration_s": float(r["duration_s"]),
                     "color": color_map.get(act, theme.OTHER_COLOR)})
    rows.sort(key=lambda s: s["start"])

    total_s = sum(s["duration_s"] for s in rows)
    per = {}
    for s in rows:
        per[s["activity"]] = per.get(s["activity"], 0.0) + s["duration_s"]
    activities = [
        {"activity": a, "seconds": v, "share": v / total_s if total_s else 0.0,
         "count": sum(1 for s in rows if s["activity"] == a),
         "color": color_map.get(a, theme.OTHER_COLOR)}
        for a, v in sorted(per.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    items, prev_end = [], None
    for i, s in enumerate(rows):
        if prev_end is not None and (s["start"] - prev_end).total_seconds() >= MIN_GAP_S:
            items.append({"kind": "gap", "seconds": (s["start"] - prev_end).total_seconds()})
        items.append({"kind": "session", "index": i, **s})
        prev_end = s["end"] if prev_end is None else max(prev_end, s["end"])

    first_start = rows[0]["start"]
    last_end = max(s["end"] for s in rows)
    lo, hi = _ribbon_window(day, first_start, last_end, now)
    return {
        "total_s": total_s, "count": len(rows), "sessions": rows,
        "first_start": first_start, "last_end": last_end,
        "activities": activities, "items": items,
        "window": (lo, hi), "ticks": _ticks(lo, hi),
    }


# --------------------------------------------------------------------------- #
# HTML                                                                        #
# --------------------------------------------------------------------------- #
def _esc(s):
    return html.escape(str(s), quote=True)


def _clock(t):
    return t.strftime("%H:%M")


def _big_duration(seconds):
    """'7h 33m' with the unit letters set small (hero figure)."""
    total_min = int(round(seconds / 60.0))
    h, m = divmod(total_min, 60)
    parts = []
    if h:
        parts.append(f'{h}<span class="bb-unit">h</span>')
    if m or not h:
        parts.append(f'{m}<span class="bb-unit">m</span>')
    return " ".join(parts)


def _relative_label(day, today):
    delta = (today - day).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Yesterday"
    if delta == -1:
        return "Tomorrow"
    if 1 < delta < 7:
        return f"{delta} days ago"
    if 7 <= delta < 14:
        return "Last week"
    if 14 <= delta < 60:
        return f"{delta // 7} weeks ago"
    if delta >= 60:
        months = (today.year - day.year) * 12 + today.month - day.month
        return f"{months} months ago" if months < 24 else f"{months // 12} years ago"
    return "Upcoming"


def _date_title(day, today):
    fmt = "%A, %B %-d" if day.year == today.year else "%A, %B %-d, %Y"
    return day.strftime(fmt)


def _pct(t, lo, span_s):
    return max(0.0, min(100.0, (t - lo).total_seconds() / span_s * 100.0))


def _ribbon(summary, now):
    lo, hi = summary["window"]
    span_s = (hi - lo).total_seconds()
    segs = []
    for i, s in enumerate(summary["sessions"]):
        left = _pct(s["start"], lo, span_s)
        width = _pct(s["end"], lo, span_s) - left
        tip = (f'{pretty_name(s["activity"])} · {_clock(s["start"])}–{_clock(s["end"])}'
               f' · {dd.fmt_duration(s["duration_s"], "minute")}')
        segs.append(
            f'<button type="button" class="bb-seg" data-bb-session="{i}" title="{_esc(tip)}" '
            f'aria-label="{_esc(tip)}" '
            f'style="left:{left:.3f}%;width:{width:.3f}%;background:{s["color"]}"></button>'
        )
    if now is not None and lo <= now <= hi:
        segs.append(f'<div class="bb-now" title="now" '
                    f'style="left:{_pct(now, lo, span_s):.3f}%"></div>')
    ticks = "".join(
        f'<span class="bb-tick" style="left:{_pct(t, lo, span_s):.3f}%">{_clock(t)}</span>'
        for t in summary["ticks"]
    )
    grid = "".join(
        f'<i class="bb-grid" style="left:{_pct(t, lo, span_s):.3f}%"></i>'
        for t in summary["ticks"]
    )
    return (f'<div class="bb-ribbon"><div class="bb-track">{grid}{"".join(segs)}</div>'
            f'<div class="bb-ticks">{ticks}</div></div>')


def _breakdown(summary):
    rows = []
    for a in summary["activities"]:
        n = a["count"]
        rows.append(
            '<li class="bb-act">'
            f'<div class="bb-act-head"><span class="bb-dot" style="background:{a["color"]}"></span>'
            f'<span class="bb-act-name">{_esc(pretty_name(a["activity"]))}</span>'
            f'<span class="bb-act-time">{dd.fmt_duration(a["seconds"], "minute")}</span></div>'
            f'<div class="bb-act-bar"><span style="width:{a["share"] * 100:.2f}%;'
            f'background:{a["color"]}"></span></div>'
            f'<div class="bb-act-meta">{round(a["share"] * 100)}% · '
            f'{n} session{"s" if n != 1 else ""}</div>'
            '</li>'
        )
    return f'<ul class="bb-acts">{"".join(rows)}</ul>'


def _session_list(summary):
    out = []
    for it in summary["items"]:
        if it["kind"] == "gap":
            out.append(f'<li class="bb-gap"><span>{dd.fmt_duration(it["seconds"], "minute")} '
                       'break</span></li>')
            continue
        out.append(
            f'<li class="bb-sess" id="bb-session-{it["index"]}" '
            f'style="--c:{it["color"]}">'
            f'<span class="bb-sess-time">{_clock(it["start"])}<span class="bb-sess-end">'
            f'{_clock(it["end"])}</span></span>'
            f'<span class="bb-sess-name">{_esc(pretty_name(it["activity"]))}</span>'
            f'<span class="bb-sess-dur">{dd.fmt_duration(it["duration_s"], "minute")}</span>'
            '</li>'
        )
    return f'<ol class="bb-sessions">{"".join(out)}</ol>'


def render_day_html(summary, day, today, tzname, now=None):
    """The Day view body as one HTML string (``summary`` from `summarize_day`,
    or None for an empty day)."""
    hero_top = (f'<div class="bb-eyebrow">{_esc(_relative_label(day, today))}</div>'
                f'<div class="bb-date">{_esc(_date_title(day, today))}</div>')
    foot = (f'<p class="bb-foot">Day runs 03:00 → 03:00 {_esc(tzname)} · '
            'sessions under a minute and rest breaks are hidden</p>')

    if summary is None:
        return (f'<div class="bb-day"><header class="bb-hero">{hero_top}</header>'
                '<section class="bb-card bb-empty">'
                '<div class="bb-empty-title">Nothing tracked</div>'
                '<div class="bb-empty-sub">No sessions of a minute or longer on this day. '
                'Swipe or use the arrows to browse other days.</div>'
                f'</section>{foot}</div>')

    n_act = len(summary["activities"])
    facts = [
        f'{summary["count"]} session{"s" if summary["count"] != 1 else ""}',
        f'{n_act} activit{"ies" if n_act != 1 else "y"}',
        f'{_clock(summary["first_start"])} – {_clock(summary["last_end"])}',
    ]
    hero = (
        f'<header class="bb-hero">{hero_top}'
        f'<div class="bb-total">{_big_duration(summary["total_s"])}</div>'
        f'<div class="bb-facts">{"".join(f"<span>{_esc(f)}</span>" for f in facts)}</div>'
        '</header>'
    )
    timeline = (f'<section class="bb-card"><div class="bb-label">Timeline</div>'
                f'{_ribbon(summary, now)}</section>')
    breakdown = (f'<section class="bb-card"><div class="bb-label">By activity</div>'
                 f'{_breakdown(summary)}</section>')
    sessions = (f'<section class="bb-card"><div class="bb-label">Sessions</div>'
                f'{_session_list(summary)}</section>')
    return (f'<div class="bb-day">{hero}{timeline}'
            f'<div class="bb-cols">{breakdown}{sessions}</div>{foot}</div>')
