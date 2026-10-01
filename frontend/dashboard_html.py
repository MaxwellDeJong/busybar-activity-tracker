"""Shared HTML building blocks for every dashboard view.

Each view (Day in dashboard_day, Week / Month / Heatmap in dashboard_period) is
rendered as hand-built HTML rather than Plotly: on a phone, hover-driven charts
need a tap to read anything and a squeezed axis turns data into slivers. This
module holds the pieces they share — the hero header, cards, the per-activity
breakdown and the formatting helpers — so the views read as one page family.

Markup only: the ``.bb-*`` styles live in dashboard.py's global CSS and are mixed
from ``currentColor``, so every page follows Streamlit's light/dark theme. Any
element carrying ``data-bb-day="<iso date>"`` is a drill target: dashboard.py
mounts the page through a component that turns a tap on it into "open that day".
"""
from __future__ import annotations

import html

import dashboard_data as dd
import dashboard_theme as theme


def esc(s):
    return html.escape(str(s), quote=True)


def plural(n, word, many=None):
    """'1 session' / '3 sessions'; ``many`` for irregular plurals ('activities')."""
    return f"{n} {word if n == 1 else (many or word + 's')}"


def pretty_name(activity):
    """'tech_reading' -> 'Tech reading' for display; the raw key stays the color key."""
    text = str(activity).replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else str(activity)


def big_duration(seconds):
    """'7h 33m' with the unit letters set small (hero figure)."""
    total_min = int(round(seconds / 60.0))
    h, m = divmod(total_min, 60)
    parts = []
    if h:
        parts.append(f'{h}<span class="bb-unit">h</span>')
    if m or not h:
        parts.append(f'{m}<span class="bb-unit">m</span>')
    return " ".join(parts)


def short_duration(seconds):
    """Narrow label for tight cells: '7.6h' from an hour up, else '45m'."""
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    return f"{int(round(seconds / 60))}m"


def activity_totals(pairs, color_map):
    """Per-activity totals from ``(activity, seconds)`` pairs (one per session).

    Returns dicts with ``activity``, ``seconds``, ``share`` of the grand total,
    session ``count`` and ``color``, largest first (ties by name).
    """
    secs, counts = {}, {}
    for act, s in pairs:
        secs[act] = secs.get(act, 0.0) + float(s)
        counts[act] = counts.get(act, 0) + 1
    total = sum(secs.values())
    return [
        {"activity": a, "seconds": v, "share": v / total if total else 0.0,
         "count": counts[a], "color": color_map.get(a, theme.OTHER_COLOR)}
        for a, v in sorted(secs.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def hero(eyebrow, title, total_s=None, facts=()):
    """Eyebrow + title, then (when there is data) the big total and a facts line."""
    out = (f'<header class="bb-hero"><div class="bb-eyebrow">{esc(eyebrow)}</div>'
           f'<div class="bb-date">{esc(title)}</div>')
    if total_s is not None:
        out += f'<div class="bb-total">{big_duration(total_s)}</div>'
    if facts:
        spans = "".join(f"<span>{esc(f)}</span>" for f in facts)
        out += f'<div class="bb-facts"><div>{spans}</div></div>'
    return out + "</header>"


def card(label, body, cls="", hint=None):
    extra = f" {cls}" if cls else ""
    tip = f'<span class="bb-hint">{esc(hint)}</span>' if hint else ""
    return (f'<section class="bb-card{extra}"><div class="bb-label">{esc(label)}{tip}</div>'
            f'{body}</section>')


def empty_card(sub):
    return ('<section class="bb-card bb-empty">'
            '<div class="bb-empty-title">Nothing tracked</div>'
            f'<div class="bb-empty-sub">{esc(sub)}</div></section>')


def footnote(tzname):
    return (f'<p class="bb-foot">Day runs 03:00 → 03:00 {esc(tzname)} · '
            'sessions under a minute and rest breaks are hidden</p>')


def breakdown(activities):
    """The 'By activity' list: name, total, share bar, share % and session count."""
    rows = []
    for a in activities:
        rows.append(
            '<li class="bb-act">'
            f'<div class="bb-act-head"><span class="bb-dot" style="background:{a["color"]}"></span>'
            f'<span class="bb-act-name">{esc(pretty_name(a["activity"]))}</span>'
            f'<span class="bb-act-time">{dd.fmt_duration(a["seconds"], "minute")}</span></div>'
            f'<div class="bb-act-bar"><span style="width:{a["share"] * 100:.2f}%;'
            f'background:{a["color"]}"></span></div>'
            f'<div class="bb-act-meta">{round(a["share"] * 100)}% · '
            f'{plural(a["count"], "session")}</div>'
            '</li>'
        )
    return f'<ul class="bb-acts">{"".join(rows)}</ul>'


def page(body, cls=""):
    extra = f" {cls}" if cls else ""
    return f'<div class="bb-page{extra}">{body}</div>'
