"""Palette, activity->color assignment, and Plotly theming for the dashboard.

The palette is the validated data-viz reference instance: a fixed-order categorical
ramp (assigned to activities by alphabetical name so a given activity always keeps
its color), a single-hue blue sequential ramp for the heatmap, and a set of light
ink + surface tokens. The app is light-only, so there is one background to validate
against. Colors are chosen last and were validated with the palette checker — do not
reorder the categorical slots without re-validating.
"""
from __future__ import annotations

# Fixed categorical slot order (never cycled). Slot i -> the i-th activity by name.
CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
               "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
MAX_SLOTS = len(CATEGORICAL)            # 8; activities past this fold to "Other"
OTHER_COLOR = "#898781"                 # muted grey

# Single-hue blue sequential ramp (heatmap magnitude), light->dark, steps 100..700.
SEQUENTIAL_BLUE = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5",
                   "#256abf", "#184f95", "#0d366b"]

# One sans stack shared by the Streamlit chrome (config.toml `font = "sans-serif"`)
# and every chart, so the app reads as a single typeface.
FONT_STACK = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif'

# Chart chrome / ink tokens.
INK = {
    "surface": "#fcfcfb", "plane": "#f9f9f7", "primary": "#0b0b0b",
    "secondary": "#52514e", "muted": "#898781", "grid": "#e1e0d9",
    "baseline": "#c3c2b7",
}


def text_on(bg_hex):
    """Black or white ink for a label set *inside* a colored fill, by luminance.

    A direct label riding a stacked segment is the one place text wears no ink
    token (marks-and-anatomy): pick whichever of white / near-black clears contrast
    against the fill, so a light slot (yellow) gets dark text and a dark slot gets
    white.
    """
    h = bg_hex.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    # Perceptual luminance (sRGB coefficients); no gamma step needed at this precision.
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#0b0b0b" if lum > 0.6 else "#ffffff"


def activity_colors(activities):
    """Map activities to fixed palette slots by alphabetical name.

    Stable: a given activity always gets the same color regardless of which other
    activities are present. Activities past slot 8 (alphabetically) share the muted
    "Other" grey. Returns {activity: hex}.
    """
    ordered = sorted(set(activities))
    out = {}
    for i, act in enumerate(ordered):
        out[act] = CATEGORICAL[i] if i < MAX_SLOTS else OTHER_COLOR
    return out


def apply_theme(fig, margin=None):
    """Style a Plotly figure with the app's surfaces, ink, and recessive axes.

    ``margin`` overrides the default padding for charts whose labels sit outside the
    plot (e.g. value labels at a bar tip need more right margin).
    """
    ink = INK
    fig.update_layout(
        paper_bgcolor=ink["surface"],
        plot_bgcolor=ink["surface"],
        font=dict(family=FONT_STACK, color=ink["secondary"], size=13),
        title=dict(font=dict(color=ink["primary"], size=15)),
        legend=dict(
            font=dict(color=ink["secondary"], size=12),
            bgcolor="rgba(0,0,0,0)", itemsizing="constant",
        ),
        margin=margin or dict(l=8, r=8, t=40, b=8),
        hoverlabel=dict(
            font=dict(family=FONT_STACK, color=ink["primary"], size=12),
            bgcolor=ink["plane"], bordercolor=ink["grid"],
        ),
    )
    axis = dict(
        gridcolor=ink["grid"], linecolor=ink["baseline"], zerolinecolor=ink["grid"],
        tickfont=dict(color=ink["muted"]), title=dict(font=dict(color=ink["secondary"])),
        automargin=True,   # grow the margin to fit ticks + title; never clip them
    )
    fig.update_xaxes(**axis)
    fig.update_yaxes(**axis)
    return fig
