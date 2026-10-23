"""Per-player records built from a match list (most recent match first)."""
from datetime import date, timedelta

from dataset import COURTS


def record(ms: list[dict]) -> dict:
    w = sum(m["result"] == "W" for m in ms)
    return {"won": w, "lost": len(ms) - w, "pct": round(100 * w / len(ms), 1) if ms else None}


def player_stats(ms: list[dict]) -> dict:
    cutoff = (date.today() - timedelta(days=365)).isoformat()
    recent = [m for m in ms if m["date"] >= cutoff]
    year = str(date.today().year)
    return {
        "last12": record(recent),
        "ytd": record([m for m in ms if m["date"].startswith(year)]),
        "career": record(ms),
        "surfaces12": {k: record([m for m in recent if m["court"] == k]) for k in COURTS},
        "surfacesCareer": {k: record([m for m in ms if m["court"] == k]) for k in COURTS},
        "last10": ms[:10],
    }
