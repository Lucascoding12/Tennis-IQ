"""Billy's Model: screen today's and tomorrow's matches with Billy's tests.

Tests (the favourite = the better-ranked player):
  1. Ranking gap of at least RANK_GAP places.
  2. Favourite won at least 8 of their last 10 (7/10 passes but is flagged).
  3. Both players' win rate on the tournament's court type this season (last 12
     months when either has fewer than MIN_SAMPLE matches there this year);
     the favourite must have the better rate.
"""
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import numpy as np

import atp
import lab
from dataset import UNRANKED

RANK_GAP = 50
MIN_SAMPLE = 3
LEVELS = ["GS", "1000", "500", "250", "WC", "OL", "CH"]


# ---------------------------------------------------------------- backtest

def backtest(ds, from_year: int = 2015) -> dict:
    """How often favourites that passed the tests went on to win, split by last-10 form."""
    df = ds.df
    df = df[df.level.isin(LEVELS) & (df.year >= from_year) & df.p1_rank.notna() & df.p2_rank.notna()]
    fav1 = (df.p1_rank < df.p2_rank).to_numpy()
    pick = lambda a, b: np.where(fav1, df[a].to_numpy(), df[b].to_numpy())  # noqa: E731
    gap = (df.p1_rank - df.p2_rank).abs().to_numpy()
    l10, l10n = pick("p1_last10", "p2_last10"), pick("p1_last10_n", "p2_last10_n")
    season_ok = (pick("p1_season_court_n", "p2_season_court_n") >= MIN_SAMPLE) & \
                (pick("p2_season_court_n", "p1_season_court_n") >= MIN_SAMPLE)

    def rate(w, n):
        return pick(w[0], w[1]) / np.maximum(pick(n[0], n[1]), 1)
    fav_rate = np.where(season_ok, rate(("p1_season_court_w", "p2_season_court_w"), ("p1_season_court_n", "p2_season_court_n")),
                        rate(("p1_court12_w", "p2_court12_w"), ("p1_court12_n", "p2_court12_n")))
    dog_rate = np.where(season_ok, rate(("p2_season_court_w", "p1_season_court_w"), ("p2_season_court_n", "p1_season_court_n")),
                        rate(("p2_court12_w", "p1_court12_w"), ("p2_court12_n", "p1_court12_n")))
    known = season_ok | ((pick("p1_court12_n", "p2_court12_n") >= MIN_SAMPLE) &
                         (pick("p2_court12_n", "p1_court12_n") >= MIN_SAMPLE))
    passed = (gap >= RANK_GAP) & (l10n >= 10) & (l10 >= 7) & known & (fav_rate >= dog_rate)
    fav_won = np.where(fav1, df.label.to_numpy() == 1, df.label.to_numpy() == 0)

    def stat(mask):
        n = int(mask.sum())
        wr = float(fav_won[mask].mean()) if n else 0
        return {"n": n, "win": round(100 * wr, 1), "fair_odds": round(1 / wr, 3) if wr else None}
    return {"from": from_year, "form8": stat(passed & (l10 >= 8)), "form7": stat(passed & (l10 == 7))}


# ---------------------------------------------------------------- live screening

def _court_record(ms: list[dict], court: str, season_only: bool) -> dict:
    year = str(date.today().year)
    cutoff = (date.today() - timedelta(days=365)).isoformat()
    xs = [m for m in ms if m["court"] == court and (m["date"].startswith(year) if season_only else m["date"] >= cutoff)]
    won = sum(m["result"] == "W" for m in xs)
    return {"won": won, "lost": len(xs) - won}


def _rate(r: dict) -> float | None:
    n = r["won"] + r["lost"]
    return r["won"] / n if n else None


