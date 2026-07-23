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


def build_heatmap(values, hover_text, start, end, mode="light", unit="hr"):
    """GitHub-contributions heatmap: columns = weeks, rows = weekday (Mon top).

    `values` is a date->float mapping in **minutes** (the selected measure) for
    every day in [start, end]; in-range days with no activity are 0 (near-zero
    step), while padding cells outside the range are blank. `hover_text` is
    date->html string. Cells are colored on the single-hue blue sequential ramp;
    the z-values (and colorbar) are shown in hours for readability.
    """
    ink = theme.INK[mode]
    grid_start, num_weeks = calendar_grid(start, end)

    z = [[None] * num_weeks for _ in range(7)]
    text = [[""] * num_weeks for _ in range(7)]
    day = start
    while day <= end:
        col = (day - grid_start).days // 7
        row = day.weekday()
        z[row][col] = float(values.get(day, 0.0)) / 60.0     # minutes -> hours for coloring
        text[row][col] = hover_text.get(day, day.strftime("%a %-m/%-d/%Y"))
        day += dt.timedelta(days=1)

    zmax = max((v for r in z for v in r if v is not None), default=0.0) or 1.0
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

    fig = go.Figure(go.Heatmap(
        z=z, text=text, x=list(range(num_weeks)), y=list(range(7)),
        colorscale=colorscale, zmin=0, zmax=zmax, xgap=3, ygap=3,
        hovertemplate="%{text}<extra></extra>",
        colorbar=dict(title=unit, outlinewidth=0, thickness=12,
                      tickfont=dict(color=ink["muted"])),
    ))
    fig.update_layout(height=210, showlegend=False)
    fig.update_xaxes(tickvals=tickvals, ticktext=ticktext, showgrid=False,
                     zeroline=False, ticks="", side="top")
    fig.update_yaxes(tickvals=list(range(7)), ticktext=_WEEKDAYS, showgrid=False,
                     zeroline=False, ticks="", autorange="reversed")
    fig = theme.apply_theme(fig, mode)
    # Heatmap has no bars/lines; keep axis lines invisible for the calendar look.
    fig.update_xaxes(linecolor="rgba(0,0,0,0)")
    fig.update_yaxes(linecolor="rgba(0,0,0,0)")
    return fig
