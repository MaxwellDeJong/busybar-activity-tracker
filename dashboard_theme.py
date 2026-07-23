"""Palette, activity->color assignment, and Plotly theming for the dashboard.

The palette is the validated data-viz reference instance: a fixed-order categorical
ramp (assigned to activities by alphabetical name so a given activity always keeps
its color), a single-hue blue sequential ramp for the heatmap, and matched
light/dark ink + surface tokens. Colors are chosen last and were validated with the
palette checker — do not reorder the categorical slots without re-validating.
"""
from __future__ import annotations

# Fixed categorical slot order (never cycled). Slot i -> the i-th activity by name.
CATEGORICAL = {
    "light": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
              "#e87ba4", "#008300", "#4a3aa7", "#e34948"],
    "dark":  ["#3987e5", "#d95926", "#199e70", "#c98500",
              "#d55181", "#008300", "#9085e9", "#e66767"],
}
MAX_SLOTS = len(CATEGORICAL["light"])          # 8; activities past this fold to "Other"
OTHER_COLOR = "#898781"                          # muted grey, shared in both modes

# Single-hue blue sequential ramp (heatmap magnitude), light->dark, steps 100..700.
SEQUENTIAL_BLUE = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5",
                   "#256abf", "#184f95", "#0d366b"]

# Chart chrome / ink tokens per mode.
INK = {
    "light": {
        "surface": "#fcfcfb", "plane": "#f9f9f7", "primary": "#0b0b0b",
        "secondary": "#52514e", "muted": "#898781", "grid": "#e1e0d9",
        "baseline": "#c3c2b7",
    },
    "dark": {
        "surface": "#1a1a19", "plane": "#0d0d0d", "primary": "#ffffff",
        "secondary": "#c3c2b7", "muted": "#898781", "grid": "#2c2c2a",
        "baseline": "#383835",
    },
}


def activity_colors(activities, mode="light"):
    """Map activities to fixed palette slots by alphabetical name.

    Stable: a given activity always gets the same color regardless of which other
    activities are present. Activities past slot 8 (alphabetically) share the muted
    "Other" grey. Returns {activity: hex}.
    """
    ramp = CATEGORICAL[mode]
    ordered = sorted(set(activities))
    out = {}
    for i, act in enumerate(ordered):
        out[act] = ramp[i] if i < MAX_SLOTS else OTHER_COLOR
    return out


def apply_theme(fig, mode="light"):
    """Style a Plotly figure with the mode's surfaces, ink, and recessive axes."""
    ink = INK[mode]
    fig.update_layout(
        paper_bgcolor=ink["surface"],
        plot_bgcolor=ink["surface"],
        font=dict(
            family='system-ui, -apple-system, "Segoe UI", sans-serif',
            color=ink["secondary"], size=13,
        ),
        title=dict(font=dict(color=ink["primary"], size=15)),
        legend=dict(font=dict(color=ink["secondary"]), bgcolor="rgba(0,0,0,0)"),
        margin=dict(l=8, r=8, t=40, b=8),
        hoverlabel=dict(
            font=dict(family='system-ui, -apple-system, "Segoe UI", sans-serif'),
        ),
    )
    axis = dict(
        gridcolor=ink["grid"], linecolor=ink["baseline"], zerolinecolor=ink["grid"],
        tickfont=dict(color=ink["muted"]), title=dict(font=dict(color=ink["secondary"])),
    )
    fig.update_xaxes(**axis)
    fig.update_yaxes(**axis)
    return fig