def evaluate(m: dict, ids: tuple, ranks: dict, history: dict, ds, bt: dict | None) -> dict | None:
    """Run the tests on one scheduled match. Returns a report card if it passes, or if it
    misses exactly one of the softer tests (court record, or 6/10 form); None otherwise."""
    a, b = ids
    ra, rb = ranks.get(a) or UNRANKED, ranks.get(b) or UNRANKED
    gap = abs(ra - rb)
    if gap < RANK_GAP:
        return None
    fav, dog = (a, b) if ra <= rb else (b, a)
    fav_name, dog_name = (m["home"], m["away"]) if fav == a else (m["away"], m["home"])

    f10, d10 = (history.get(fav) or [])[:10], (history.get(dog) or [])[:10]
    fw, dw = sum(x["result"] == "W" for x in f10), sum(x["result"] == "W" for x in d10)
    if len(f10) < 10 or fw < 6:
        return None
    form_ok = fw >= 7

    court = m["court"]
    fms, dms = history.get(fav) or [], history.get(dog) or []
    fc, dc = _court_record(fms, court, True), _court_record(dms, court, True)
    window = "season"
    if fc["won"] + fc["lost"] < MIN_SAMPLE or dc["won"] + dc["lost"] < MIN_SAMPLE:
        window = "last12"
        fc, dc = _court_record(fms, court, False), _court_record(dms, court, False)
    fr, dr = _rate(fc), _rate(dc)
    court_ok = fr is not None and dr is not None and fr >= dr
    if not form_ok and not court_ok:
        return None  # more than one miss: not worth showing

    try:
        p = lab.predict(ds, fav, dog, court, m["level"], ranks.get(fav), ranks.get(dog))
        prob = round(100 * p, 1) if p is not None else None
    except Exception:
        prob = None
    score = (fw >= 8) + (prob is not None and prob >= 70) + (gap >= 100)
    passed = form_ok and court_ok
    grade = (["C", "B", "A", "A+"] if passed else ["D", "C", "B", "B"])[score]  # a miss caps the grade at B
    pct = lambda r: round(100 * r) if r is not None else None  # noqa: E731
    return {
        "match": m, "grade": grade, "passed": passed, "gap": gap, "model": prob,
        "fav": {"id": fav, "name": fav_name, "rank": ranks.get(fav)},
        "dog": {"id": dog, "name": dog_name, "rank": ranks.get(dog)},
        # [gap, last 10, court] in the order the report card shows them
        "checks": [
            {"ok": True},
            {"ok": form_ok, "warn": fw == 7, "fav10": [x["result"] for x in f10], "dog10": [x["result"] for x in d10]},
            {"ok": court_ok},
        ],
        "summary": {"favL10": fw, "dogL10": dw, "favL10n": len(f10), "dogL10n": len(d10),
                    "favCourt": pct(fr), "dogCourt": pct(dr), "favCourtRec": fc, "dogCourtRec": dc, "courtWindow": window},
        "history": {"form": bt["form8"] if fw >= 8 else bt["form7"]} if bt else None,
    }


_cache: dict = {}


def screen(ds, week: list[dict], bt: dict | None, get_history) -> list[dict]:
    """Report cards for this week's not-yet-started ATP + Challenger matches that pass."""
    hit = _cache.get("screen")
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    ranks = atp.current_ranks()
    names = {**ds.names, **{p["PlayerId"].upper(): p["Name"] for p in atp.rankings()}}
    todo = []
    for w in week:
        if w["state"] != "notstarted":
            continue
        a, b = (p["id"] for p in w["players"])
        m = {"status": "notstarted", "start": w["start"], "tournament": w["tournament"], "tour": w["tour"],
             "court": w["court"], "level": w["level"], "round": w["round"],
             "home": names.get(a, a), "away": names.get(b, b), "winner": None}
        todo.append((m, a, b))
    need = {pid for _, a, b in todo
            if abs((ranks.get(a) or UNRANKED) - (ranks.get(b) or UNRANKED)) >= RANK_GAP for pid in (a, b)}

    def hist(pid):
        try:
            return pid, get_history(pid)
        except Exception:
            return pid, []
    with ThreadPoolExecutor(8) as pool:
        history = dict(pool.map(hist, need))

    cards = [c for m, a, b in todo if (c := evaluate(m, (a, b), ranks, history, ds, bt))]
    order = {"A+": 0, "A": 1, "B": 2, "C": 3, "D": 4}
    cards.sort(key=lambda c: (not c["passed"], order[c["grade"]], c["match"]["start"] or 0))
    _cache["screen"] = (time.time(), cards)
    return cards
