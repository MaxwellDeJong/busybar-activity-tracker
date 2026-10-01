"""Palette and activity->color assignment for the dashboard.

A fixed-order categorical ramp, assigned to activities by alphabetical name so a
given activity always keeps its color in every view. The slots were validated
with the palette checker as mid-tone fills that hold up on both light and dark
surfaces — do not reorder them without re-validating. Everything else on the
pages (ink, lines, washes) is mixed from currentColor in dashboard.py's CSS.
"""
from __future__ import annotations

# Fixed categorical slot order (never cycled). Slot i -> the i-th activity by name.
CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
               "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
MAX_SLOTS = len(CATEGORICAL)            # 8; activities past this fold to "Other"
OTHER_COLOR = "#898781"                 # muted grey


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
