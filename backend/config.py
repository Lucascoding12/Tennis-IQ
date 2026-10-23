"""Where Tennis IQ stores downloaded data and trained models.

Set TIQ_DATA_DIR to a persistent disk in production (e.g. /var/data on Render).
"""
import os
from pathlib import Path

VAR = Path(os.environ.get("TIQ_DATA_DIR") or Path(__file__).parent / "var")
ACTIVITY_DIR = VAR / "activity"   # crawled ATP career histories
STATS_DIR = VAR / "stats"         # ATP serve/return stats per player-season
MODEL_DIR = VAR / "models"        # trained models + backtest reports


def write_atomic(path: Path, data: str | bytes) -> None:
    """Write via a temp file + rename, so a crash mid-write never leaves a broken file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{id(data)}.tmp")
    tmp.write_bytes(data.encode() if isinstance(data, str) else data)
    os.replace(tmp, path)
