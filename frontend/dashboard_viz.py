"""Pure Plotly figure builders for the dashboard.

These functions take a prepared sessions DataFrame (see dashboard_data) plus a
stable {activity: color} map and return a Plotly Figure. They import no Streamlit,
so they can be smoke-tested standalone.
"""
from __future__ import annotations

import datetime as dt

import plotly.graph_objects as go

import dashboard_data as dd
import dashboard_theme as theme

# Above this many minutes a value axis reads more cleanly in hours than in minutes.
_HOURS_THRESHOLD_MIN = 120


def _value_axis(max_minutes):
    """Pick the value-axis unit from the largest value it must show.

    Returns ``(title, divisor)``: divide a minutes value by ``divisor`` to get the
    plotted number, and label the axis ``title``. Small ranges stay in minutes;
    multi-hour ranges switch to hours so the ticks are 0/2/4/… not 0/120/240/….
    """
    if max_minutes >= _HOURS_THRESHOLD_MIN:
        return "hours", 60.0
    return "minutes", 1.0


def build_activity_totals(sessions, color_map):
    """Horizontal bar of total minutes per activity, largest on top,
    with direct duration labels (identity never rests on color alone)."""
    ink = theme.INK
    totals = sessions.groupby("activity")["duration_s"].sum().sort_values()  # asc -> largest on top
    minutes = totals.values / 60.0
    unit, div = _value_axis(minutes.max() if len(minutes) else 0.0)
    xs = minutes / div
    labels = [dd.fmt_duration(v, "minute") for v in totals.values]     # nearest-minute labels
    colors = [color_map.get(a, theme.OTHER_COLOR) for a in totals.index]

    fig = go.Figure(go.Bar(
        x=xs, y=list(totals.index), orientation="h",
        marker=dict(color=colors, line=dict(width=0), cornerradius=4),  # rounded tip
        width=0.62,                                    # cap thickness; leave the slot air
        text=labels, textposition="outside", cliponaxis=False,
        textfont=dict(color=ink["secondary"], size=12),
        customdata=labels,
        hovertemplate="<b>%{y}</b><br>%{customdata}<extra></extra>",
    ))
    fig.update_layout(
        height=max(140, 46 * len(totals)), showlegend=False, bargap=0.4,
    )
    # Headroom on the right so the outside value labels never clip.
    fig.update_xaxes(title=unit, rangemode="tozero",
                     range=[0, xs.max() * 1.18 if len(xs) else 1])
    fig.update_yaxes(title=None, ticksuffix="  ")
    return theme.apply_theme(fig, margin=dict(l=8, r=64, t=24, b=8))


# --------------------------------------------------------------------------- #
# Stacked daily bars (Week / Month)                                           #
# --------------------------------------------------------------------------- #
def build_stacked_daily(pivot, color_map, xlabel_fmt="%-d"):
    """Stacked daily bars: one bar per day in ``pivot.index``, stacked by activity.

    ``pivot`` is a day x activity frame of **minutes** (see `range_by_activity`),
    already sliced to the target week/month with all-zero activity columns dropped.
    Both the Week and Month views share this form. Each bar segment carries its ISO
    day in ``customdata[0]`` so a click can drill into the Day view. When 4 or fewer
    activities are visible they are also direct-labeled on their tallest day, so
    identity is never carried by color alone; the legend is always present.
    """
    ink = theme.INK
    days = list(pivot.index)
    labels = [d.strftime(xlabel_fmt) for d in days]
    activities = list(pivot.columns)          # alphabetical -> stable stack + colors
    direct = len(activities) <= 4

    # Unit follows the tallest stacked day (a day's total across activities).
    day_totals = pivot.sum(axis=1)
    unit, div = _value_axis(float(day_totals.max()) if len(day_totals) else 0.0)

    fig = go.Figure()
    for activity in activities:
        vals = [float(v) for v in pivot[activity].values]
        customdata = [
            [d.isoformat(), d.strftime("%a %-m/%-d"), dd.fmt_duration(v * 60.0, "minute")]
            for d, v in zip(days, vals)
        ]
        text = None
        if direct and vals:
            top = max(range(len(vals)), key=lambda i: vals[i])   # this activity's peak day
            text = [activity if (i == top and vals[i] > 0) else "" for i in range(len(vals))]
        color = color_map.get(activity, theme.OTHER_COLOR)
        fig.add_bar(
            name=activity, x=labels, y=[v / div for v in vals],   # minutes -> unit
            marker=dict(color=color,
                        line=dict(width=2, color=ink["surface"])),   # 2px surface gap
            customdata=customdata,
            text=text, textposition="inside", insidetextanchor="middle",
            textfont=dict(color=theme.text_on(color)),   # ink vs. white by fill luminance
            hovertemplate="<b>%{fullData.name}</b><br>%{customdata[1]}"
                          "<br>%{customdata[2]}<extra></extra>",
        )

    fig.update_layout(
        barmode="stack", bargap=0.34, barcornerradius=3, height=380,  # rounded stack top
        showlegend=True,
        uniformtext=dict(mode="hide", minsize=8),   # drop direct labels that don't fit
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
    )
    fig.update_xaxes(title=None, type="category", showgrid=False)
    fig.update_yaxes(title=unit, rangemode="tozero")
    return theme.apply_theme(fig)


