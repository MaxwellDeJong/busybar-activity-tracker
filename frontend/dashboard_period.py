"""Week, Month and Heatmap views: summary shaping + HTML (no Streamlit).

The multi-day siblings of dashboard_day, built from the same dashboard_html pieces
so every view reads as one page family. Each lays out for a phone first:

  * Week    — seven stacked day columns (activity mix, height = day total).
  * Month   — a calendar grid; each cell shows the day's hours and a strip whose
              width is the day's total relative to the month's best day.
  * Heatmap — small-multiple month calendars, newest first, two across on a
              phone, shaded in five steps relative to the busiest day in range.

Every day cell carries ``data-bb-day`` so a tap opens it in the Day view.
`summarize_period` / `summarize_heatmap` are pure data (unit-tested); the
``render_*`` functions turn them into markup.
"""
from __future__ import annotations

import calendar
import datetime as dt
import math

import dashboard_data as dd
import dashboard_html as h
import dashboard_theme as theme

_WEEKDAY_LETTERS = ["M", "T", "W", "T", "F", "S", "S"]
# Heatmap shading: level 0 is "no time"; 1..LEVELS split (0, max] evenly.
LEVELS = 4


def _days(start, end):
    d = start
    while d <= end:
        yield d
        d += dt.timedelta(days=1)


def summarize_period(sessions, start, end, color_map, today):
    """Shape the sessions of logical days [start, end] for the Week/Month views.

    ``sessions`` is `dashboard_data.range_sessions` output. Returns a dict with
    ``days`` (one entry per calendar day: ``date``, ``total_s``, ``segments`` as
    (activity, seconds, color) in stable alphabetical stack order, ``future``),
    ``total_s``, ``active_days``, ``elapsed_days`` (days not in the future),
    ``best`` (the busiest day entry, or None), ``max_s`` and ``activities``.
    """
    by_day = {d: {} for d in _days(start, end)}
    pairs = []
    if sessions is not None and not sessions.empty:
        for act, day, secs in zip(sessions["activity"], sessions["logical_day"],
                                  sessions["duration_s"]):
            if day in by_day:
                by_day[day][act] = by_day[day].get(act, 0.0) + float(secs)
                pairs.append((act, float(secs)))

    days = []
    for d, acts in by_day.items():
        days.append({
            "date": d, "total_s": sum(acts.values()), "future": d > today,
            "segments": [(a, acts[a], color_map.get(a, theme.OTHER_COLOR))
                         for a in sorted(acts)],
        })
    active = [d for d in days if d["total_s"] > 0]
    total_s = sum(d["total_s"] for d in days)
    return {
        "start": start, "end": end, "days": days, "total_s": total_s,
        "active_days": len(active),
        "elapsed_days": sum(1 for d in days if not d["future"]),
        "best": max(active, key=lambda d: d["total_s"]) if active else None,
        "max_s": max((d["total_s"] for d in days), default=0.0),
        "activities": h.activity_totals(pairs, color_map),
    }


def _period_facts(s, span_label):
    facts = [f'{s["active_days"]} of {s["elapsed_days"]} days active']
    if s["active_days"]:
        avg = s["total_s"] / s["active_days"]
        facts.append(f'{dd.fmt_duration(avg, "minute")} avg per active day')
    if s["best"] is not None and span_label == "month":
        b = s["best"]
        facts.append(f'Best {b["date"]:%b %-d} · {dd.fmt_duration(b["total_s"], "minute")}')
    elif s["activities"]:
        facts.append(h.plural(len(s["activities"]), "activity", "activities"))
    return facts


def _day_label(d):
    return f"{d:%A, %B %-d}"


def _stack(segments):
    """A flex stack of activity segments sized by their seconds."""
    return "".join(f'<i style="flex-grow:{secs:.0f};background:{color}"></i>'
                   for _, secs, color in segments)


# --------------------------------------------------------------------------- #
# Week                                                                        #
# --------------------------------------------------------------------------- #
def week_labels(monday, today):
    """(eyebrow, title) for the week starting ``monday``."""
    this_monday = today - dt.timedelta(days=today.weekday())
    n = (this_monday - monday).days // 7
    eyebrow = {0: "This week", 1: "Last week"}.get(n, f"{n} weeks ago" if n > 1 else "Upcoming")
    sunday = monday + dt.timedelta(days=6)
    if monday.year != today.year or sunday.year != today.year:
        title = f"{monday:%b %-d, %Y} – {sunday:%b %-d, %Y}"
    elif monday.month == sunday.month:
        title = f"{monday:%B %-d} – {sunday:%-d}"
    else:
        title = f"{monday:%b %-d} – {sunday:%b %-d}"
    return eyebrow, title


