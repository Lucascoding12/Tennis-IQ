"""Tennis IQ API server.

A background thread keeps everything fresh (crawl ATP, rebuild the dataset,
retrain models, re-run backtests) every ENGINE_EVERY seconds; the HTTP handlers
only read from that state or make cached calls to atptour.com.
"""
import asyncio
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi import Body, Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

import atp
import config
import dataset
import lab
import points
import stats
import system

FRONTEND = Path(os.environ.get("TIQ_FRONTEND_DIR") or Path(__file__).parent.parent / "frontend")
ENGINE_EVERY = 15 * 60  # seconds between background data updates
ADMIN_TOKEN = os.environ.get("TIQ_ADMIN_TOKEN")  # required for model/data changes when set


# ---------------------------------------------------------------- background engine

class Engine:
    def __init__(self):
        self.ds: dataset.Dataset | None = None
        self.backtest: dict | None = None
        self.status = {"phase": "starting", "crawl_done": 0, "crawl_total": 0, "last_update": None, "error": None}
        self.wake = threading.Event()

    def rebuild(self):
        self.status["phase"] = "building dataset"
        ds = dataset.build()
        if not lab.list_models()["models"]:
            self.status["phase"] = "training first models"
            lab.train(ds, {"name": "Elo baseline", "type": "elo"})
            lab.train(ds, lab.DEFAULT_CONFIG, activate=True)
        else:
            self.status["phase"] = "retraining models"
            lab.retrain_auto(ds)
        self.ds = ds
        self.backtest = system.backtest(ds)
        system._cache.clear()
        if points.needs_backtest(ds):  # first run, and again each new season
            self.status["phase"] = "backtesting the points model (~3 min)"
            points.backtest(ds)

    def cycle(self, first: bool):
        if first and any(dataset.ACTIVITY_DIR.glob("*.json")):
            self.rebuild()  # serve what's on disk right away, then refresh it
        self.status["phase"] = "downloading ATP match histories"
        changed = dataset.refresh_players(playing_today()) + dataset.crawl(self.status)
        if changed or self.ds is None:
            self.rebuild()
        self.status.update(phase="idle", error=None, last_update=time.time())

    def run(self):
        first = True
        while True:
            try:
                self.cycle(first)
            except Exception as e:
                traceback.print_exc()
                self.status.update(phase="idle", error=str(e))
            first = False
            self.wake.wait(ENGINE_EVERY)
            self.wake.clear()


engine = Engine()


@asynccontextmanager
async def lifespan(_):
    threading.Thread(target=engine.run, daemon=True).start()
    yield


app = FastAPI(title="Tennis IQ", lifespan=lifespan)


def _admin(x_admin_token: str | None = Header(default=None)):
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(401, "Admin password required")


def _ready():
    if engine.ds is None:
        raise HTTPException(503, f"Data is loading ({engine.status['phase']}), so give it a minute")
    return engine.ds


def _week() -> list[dict]:
    try:
        return atp.week_matches()
    except Exception:
        traceback.print_exc()
        return []


def playing_today() -> list[str]:
    """Players still to play this week (refreshed first by the engine)."""
    return list({p["id"] for m in _week() if m["state"] != "finished" for p in m["players"]})


def predict_pair(a: str, b: str, court: str, level: str = "250") -> float | None:
    ranks = atp.current_ranks()
    try:
        return lab.predict(engine.ds, a, b, court, level, ranks.get(a), ranks.get(b))
    except Exception:
        traceback.print_exc()
        return None


# ---------------------------------------------------------------- player histories

def _round_order(name: str) -> int:
    """Sort key for round names from either ATP ('Quarter-Finals', 'Q1') or ESPN ('Quarterfinal')."""
    n = (name or "").lower().replace("-", "")
    if n.startswith("q") and ("ual" in n or n[1:2].isdigit()):
        return 0
    for i, key in enumerate(("128", "64", "32", "16", "quarter", "semi", "final"), 1):
        if key in n:
            return i
    return 3


