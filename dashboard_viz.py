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

# Logical day window: [day 03:00, next day 03:00).
DAY_START_HOUR = dd.DAY_CUTOFF_HOUR


def _day_window(day):
    start = dt.datetime.combine(day, dt.time(DAY_START_HOUR, 0))
    return start, start + dt.timedelta(days=1)


def build_day_timeline(sessions, day, color_map, mode="light"):
    """A single-lane time-of-day ribbon: one bar per session at its real clock
    position, colored by activity. x-axis spans the full logical day (03:00->03:00).
    """
    win_start, win_end = _day_window(day)
    fig = go.Figure()

    # One trace per activity so the legend carries identity (color is not alone).
    for activity in sorted(sessions["activity"].unique()):
        rows = sessions[sessions["activity"] == activity]
        starts, widths, custom = [], [], []
        for _, r in rows.iterrows():
            s = r["start_local"]
            if hasattr(s, "to_pydatetime"):                # pandas Timestamp -> datetime
                s = s.to_pydatetime()
            s = s.replace(tzinfo=None)                      # already local; drop tz for plotting
            starts.append(s)
            widths.append(r["duration_s"] * 1000.0)         # bar width in ms
            custom.append((activity, s.strftime("%H:%M:%S"), dd.fmt_duration(r["duration_s"])))
        fig.add_bar(
            name=activity, y=["day"] * len(rows), x=widths, base=starts,
            orientation="h", marker=dict(color=color_map.get(activity, theme.OTHER_COLOR),
                                         line=dict(width=0)),
            customdata=custom, offsetgroup="day", width=0.6,
            hovertemplate="<b>%{customdata[0]}</b><br>start %{customdata[1]}"
                          "<br>%{customdata[2]}<extra></extra>",
        )

    fig.update_layout(
        barmode="overlay", height=150, showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
    )
    fig.update_xaxes(
        type="date", range=[win_start, win_end],
        tickformat="%H:%M", dtick=3600000 * 3,   # a tick every 3 hours
    )
    fig.update_yaxes(showticklabels=False, showgrid=False, title=None)
    return theme.apply_theme(fig, mode)


def build_activity_totals(sessions, color_map, mode="light"):
    """Horizontal bar of total minutes per activity for the day, largest on top,
    with direct duration labels (identity never rests on color alone)."""
    ink = theme.INK[mode]
    totals = sessions.groupby("activity")["duration_s"].sum().sort_values()  # asc -> largest on top
    labels = [dd.fmt_duration(v) for v in totals.values]
    colors = [color_map.get(a, theme.OTHER_COLOR) for a in totals.index]

    fig = go.Figure(go.Bar(
        x=(totals.values / 60.0), y=list(totals.index), orientation="h",
        marker=dict(color=colors, line=dict(width=0)),
        text=labels, textposition="outside",
        textfont=dict(color=ink["secondary"]),
        hovertemplate="<b>%{y}</b><br>%{x:.1f} min<extra></extra>",
    ))
    fig.update_layout(
        height=max(140, 46 * len(totals)), showlegend=False,
    )
    fig.update_xaxes(title="minutes", rangemode="tozero")
    fig.update_yaxes(title=None, ticksuffix="  ")
    return theme.apply_theme(fig, mode)


# --------------------------------------------------------------------------- #
# Stacked daily bars (Week / Month)                                           #
# --------------------------------------------------------------------------- #
def build_stacked_daily(pivot, color_map, mode="light", xlabel_fmt="%-d"):
    """Stacked daily bars: one bar per day in ``pivot.index``, stacked by activity.

    ``pivot`` is a day x activity frame of **minutes** (see `range_by_activity`),
    already sliced to the target week/month with all-zero activity columns dropped.
    Both the Week and Month views share this form. Each bar segment carries its ISO
    day in ``customdata[0]`` so a click can drill into the Day view. When 4 or fewer
    activities are visible they are also direct-labeled on their tallest day, so
    identity is never carried by color alone; the legend is always present.
    """
    ink = theme.INK[mode]
    days = list(pivot.index)
    labels = [d.strftime(xlabel_fmt) for d in days]
    activities = list(pivot.columns)          # alphabetical -> stable stack + colors
    direct = len(activities) <= 4

    fig = go.Figure()
    for activity in activities:
        vals = [float(v) for v in pivot[activity].values]
        customdata = [
            [d.isoformat(), d.strftime("%a %-m/%-d"), dd.fmt_duration(v * 60.0)]
            for d, v in zip(days, vals)
        ]
        text = None
        if direct and vals:
            top = max(range(len(vals)), key=lambda i: vals[i])   # this activity's peak day
            text = [activity if (i == top and vals[i] > 0) else "" for i in range(len(vals))]
        fig.add_bar(
            name=activity, x=labels, y=vals,
            marker=dict(color=color_map.get(activity, theme.OTHER_COLOR),
                        line=dict(width=2, color=ink["surface"])),   # 2px surface gap
            customdata=customdata,
            text=text, textposition="inside", insidetextanchor="middle",
            textfont=dict(color="#ffffff"),
            hovertemplate="<b>%{fullData.name}</b><br>%{customdata[1]}"
                          "<br>%{customdata[2]}<extra></extra>",
        )

    fig.update_layout(
        barmode="stack", bargap=0.28, height=380, showlegend=True,
        uniformtext=dict(mode="hide", minsize=8),   # drop direct labels that don't fit
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
    )
    fig.update_xaxes(title=None, type="category", showgrid=False)
    fig.update_yaxes(title="minutes", rangemode="tozero")
    return theme.apply_theme(fig, mode)


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


def build_heatmap(values, hover_text, start, end, mode="light", unit="hr"):
    """GitHub-contributions heatmap: columns = weeks, rows = weekday (Mon top).

    `values` is a date->float mapping in **minutes** (the selected measure) for
    every day in [start, end]; in-range days with no activity are 0 (near-zero
    step). `hover_text` is date->html string.

    Rendered as square **scatter markers** (one per day) rather than a Heatmap
    trace: markers emit point-selection events, so cells are clickable for
    drill-down (Heatmap traces are not selectable). Marker color encodes magnitude
    on the single-hue blue ramp; color/colorbar are shown in hours for readability.
    """
    ink = theme.INK[mode]
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
    # Sequential ramp must recede toward the mode's surface: on light, near-zero is
    # the lightest step; on dark, near-zero is the darkest (near-surface) step and
    # high values brighten. So the ramp direction is mode-aware, not an auto-flip.
    ramp = theme.SEQUENTIAL_BLUE if mode == "light" else list(reversed(theme.SEQUENTIAL_BLUE))
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
            symbol="square", size=15,
            color=colors, colorscale=colorscale, cmin=0, cmax=cmax,
            showscale=True, line=dict(width=0),
            colorbar=dict(title=unit, outlinewidth=0, thickness=12,
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
    fig = theme.apply_theme(fig, mode)
    # Calendar look: no visible axis lines.
    fig.update_xaxes(linecolor="rgba(0,0,0,0)")
    fig.update_yaxes(linecolor="rgba(0,0,0,0)")
    return fig
