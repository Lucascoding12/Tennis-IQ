"""Training dataset built from ATP match histories.

Pipeline:
  1. crawl()  — download the career match list of the top N ranked players from
                atptour.com into var/activity/<id>.json (refreshed when stale).
  2. build()  — merge those files into one de-duplicated, chronological match
                table, replaying every match to compute Elo ratings and form
                stats *as they were before the match* (no peeking at the future).
  3. The resulting `Dataset` holds the training table and the final per-player
     state, which is what live predictions use.
"""
import json
import math
import os
import threading
import time
import zlib
from collections import deque, namedtuple
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import numpy as np
import pandas as pd

import atp
import config

ACTIVITY_DIR = config.ACTIVITY_DIR
CRAWL_SIZE = 500          # top-N ranked players whose full careers are downloaded
STALE_AFTER = 6 * 3600    # re-download a player's history after this many seconds
FIRST_YEAR = 2000
COURTS = ["outdoor_hard", "indoor_hard", "clay", "grass"]
UNRANKED = 2000
HIST_FROM = (date.today() - timedelta(days=3 * 365)).isoformat()
ELO_ONLY_LEVELS = {"FU"}  # ITF Futures: used for Elo ratings, too low-level to train on

FEATURES = {
    "rank_log_ratio": "log(rank2 / rank1) — positive when player 1 is ranked higher",
    "elo_diff": "Overall Elo rating difference",
    "surface_elo_diff": "Elo difference on this court type",
    "elo_prob": "Elo win probability (blend of overall and court Elo)",
    "form_12m_diff": "12-month win % difference (shrunk toward 50% for small samples)",
    "surface_form_12m_diff": "12-month win % on this court type, difference",
    "last10_diff": "Wins in last 10 matches, difference",
    "activity_12m_diff": "Matches played in last 12 months, difference",
    "load_30d_diff": "Matches played in last 30 days, difference (fatigue / rhythm)",
    "h2h_edge": "Head-to-head edge, shrunk: (w1 - w2) / (w1 + w2 + 3)",
    "h2h_n": "Number of previous meetings",
    "experience_diff": "log(career matches) difference",
    "best_of_5": "Grand Slam match (best of 5)",
    "is_clay": "Played on clay",
    "is_grass": "Played on grass",
    "is_indoor": "Played indoors",
    "is_challenger": "Challenger-level match",
}


# ---------------------------------------------------------------- crawl

_failed: dict[str, float] = {}  # player id -> time of last failed download


def _fetch_activity(pid: str) -> bool:
    """Download one player's history. Returns True if the saved copy changed."""
    f = ACTIVITY_DIR / f"{pid}.json"
    try:
        text, _ = atp.request(f"{atp.BASE}/en/-/www/activity/sgl/{pid.lower()}/?v=1", kind="text")
    except Exception:
        _failed[pid] = time.time()
        if f.exists():
            os.utime(f)  # try again after STALE_AFTER instead of every cycle
        return False
    if f.exists() and f.read_text() == text:
        os.utime(f)
        return False
    config.write_atomic(f, text)
    return True


def crawl(progress: dict) -> int:
    """Download stale or missing histories for the top CRAWL_SIZE players. Returns # changed."""
    ids = [p["PlayerId"].upper() for p in atp.rankings()[:CRAWL_SIZE]]
    now = time.time()

    def stale(pid):
        f = ACTIVITY_DIR / f"{pid}.json"
        if f.exists():
            return now - f.stat().st_mtime > STALE_AFTER
        return now - _failed.get(pid, 0) > STALE_AFTER
    todo = [pid for pid in ids if stale(pid)]
    progress.update(crawl_total=len(todo), crawl_done=0)

    def one(pid):
        changed = _fetch_activity(pid)
        progress["crawl_done"] += 1
        return changed

    with ThreadPoolExecutor(4) as pool:
        return sum(pool.map(one, todo))


def refresh_players(ids: list[str]) -> int:
    """Re-download players on today's schedule (at most every 30 min). Returns # changed."""
    todo = {pid.upper() for pid in ids
            if (ACTIVITY_DIR / f"{pid.upper()}.json").exists()
            and time.time() - (ACTIVITY_DIR / f"{pid.upper()}.json").stat().st_mtime > 1800}
    with ThreadPoolExecutor(4) as pool:
        return sum(pool.map(_fetch_activity, todo))


# ---------------------------------------------------------------- merge

