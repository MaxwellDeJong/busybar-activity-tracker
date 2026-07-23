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