def _recent_by_player() -> dict:
    """This week's finished matches (ATP tournament pages), keyed by player id, newest first."""
    ranks = atp.current_ranks()
    names = {p["PlayerId"].upper(): p["Name"] for p in atp.rankings()}
    if engine.ds:
        names = {**engine.ds.names, **names}
    out: dict[str, list] = {}
    for m in _week():
        if m["state"] != "finished":
            continue
        for me, opp in (m["players"], m["players"][::-1]):
            # each side's sets carry the tiebreak points of the player who lost that set
            score = " ".join(f"{g}-{og}" + (f"({tb if tb is not None else otb})" if tb is not None or otb is not None else "")
                             for (g, tb), (og, otb) in zip(me["sets"], opp["sets"]))
            out.setdefault(me["id"], []).append({
                "date": m["date"], "endDate": m["date"], "tournament": m["tournament"],
                "level": "CH" if m["tour"] == "Challenger" else m["level"], "court": m["court"],
                "round": m["round"], "roundId": _round_order(m["round"]),
                "result": "W" if me["winner"] else "L",
                "opponentId": opp["id"], "opponent": names.get(opp["id"], opp["id"]),
                "opponentRankNow": ranks.get(opp["id"]), "score": score, "stats": None, "minutes": m["minutes"],
            })
    for v in out.values():
        v.sort(key=lambda x: (x["date"], x["roundId"]), reverse=True)
    return out


def history(pid: str) -> list[dict]:
    """ATP career history plus this week's results that ATP hasn't published yet."""
    ms = atp.matches(pid)
    newest = max((m.get("endDate") or m["date"] for m in ms), default="")
    try:
        extra = [m for m in _recent_by_player().get(pid.upper(), []) if m["date"] > newest]
    except Exception:
        extra = []
    return extra + ms


def _player(pid: str, ms: list[dict]) -> dict:
    return {"profile": atp.profile(pid), "stats": stats.player_stats(ms),
            "elo": engine.ds.elo(pid.upper()) if engine.ds else None}


def _minutes(m: dict) -> int | None:
    return m["minutes"] if "minutes" in m else atp.duration(m.get("stats"))


def _durations(histories: dict[str, list], meetings: list[dict]) -> dict:
    """Each player's last match length, court time over 14 days, and their last meeting's length."""
    cutoff = (date.today() - timedelta(days=14)).isoformat()
    out = {}
    for pid, ms in histories.items():
        recent = [m for m in ms if m["date"] >= cutoff][:8]
        last = ms[0] if ms else {}
        with ThreadPoolExecutor(8) as pool:  # each lookup is a separate request
            mins = list(pool.map(_minutes, recent))
        out[pid] = {
            "last": {"minutes": _minutes(last) if last else None, "tournament": last.get("tournament"),
                     "round": last.get("round"), "result": last.get("result"), "date": last.get("date")},
            "recentMinutes": sum(x for x in mins if x), "recentMatches": len(recent),
            "recentTimed": sum(1 for x in mins if x),
        }
    if meetings:
        m = meetings[0]
        out["lastMeeting"] = {"minutes": atp.duration(m.get("stats")), "tournament": m["tournament"],
                              "year": m["year"], "round": m["round"]}
    return out


# ---------------------------------------------------------------- H2H

@app.get("/api/health")
def health():
    return {"ok": True, "ready": engine.ds is not None, "engine": engine.status}


@app.get("/api/players")
def players(q: str = ""):
    return atp.search(q)


@app.get("/api/compare")
async def compare(p1: str, p2: str):
    p1, p2 = p1.upper(), p2.upper()
    if p1 == p2:
        raise HTTPException(400, "Pick two different players")
    try:
        h1, h2, meetings = await asyncio.gather(
            asyncio.to_thread(history, p1), asyncio.to_thread(history, p2), asyncio.to_thread(atp.h2h, p1, p2))
        a, b, durations = await asyncio.gather(
            asyncio.to_thread(_player, p1, h1), asyncio.to_thread(_player, p2, h2),
            asyncio.to_thread(_durations, {p1: h1, p2: h2}, meetings))
    except Exception as e:  # ATP site down / blocked / bad id
        raise HTTPException(502, f"Couldn't load data from atptour.com: {e}")
    return {"p1": a, "p2": b, "h2h": meetings, "durations": durations,
            "courts": [[c, c.replace("_", " ").title()] for c in dataset.COURTS]}


@app.get("/api/points")
def points_estimate(p1: str, p2: str, court: str = "outdoor_hard", bo: int = 3):
    p1, p2 = p1.upper(), p2.upper()
    if court not in points.TOUR_SPW or bo not in (3, 5):
        raise HTTPException(400, "Bad court or best-of")
    win = predict_pair(p1, p2, court, "GS" if bo == 5 else "250")
    try:
        est = points.estimate(p1, p2, court, win, bo)
    except Exception as e:
        raise HTTPException(502, f"Couldn't build the estimate: {e}")
    rep = points.report()
    return {**est, "winProb": round(100 * win, 1) if win is not None else None,
            "backtest": {k: rep[k] for k in ("verdict", "test_year", "matches")} if rep else None}