Match = namedtuple("Match", "key date event level court round round_id winner loser winner_rank loser_rank score")


def _load_matches() -> tuple[list[Match], dict, set]:
    rows, names, crawled = {}, {}, set()
    today = date.today().isoformat()
    for f in ACTIVITY_DIR.glob("*.json"):
        pid = f.stem
        crawled.add(pid)
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        for year in data.get("Activity") or []:
            if (year.get("EventYear") or 0) < FIRST_YEAR:
                continue
            for t in year.get("Tournaments") or []:
                d = (t.get("EventDate") or "")[:10]
                if not d or d > today:
                    continue
                for m in t.get("Matches") or []:
                    if m.get("IsBye") or m.get("WinLoss") not in ("W", "L"):
                        continue
                    if m.get("Set1Player") is None:  # walkover, no tennis played
                        continue
                    opp = (m.get("OpponentId") or "").upper()
                    if not opp:
                        continue
                    names.setdefault(opp, f'{m.get("OpponentFirstName") or ""} {m.get("OpponentLastName") or ""}'.strip())
                    key = f'{year["EventYear"]}-{t.get("EventId")}-{m.get("MatchId")}-{"".join(sorted([pid, opp]))}'
                    if key in rows:
                        continue
                    won = m["WinLoss"] == "W"
                    me_rank, opp_rank = t.get("PlayerRank") or None, m.get("OpponentRank") or None
                    rows[key] = Match(
                        key, d, t.get("EventDisplayName") or t.get("EventName"), t.get("EventType"),
                        atp.court(t.get("Surface"), t.get("InOutdoor")), (m.get("Round") or {}).get("ShortName"),
                        int((m.get("Round") or {}).get("Id") or 0),
                        pid if won else opp, opp if won else pid,
                        me_rank if won else opp_rank, opp_rank if won else me_rank,
                        atp._score(m, flip=not won))  # always from the winner's side
    for p in atp.rankings():
        names[p["PlayerId"].upper()] = p["Name"]
    out = sorted(rows.values(), key=lambda r: (r.date, r.round_id))
    return out, names, crawled


# ---------------------------------------------------------------- scores

def parse_score(score: str) -> dict | None:
    """Winner-perspective score '6-4 3-6 7-6(5)' -> games/sets, or None if the
    match didn't finish normally (retirement, walkover, odd format)."""
    sets = []
    for part in (score or "").split():
        try:
            a, b = part.split("(")[0].split("-")
            sets.append((int(a), int(b)))
        except ValueError:
            return None
    if not sets:
        return None
    won = sum(a > b for a, b in sets)
    lost = len(sets) - won
    if won not in (2, 3) or lost >= won or sets[-1][0] <= sets[-1][1]:
        return None
    for i, (a, b) in enumerate(sets):
        hi, lo = max(a, b), min(a, b)
        final = i == len(sets) - 1
        ok = (hi == 6 and lo <= 4) or (hi == 7 and lo in (5, 6)) or (final and hi > 7 and hi - lo == 2)
        if not ok:
            return None
    return {
        "games_w": sum(a for a, _ in sets), "games_l": sum(b for _, b in sets),
        "sets": len(sets), "bo5": won == 3, "deciding": len(sets) == 2 * won - 1,
    }


# ---------------------------------------------------------------- replay state

def _k(n: int) -> float:
    return 250 / ((n + 5) ** 0.4)


def _expect(a: float, b: float) -> float:
    return 1 / (1 + 10 ** ((b - a) / 400))


class Player:
    __slots__ = ("elo", "n", "selo", "sn", "win365", "wins", "cnt", "court365", "last10", "d30", "hist",
                 "season_court")

    def __init__(self):
        self.elo, self.n = 1500.0, 0
        self.selo = {c: 1500.0 for c in COURTS}
        self.sn = {c: 0 for c in COURTS}
        self.win365 = deque()            # (ordinal date, won, court)
        self.wins = self.cnt = 0
        self.court365 = {c: [0, 0] for c in COURTS}
        self.last10 = deque(maxlen=10)
        self.d30 = deque()
        self.hist = []                   # (iso date, elo) after each match
        self.season_court = {}           # (year, court) -> [wins, matches]

    def prune(self, day: int):
        while self.win365 and self.win365[0][0] < day - 365:
            _, w, c = self.win365.popleft()
            self.wins -= w
            self.cnt -= 1
            if c in self.court365:
                self.court365[c][0] -= w
                self.court365[c][1] -= 1
        while self.d30 and self.d30[0] < day - 30:
            self.d30.popleft()

    def form(self) -> float:
        return (self.wins + 3) / (self.cnt + 6)

    def court_form(self, court: str) -> float:
        w, n = self.court365.get(court, (0, 0))
        return (w + 3 * self.form()) / (n + 3)