# --------------------------------------------------------------------------- #
# Heatmap (overview)                                                          #
# --------------------------------------------------------------------------- #
_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def calendar_grid(start, end):
    """Map a date range onto a GitHub-style grid.

    Returns (grid_start, num_weeks) where grid_start is the Monday on/before
    `start` and columns are consecutive weeks. Cell (row, col) with row = weekday
    (Mon=0) and col = weeks since grid_start holds one calendar day.
    """
    grid_start = start - dt.timedelta(days=start.weekday())   # Monday of start's week
    num_weeks = (end - grid_start).days // 7 + 1
    return grid_start, num_weeks


def date_from_cell(grid_start, col, row):
    """Inverse of the grid mapping: (week column, weekday row) -> calendar date.

    `grid_start` is the Monday returned by `calendar_grid`. Used to turn a clicked
    heatmap cell back into the logical day it represents.
    """
    return grid_start + dt.timedelta(days=7 * col + row)


def build_heatmap(values, hover_text, start, end, unit="hr"):
    """GitHub-contributions heatmap: columns = weeks, rows = weekday (Mon top).

    `values` is a date->float mapping in **minutes** (the selected measure) for
    every day in [start, end]; in-range days with no activity are 0 (near-zero
    step). `hover_text` is date->html string.

    Rendered as square **scatter markers** (one per day) rather than a Heatmap
    trace: markers emit point-selection events, so cells are clickable for
    drill-down (Heatmap traces are not selectable). Marker color encodes magnitude
    on the single-hue blue ramp; color/colorbar are shown in hours for readability.
    """
    ink = theme.INK
    grid_start, num_weeks = calendar_grid(start, end)

    xs, ys, colors, texts = [], [], [], []
    day = start
    while day <= end:
        xs.append((day - grid_start).days // 7)      # week column
        ys.append(day.weekday())                     # weekday row (Mon=0)
        colors.append(float(values.get(day, 0.0)) / 60.0)   # minutes -> hours
        texts.append(hover_text.get(day, day.strftime("%a %-m/%-d/%Y")))
        day += dt.timedelta(days=1)

    cmax = max(colors, default=0.0) or 1.0
    # Sequential ramp recedes toward the surface: near-zero is the lightest step,
    # busiest days the darkest.
    ramp = theme.SEQUENTIAL_BLUE
    n = len(ramp)
    colorscale = [[i / (n - 1), c] for i, c in enumerate(ramp)]

    # Month labels at the column where the month first changes.
    tickvals, ticktext, prev_month = [], [], None
    for col in range(num_weeks):
        mon = grid_start + dt.timedelta(days=7 * col)
        if mon.month != prev_month:
            tickvals.append(col)
            ticktext.append(mon.strftime("%b"))
            prev_month = mon.month

    fig = go.Figure(go.Scatter(
        x=xs, y=ys, mode="markers", customdata=texts,
        marker=dict(
            symbol="square", size=16,
            color=colors, colorscale=colorscale, cmin=0, cmax=cmax,
            showscale=True,
            line=dict(width=3, color=ink["surface"]),   # surface ring -> calendar gaps
            colorbar=dict(title=dict(text=unit, font=dict(color=ink["muted"])),
                          outlinewidth=0, thickness=12, len=0.9,
                          tickfont=dict(color=ink["muted"])),
        ),
        hovertemplate="%{customdata}<extra></extra>",
    ))
    fig.update_layout(height=210, showlegend=False)
    fig.update_xaxes(tickvals=tickvals, ticktext=ticktext, showgrid=False,
                     zeroline=False, ticks="", side="top",
                     range=[-0.6, num_weeks - 0.4])
    fig.update_yaxes(tickvals=list(range(7)), ticktext=_WEEKDAYS, showgrid=False,
                     zeroline=False, ticks="", range=[6.6, -0.6])   # Mon (row 0) on top
    fig = theme.apply_theme(fig)
    # Calendar look: no visible axis lines.
    fig.update_xaxes(linecolor="rgba(0,0,0,0)")
    fig.update_yaxes(linecolor="rgba(0,0,0,0)")
    return fig