def _week_columns(s, today):
    peak = s["max_s"] or 1.0
    cols = []
    for d in s["days"]:
        date, total = d["date"], d["total_s"]
        cls = ["bb-wcol"]
        if date == today:
            cls.append("is-today")
        if d["future"]:
            cls.append("is-future")
        label = f'{_day_label(date)}: {dd.fmt_duration(total, "minute") if total else "nothing tracked"}'
        bar = ""
        if total:
            bar = (f'<span class="bb-wbar" style="height:{max(total / peak * 100, 2):.1f}%">'
                   f'{_stack(d["segments"])}</span>')
        attrs = 'disabled' if d["future"] else f'data-bb-day="{date.isoformat()}"'
        cols.append(
            f'<button type="button" class="{" ".join(cls)}" {attrs} aria-label="{h.esc(label)}">'
            f'<span class="bb-wval">{h.short_duration(total) if total else "–"}</span>'
            f'<span class="bb-wtrack">{bar}</span>'
            f'<span class="bb-wday">{date:%a}</span><span class="bb-wdate">{date.day}</span>'
            '</button>'
        )
    return f'<div class="bb-week">{"".join(cols)}</div>'


def render_week_html(s, today, tzname):
    eyebrow, title = week_labels(s["start"], today)
    if not s["total_s"]:
        return h.page(h.hero(eyebrow, title)
                      + h.empty_card("No sessions of a minute or longer this week. "
                                     "Swipe or use the arrows to browse other weeks.")
                      + h.footnote(tzname))
    return h.page(
        h.hero(eyebrow, title, s["total_s"], _period_facts(s, "week"))
        + h.card("Each day", _week_columns(s, today), hint="Tap a day to open it")
        + h.card("By activity", h.breakdown(s["activities"]))
        + h.footnote(tzname))


# --------------------------------------------------------------------------- #
# Month                                                                       #
# --------------------------------------------------------------------------- #
def month_labels(first, today):
    n = (today.year - first.year) * 12 + today.month - first.month
    eyebrow = {0: "This month", 1: "Last month"}.get(n, f"{n} months ago" if n > 1 else "Upcoming")
    return eyebrow, f"{first:%B %Y}"


def _weekday_head():
    return ('<div class="bb-cal-head">'
            + "".join(f"<span>{w}</span>" for w in _WEEKDAY_LETTERS) + "</div>")


def _month_calendar(s, today):
    peak = s["max_s"] or 1.0
    cells = ['<span class="bb-cal-pad"></span>'] * s["start"].weekday()
    for d in s["days"]:
        date, total = d["date"], d["total_s"]
        cls = ["bb-cal-day"]
        if date == today:
            cls.append("is-today")
        if d["future"]:
            cls.append("is-future")
        if not total:
            cls.append("is-empty")
        label = f'{_day_label(date)}: {dd.fmt_duration(total, "minute") if total else "nothing tracked"}'
        inner = f'<span class="bb-cal-num">{date.day}</span>'
        if total:
            inner += (f'<span class="bb-cal-val">{h.short_duration(total)}</span>'
                      f'<span class="bb-cal-strip" style="width:{max(total / peak * 100, 8):.1f}%">'
                      f'{_stack(d["segments"])}</span>')
        attrs = 'disabled' if d["future"] else f'data-bb-day="{date.isoformat()}"'
        cells.append(f'<button type="button" class="{" ".join(cls)}" {attrs} '
                     f'aria-label="{h.esc(label)}">{inner}</button>')
    return f'<div class="bb-cal">{_weekday_head()}<div class="bb-cal-grid">{"".join(cells)}</div></div>'


def render_month_html(s, today, tzname):
    eyebrow, title = month_labels(s["start"], today)
    if not s["total_s"]:
        return h.page(h.hero(eyebrow, title)
                      + h.empty_card("No sessions of a minute or longer this month. "
                                     "Swipe or use the arrows to browse other months.")
                      + h.footnote(tzname))
    return h.page(
        h.hero(eyebrow, title, s["total_s"], _period_facts(s, "month"))
        + h.card("Calendar", _month_calendar(s, today), hint="Tap a day to open it")
        + h.card("By activity", h.breakdown(s["activities"]))
        + h.footnote(tzname))


# --------------------------------------------------------------------------- #
# Heatmap                                                                     #
# --------------------------------------------------------------------------- #
def level(seconds, peak):
    """0 for no time, else 1..LEVELS by share of the busiest day."""
    if seconds <= 0 or peak <= 0:
        return 0
    return min(LEVELS, max(1, math.ceil(seconds / peak * LEVELS)))