class State:
    def __init__(self):
        self.players: dict[str, Player] = {}
        self.h2h: dict[tuple, int] = {}
        self.lock = threading.Lock()

    def get(self, pid) -> Player:
        p = self.players.get(pid)
        if p is None:
            p = self.players[pid] = Player()
        return p

    def features(self, a: str, b: str, day: int, court: str, level: str, rnd: str,
                 rank_a, rank_b) -> dict:
        pa, pb = self.get(a), self.get(b)
        pa.prune(day)
        pb.prune(day)
        sc = court if court in COURTS else "outdoor_hard"
        elo_d = pa.elo - pb.elo
        selo_d = pa.selo[sc] - pb.selo[sc]
        wa, wb = self.h2h.get((a, b), 0), self.h2h.get((b, a), 0)
        ra, rb = rank_a or UNRANKED, rank_b or UNRANKED
        return {
            "rank_log_ratio": math.log(rb / ra),
            "elo_diff": elo_d,
            "surface_elo_diff": selo_d,
            "elo_prob": 1 / (1 + 10 ** (-(0.5 * elo_d + 0.5 * selo_d) / 400)),
            "form_12m_diff": pa.form() - pb.form(),
            "surface_form_12m_diff": pa.court_form(sc) - pb.court_form(sc),
            "last10_diff": sum(pa.last10) - sum(pb.last10),
            "activity_12m_diff": pa.cnt - pb.cnt,
            "load_30d_diff": len(pa.d30) - len(pb.d30),
            "h2h_edge": (wa - wb) / (wa + wb + 3),
            "h2h_n": wa + wb,
            "experience_diff": math.log1p(pa.n) - math.log1p(pb.n),
            "best_of_5": int(level == "GS" and not (rnd or "").startswith("Q")),
            "is_clay": int(court == "clay"),
            "is_grass": int(court == "grass"),
            "is_indoor": int(court == "indoor_hard"),
            "is_challenger": int(level == "CH"),
        }

    def update(self, w: str, l: str, day: int, iso: str, court: str):
        pw, pl = self.get(w), self.get(l)
        year = int(iso[:4])
        e = _expect(pw.elo, pl.elo)
        pw.elo += _k(pw.n) * (1 - e)
        pl.elo -= _k(pl.n) * (1 - e)
        pw.n += 1
        pl.n += 1
        if court in COURTS:
            e = _expect(pw.selo[court], pl.selo[court])
            pw.selo[court] += _k(pw.sn[court]) * (1 - e)
            pl.selo[court] -= _k(pl.sn[court]) * (1 - e)
            pw.sn[court] += 1
            pl.sn[court] += 1
        for p, won in ((pw, 1), (pl, 0)):
            p.win365.append((day, won, court))
            rec = p.season_court.setdefault((year, court), [0, 0])
            rec[0] += won
            rec[1] += 1
            p.wins += won
            p.cnt += 1
            if court in p.court365:
                p.court365[court][0] += won
                p.court365[court][1] += 1
            p.last10.append(won)
            p.d30.append(day)
            if iso >= HIST_FROM:  # the player page charts the last 3 years only
                p.hist.append((iso, round(p.elo, 1)))
        self.h2h[(w, l)] = self.h2h.get((w, l), 0) + 1


class Dataset:
    def __init__(self, df: pd.DataFrame, state: State, names: dict, crawled: set):
        self.df, self.state, self.names, self.crawled = df, state, names, crawled
        self.summary = summary(self)  # computed once; the Data tab polls it

    def live_features(self, a: str, b: str, court: str, level: str = "250",
                      rank_a=None, rank_b=None) -> dict:
        with self.state.lock:
            return self.state.features(a, b, date.today().toordinal(), court, level, "R32", rank_a, rank_b)

    def elo(self, pid: str) -> dict | None:
        p = self.state.players.get(pid)
        if not p or p.n == 0:
            return None
        return {"elo": round(p.elo), "matches": p.n, **{c: round(p.selo[c]) for c in COURTS}}


