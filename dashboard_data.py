"""Loading, filtering, and shaping of Busy Bar activity sessions.

This module owns the data pipeline for both the single-day CLI summary
(`activity_summary.py`) and the interactive dashboard (`dashboard.py`). Keeping the
day-boundary and filter semantics in one place means the two tools can never
disagree about what "a day" is or which sessions count.

Two layers live here:

  * Low-level record loading + shared helpers (`load_records`, `logical_day`,
    `parse_day_arg`, `fmt_duration`) — used by the CLI summary, no pandas needed.
  * A pandas pipeline for the dashboard (`load_frame`, `prepare`, `load_prepared`,
    and the aggregation helpers) that filters and reshapes sessions into the frames
    each view consumes.

Filter rules for the prepared frame (dashboard v1):
  * rest is omitted entirely  -> drop rows where phase == "rest"
  * sub-minute noise is hidden -> drop rows where duration_s < MIN_DURATION_S
  * a "day" runs 03:00 -> 03:00 local, so late-night work lands on the day it
    started (see `logical_day`).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

DAY_CUTOFF_HOUR = 3           # a day runs [03:00, next 03:00) local
MIN_DURATION_S = 60           # drop sessions shorter than this
_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOG = os.path.join(_HERE, "activity_log.jsonl")
DEFAULT_CARD_MAP = os.path.join(_HERE, "activity_card_id_map.json")


# --------------------------------------------------------------------------- #
# Shared helpers (no pandas)                                                   #
# --------------------------------------------------------------------------- #
def parse_day_arg(s):
    """Parse 'M/D/YY' or 'M/D/YYYY' into a date."""
    parts = s.strip().split("/")
    if len(parts) != 3:
        raise ValueError(f"expected M/D/YY, got {s!r}")
    month, day, year = (int(p) for p in parts)
    if year < 100:
        year += 2000
    return dt.date(year, month, day)


def logical_day(local_dt):
    """The day a local datetime belongs to, given the 3 AM cutoff."""
    return (local_dt - dt.timedelta(hours=DAY_CUTOFF_HOUR)).date()


def fmt_duration(seconds, precision="second"):
    """Human-readable duration.

    ``precision="second"`` (the CLI default) keeps seconds: '1h5m30s' / '5m2s' /
    '42s'. ``precision="minute"`` rounds to the nearest minute and drops the seconds
    field — '4h34m' / '43m' — the right altitude for the dashboard's aggregates and
    hovers, where second-level detail is just noise.
    """
    if precision == "minute":
        total_min = int(round(seconds / 60.0))
        h, m = divmod(total_min, 60)
        return f"{h}h{m:02d}m" if h else f"{m}m"

    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    out = ""
    if h:
        out += f"{h}h"
    if h or m:
        out += f"{m}m"
    out += f"{sec}s"
    return out


def load_card_map(path=DEFAULT_CARD_MAP):
    """Load the card_id -> activity-name map. Returns {} if the file is missing so
    callers degrade to the raw ``activity`` field rather than crashing."""
    if not os.path.exists(path):
        return {}
    with open(path) as fh:
        return json.load(fh)


def load_records(path=DEFAULT_LOG):
    """Parse the jsonl log into a list of raw session dicts.

    Each returned record is the original JSON object plus two derived fields:
      * ``start_local`` -- the start as a timezone-aware local datetime
      * ``duration_s``  -- coerced to float (default 0.0)

    No filtering is applied here; malformed lines are skipped with a warning.
    Raises FileNotFoundError if the log is missing (callers decide how to report).
    """
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    card_map = load_card_map()
    records = []
    with open(path) as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"warning: skipping malformed line {lineno}: {e}", file=sys.stderr)
                continue
            # ISO-8601 with trailing Z (UTC); convert to system-local time.
            start_utc = dt.datetime.fromisoformat(rec["start"].replace("Z", "+00:00"))
            rec["start_local"] = start_utc.astimezone()
            rec["duration_s"] = float(rec.get("duration_s", 0.0))
            # Resolve a friendly activity name via the card map. The recorder writes
            # the raw card_id as `activity` for any card missing from the map, so
            # prefer the map's name, then the record's own label, then the raw id.
            cid = rec.get("card_id")
            rec["activity"] = card_map.get(cid) or rec.get("activity") or cid
            records.append(rec)
    return records


# --------------------------------------------------------------------------- #
# pandas pipeline (dashboard)                                                  #
# --------------------------------------------------------------------------- #
# Columns guaranteed on a prepared frame (used to build empty frames too).
_PREPARED_COLUMNS = [
    "activity", "phase", "start_local", "duration_s", "duration_min", "logical_day",
]


def _empty_prepared():
    import pandas as pd
    return pd.DataFrame({c: pd.Series(dtype="object") for c in _PREPARED_COLUMNS})


def load_frame(path=DEFAULT_LOG):
    """Load the raw log into a DataFrame (one row per session, unfiltered)."""
    import pandas as pd
    records = load_records(path)
    if not records:
        return pd.DataFrame()
    return pd.DataFrame(records)


def prepare(df):
    """Filter and derive the frame the dashboard views consume.

    Drops rest sessions and sub-minute noise; adds ``duration_min`` and
    ``logical_day``. Accepts (and returns) an empty frame gracefully.
    """
    import pandas as pd  # noqa: F401  (kept local so CLI users need not import pandas)
    if df is None or df.empty:
        return _empty_prepared()

    out = df.copy()
    # 1. drop rest sessions entirely
    if "phase" in out.columns:
        out = out[out["phase"] != "rest"]
    # 2. drop sub-minute noise
    out = out[out["duration_s"] >= MIN_DURATION_S]

    if out.empty:
        return _empty_prepared()

    # 3. derived fields
    out = out.copy()
    out["duration_min"] = out["duration_s"] / 60.0
    out["logical_day"] = out["start_local"].map(logical_day)
    return out.reset_index(drop=True)


def load_prepared(path=DEFAULT_LOG):
    """Convenience: load_frame + prepare."""
    return prepare(load_frame(path))


def daily_by_activity(df):
    """Pivot to minutes per (logical_day x activity).

    index = logical_day (date), columns = activity, values = summed minutes.
    Missing (day, activity) cells are 0. Returns an empty frame if no sessions.
    """
    import pandas as pd
    if df is None or df.empty:
        return pd.DataFrame()
    pivot = pd.pivot_table(
        df, index="logical_day", columns="activity",
        values="duration_min", aggfunc="sum", fill_value=0.0,
    )
    pivot.columns.name = None
    return pivot.sort_index()


def daily_productive(df):
    """Series of total productive minutes per logical_day (all activities summed)."""
    import pandas as pd
    if df is None or df.empty:
        return pd.Series(dtype="float64", name="productive_min")
    s = df.groupby("logical_day")["duration_min"].sum().sort_index()
    s.name = "productive_min"
    return s


def day_sessions(df, day):
    """All sessions for one logical day, sorted by start time.

    ``day`` may be a datetime.date or a 'M/D/YY' string.
    """
    import pandas as pd  # noqa: F401
    if isinstance(day, str):
        day = parse_day_arg(day)
    if df is None or df.empty:
        return _empty_prepared()
    sel = df[df["logical_day"] == day]
    return sel.sort_values("start_local").reset_index(drop=True)


def range_by_activity(df, start, end):
    """`daily_by_activity` sliced to logical days in [start, end] (inclusive),
    reindexed to a continuous daily calendar so gap days appear as all-zero rows
    (the heatmap and stacked-bar views need every day present). ``start``/``end``
    are datetime.date.
    """
    import pandas as pd
    pivot = daily_by_activity(df)
    full_index = pd.date_range(start, end, freq="D").date
    if pivot.empty:
        return pd.DataFrame(index=pd.Index(full_index, name="logical_day"))
    sliced = pivot[(pivot.index >= start) & (pivot.index <= end)]
    return sliced.reindex(full_index, fill_value=0.0).rename_axis("logical_day")


def week_bounds(day):
    """Monday..Sunday of the week containing ``day`` (both datetime.date).

    Weeks are Mon-Sun to match the heatmap's row layout. ``day`` may be a date or a
    'M/D/YY' string.
    """
    if isinstance(day, str):
        day = parse_day_arg(day)
    monday = day - dt.timedelta(days=day.weekday())
    return monday, monday + dt.timedelta(days=6)


def month_bounds(day):
    """First..last calendar day of the month containing ``day`` (both date).

    ``day`` may be a date or a 'M/D/YY' string.
    """
    if isinstance(day, str):
        day = parse_day_arg(day)
    first = day.replace(day=1)
    nxt = first.replace(year=first.year + 1, month=1) if first.month == 12 \
        else first.replace(month=first.month + 1)
    return first, nxt - dt.timedelta(days=1)


def range_sessions(df, start, end):
    """All sessions whose logical_day falls in [start, end], sorted by start time.

    The multi-day analogue of `day_sessions`, used for week/month aggregate tables.
    ``start``/``end`` may be dates or 'M/D/YY' strings.
    """
    if isinstance(start, str):
        start = parse_day_arg(start)
    if isinstance(end, str):
        end = parse_day_arg(end)
    if df is None or df.empty:
        return _empty_prepared()
    sel = df[(df["logical_day"] >= start) & (df["logical_day"] <= end)]
    return sel.sort_values("start_local").reset_index(drop=True)