@app.get("/api/player/{pid}")
def player(pid: str):
    pid = pid.upper()
    try:
        ms = history(pid)
        info = _player(pid, ms)
    except Exception as e:
        raise HTTPException(502, f"Couldn't load data from atptour.com: {e}")
    years = {}
    for m in ms:
        y = years.setdefault(m["date"][:4], {"won": 0, "lost": 0})
        y["won" if m["result"] == "W" else "lost"] += 1
    hist = []
    if engine.ds and pid in engine.ds.state.players:
        cutoff = (date.today() - timedelta(days=3 * 365)).isoformat()
        hist = [h for h in engine.ds.state.players[pid].hist if h[0] >= cutoff]
    info.update(years=[{"year": y, **r} for y, r in sorted(years.items(), reverse=True)][:10],
                eloHistory=hist, recent=ms[:25])
    return info


# ---------------------------------------------------------------- Billy's Model

@app.get("/api/system")
def billy():
    ds = _ready()
    return {"passing": system.screen(ds, _week(), engine.backtest, history), "rules": {"gap": system.RANK_GAP}}


# ---------------------------------------------------------------- rankings

@app.get("/api/rankings")
def rankings(limit: int = 300):
    ds = engine.ds
    today = date.today().toordinal()
    rows = []
    for p in atp.rankings()[:min(limit, 300)]:
        pid = p["PlayerId"].upper()
        st = ds.state.players.get(pid) if ds else None
        if st:
            with ds.state.lock:
                st.prune(today)
        rows.append({
            "id": pid, "rank": p["RankNo"], "name": p["Name"], "country": p["CountryCode"],
            "points": int(str(p["Points"]).replace(",", "") or 0), "move": p.get("Movement") or 0,
            "elo": round(st.elo) if st and st.n else None,
            "surfaceElo": {c: round(st.selo[c]) for c in dataset.COURTS} if st and st.n else None,
            "won12": st.wins if st else None, "lost12": (st.cnt - st.wins) if st else None,
        })
    for i, r in enumerate(sorted([r for r in rows if r["elo"]], key=lambda r: -r["elo"]), 1):
        r["eloRank"] = i
    return {"rows": rows}


# ---------------------------------------------------------------- data + model lab

@app.get("/api/data/status")
def data_status():
    return {"engine": engine.status, "dataset": engine.ds.summary if engine.ds else {"ready": False},
            "crawlSize": dataset.CRAWL_SIZE, "everyMinutes": ENGINE_EVERY // 60}


@app.post("/api/data/refresh", dependencies=[Depends(_admin)])
def data_refresh():
    engine.wake.set()
    return {"ok": True}


_csv_lock = threading.Lock()


@app.get("/api/data/download.csv")
def data_download():
    ds = _ready()
    path = config.VAR / "tennis_iq_matches.csv"
    with _csv_lock:  # written once per dataset build, then served from disk
        if getattr(ds, "csv_written", False) is False or not path.exists():
            tmp = path.with_suffix(".tmp")
            ds.df.to_csv(tmp, index=False)
            os.replace(tmp, path)
            ds.csv_written = True
    return FileResponse(path, media_type="text/csv", filename="tennis_iq_matches.csv")


@app.get("/api/games/report")
def games_report():
    return {}  # the old games model was replaced by the points model; the Data tab hides this section


@app.get("/api/lab/options")
def lab_options():
    return {"features": dataset.FEATURES, "types": lab.MODEL_TYPES, "default": lab.DEFAULT_CONFIG}


@app.get("/api/lab/models")
def lab_models():
    return lab.list_models()


@app.post("/api/lab/train", dependencies=[Depends(_admin)])
def lab_train(cfg: dict = Body(...)):
    ds = _ready()
    try:
        return lab.train(ds, cfg, activate=bool(cfg.get("activate")))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/lab/models/{slug}/activate", dependencies=[Depends(_admin)])
def lab_activate(slug: str):
    try:
        lab.activate(slug)
    except KeyError:
        raise HTTPException(404)
    return lab.list_models()


@app.delete("/api/lab/models/{slug}", dependencies=[Depends(_admin)])
def lab_delete(slug: str):
    lab.delete(slug)
    return lab.list_models()


# ---------------------------------------------------------------- images + website

@app.get("/img/{pid}")
def img(pid: str):
    try:
        data, ctype = atp.headshot(pid)
    except Exception:
        raise HTTPException(404)
    return Response(data, media_type=ctype, headers={"Cache-Control": "public, max-age=604800"})


if FRONTEND.exists():  # the backend can serve the website itself (Netlify is optional)
    app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="site")