def build() -> Dataset:
    matches, names, crawled = _load_matches()
    state = State()
    chunks, rows = [], []

    def flush():  # build the table in chunks so 100k row dicts never sit in memory at once
        if rows:
            df = pd.DataFrame(rows)
            floats = df.select_dtypes("float64").columns
            df[floats] = df[floats].astype("float32")
            chunks.append(df)
            rows.clear()

    for m in matches:
        day = date.fromisoformat(m.date).toordinal()
        w, l = m.winner, m.loser
        if m.level in ELO_ONLY_LEVELS:  # rate the match, but keep it out of the training table
            state.get(w).prune(day)
            state.get(l).prune(day)
            state.update(w, l, day, m.date, m.court)
            continue
        flip = zlib.crc32(m.key.encode()) & 1          # random but stable orientation
        a, b = (l, w) if flip else (w, l)
        ra, rb = (m.loser_rank, m.winner_rank) if flip else (m.winner_rank, m.loser_rank)
        pa, pb = state.get(a), state.get(b)
        n_a, n_b = pa.n, pb.n
        feats = state.features(a, b, day, m.court, m.level, m.round, ra, rb)
        year = int(m.date[:4])
        sa = pa.season_court.get((year, m.court), [0, 0])
        sb = pb.season_court.get((year, m.court), [0, 0])
        ca, cb = pa.court365.get(m.court, [0, 0]), pb.court365.get(m.court, [0, 0])
        sc = parse_score(m.score)
        rows.append({
            "key": m.key, "date": m.date, "year": year, "event": m.event, "level": m.level,
            "court": m.court, "round": m.round,
            "p1": a, "p2": b, "p1_name": names.get(a, a), "p2_name": names.get(b, b),
            "p1_rank": ra, "p2_rank": rb, "p1_prior_matches": n_a, "p2_prior_matches": n_b,
            "winner": w, "score": m.score, "label": int(not flip), **feats,
            # raw inputs for Billy's Model backtest
            "p1_last10": sum(pa.last10), "p2_last10": sum(pb.last10),
            "p1_last10_n": len(pa.last10), "p2_last10_n": len(pb.last10),
            "p1_season_court_w": sa[0], "p1_season_court_n": sa[1],
            "p2_season_court_w": sb[0], "p2_season_court_n": sb[1],
            "p1_court12_w": ca[0], "p1_court12_n": ca[1], "p2_court12_w": cb[0], "p2_court12_n": cb[1],
            # outcomes for the points/sets backtest (None when the match didn't finish normally)
            "complete": sc is not None,
            "total_games": sc["games_w"] + sc["games_l"] if sc else None,
            "went_deciding": sc["deciding"] if sc else None,
            "is_bo5_actual": sc["bo5"] if sc else None,
        })
        if len(rows) >= 10000:
            flush()
        state.update(w, l, day, m.date, m.court)
    flush()
    del matches
    return Dataset(pd.concat(chunks, ignore_index=True), state, names, crawled)


def summary(ds: "Dataset") -> dict:
    if ds.df.empty:
        return {"ready": False}
    df = ds.df
    fav = df.dropna(subset=["p1_rank", "p2_rank"])
    fav = fav[fav.p1_rank != fav.p2_rank]
    fav_won = ((fav.p1_rank < fav.p2_rank) == (fav.label == 1))
    elo_won = (df.elo_prob > 0.5) == (df.label == 1)
    by_court = []
    for c in COURTS + ["other"]:
        sub = df[df.court == c]
        if len(sub):
            fsub = fav[fav.court == c]
            by_court.append({"court": c, "matches": len(sub),
                             "favWin": round(100 * fav_won[fav.court == c].mean(), 1) if len(fsub) else None,
                             "eloWin": round(100 * elo_won[df.court == c].mean(), 1)})
    by_year = df.groupby("year").size()
    recent = df.tail(25).iloc[::-1]
    return {
        "ready": True,
        "matches": len(df),
        "players": int(pd.concat([df.p1, df.p2]).nunique()),
        "crawled": len(ds.crawled),
        "from": df.date.min(), "to": df.date.max(),
        "byCourt": by_court,
        "byYear": {int(k): int(v) for k, v in by_year.items()},
        "recent": recent[["date", "event", "level", "court", "round", "p1_name", "p2_name",
                          "score", "winner", "p1"]].replace({np.nan: None}).to_dict("records"),
    }