def _streaks(values, start, end, today):
    """(current, longest) runs of consecutive active days. The current streak may
    end yesterday, so an untracked-yet today doesn't reset it."""
    longest = run = 0
    for d in _days(start, end):
        run = run + 1 if values.get(d, 0) > 0 else 0
        longest = max(longest, run)
    current, d = 0, min(end, today)
    if values.get(d, 0) <= 0:
        d -= dt.timedelta(days=1)
    while d >= start and values.get(d, 0) > 0:
        current += 1
        d -= dt.timedelta(days=1)
    return current, longest


def summarize_heatmap(sessions, start, end, color_map, today, activity=None):
    """Per-day seconds over [start, end] for every activity, or just ``activity``.

    Returns ``values`` {date: seconds}, ``peak``, ``total_s``, ``active_days``,
    ``best`` (date or None), ``streak`` / ``longest`` (consecutive active days),
    ``color`` (the shading hue), ``activities`` (breakdown over the range) and
    ``months`` — (year, month) pairs touching the range, newest first.
    """
    values, pairs = {}, []
    if sessions is not None and not sessions.empty:
        for act, day, secs in zip(sessions["activity"], sessions["logical_day"],
                                  sessions["duration_s"]):
            if start <= day <= end:
                pairs.append((act, float(secs)))
                if activity is None or act == activity:
                    values[day] = values.get(day, 0.0) + float(secs)
    active = {d: v for d, v in values.items() if v > 0}
    streak, longest = _streaks(values, start, end, today)
    months, y, m = [], end.year, end.month
    while (y, m) >= (start.year, start.month):
        months.append((y, m))
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    color = (theme.CATEGORICAL[0] if activity is None
             else color_map.get(activity, theme.OTHER_COLOR))
    return {
        "start": start, "end": end, "values": values,
        "peak": max(active.values(), default=0.0),
        "total_s": sum(active.values()), "active_days": len(active),
        "best": max(active, key=active.get) if active else None,
        "streak": streak, "longest": longest, "color": color,
        "activities": h.activity_totals(pairs, color_map), "months": months,
    }


def _hm_month(year, month, s, today):
    first = dt.date(year, month, 1)
    n_days = calendar.monthrange(year, month)[1]
    cells = ['<span class="bb-hm-cell is-pad"></span>'] * first.weekday()
    for day in range(1, n_days + 1):
        date = dt.date(year, month, day)
        if not (s["start"] <= date <= s["end"]):
            cells.append('<span class="bb-hm-cell is-out"></span>')
            continue
        secs = s["values"].get(date, 0.0)
        label = f'{_day_label(date)}: {dd.fmt_duration(secs, "minute") if secs else "nothing tracked"}'
        today_cls = " is-today" if date == today else ""
        cells.append(f'<button type="button" class="bb-hm-cell lv-{level(secs, s["peak"])}{today_cls}" '
                     f'data-bb-day="{date.isoformat()}" title="{h.esc(label)}" '
                     f'aria-label="{h.esc(label)}"></button>')
    show_year = month == 1 or (year, month) == s["months"][0] or (year, month) == s["months"][-1]
    title = f"{first:%b %Y}" if show_year else f"{first:%b}"
    return (f'<div class="bb-hm-month"><div class="bb-hm-title">{title}</div>'
            f'<div class="bb-hm-grid">{"".join(cells)}</div></div>')


def _hm_legend(s):
    swatches = "".join(f'<span class="bb-hm-cell lv-{i}"></span>' for i in range(LEVELS + 1))
    top = f' · darkest ≈ {dd.fmt_duration(s["peak"], "minute")}' if s["peak"] else ""
    return (f'<div class="bb-hm-legend"><span>Less</span>{swatches}<span>More{top}</span></div>')


def render_heatmap_html(s, today, tzname, range_label, measure_label):
    title = f'{s["start"]:%b %-d, %Y} – {s["end"]:%b %-d, %Y}'
    eyebrow = range_label if measure_label is None else f"{range_label} · {measure_label}"
    grid = (f'<div style="--c:{s["color"]}"><div class="bb-hm">'
            + "".join(_hm_month(y, m, s, today) for y, m in s["months"])
            + f'</div>{_hm_legend(s)}</div>')
    if not s["total_s"]:
        return h.page(h.hero(eyebrow, title)
                      + h.empty_card("No sessions of a minute or longer in this range.")
                      + h.card("Calendar", grid) + h.footnote(tzname))
    facts = [f'{s["active_days"]} active days',
             f'{dd.fmt_duration(s["total_s"] / s["active_days"], "minute")} avg per active day',
             f'Best {s["best"]:%b %-d} · {dd.fmt_duration(s["peak"], "minute")}',
             f'Streak {h.plural(s["streak"], "day")} (best {s["longest"]})']
    body = (h.hero(eyebrow, title, s["total_s"], facts)
            + h.card("Calendar", grid, hint="Tap a day to open it"))
    if measure_label is None:
        body += h.card("By activity", h.breakdown(s["activities"]))
    return h.page(body + h.footnote(tzname))
