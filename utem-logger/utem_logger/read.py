"""Reading the logs back.

    from utem_logger.read import load_day, load_frame
    recs = load_day(r"Z:/data/logs/utem-state/2026/2026-10/2026-10-15")
    changes = [r for r in recs if r["event"] == "change"]
    frames = [r for r in recs if r["event"] == "frame"]
    img = load_frame(day, frames[0]["file"])        # uint16 (1080, 1440, 3), sensor counts 0-1023

    df = to_dataframe(recs)    # pandas, one row per state line, one column per getter (needs pandas)
"""
from __future__ import annotations

import json
import pathlib

from . import png


def load_day(day_dir):
    out = []
    with open(pathlib.Path(day_dir) / "state.jsonl", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:      # a line cut by a power loss
                    pass
    return out


def load_frame(day_dir, rel):
    return png.load(pathlib.Path(day_dir) / rel)


def frame_info(path):
    """The state and camera settings embedded in a frame."""
    t = png.read_text(path)
    return {"time": t.get("utem_time"), "camera": json.loads(t.get("utem_camera", "{}")),
            "state": json.loads(t.get("utem_state", "{}"))}


def to_dataframe(recs, events=("start", "change", "heartbeat")):
    import pandas as pd
    rows = [{"t": r["t"], "event": r["event"], **r["state"]} for r in recs if r["event"] in events]
    df = pd.DataFrame(rows)
    df["t"] = pd.to_datetime(df["t"])
    return df.set_index("t")
