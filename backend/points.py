"""Total points / games / sets estimate from serve & return stats.

Method (the standard statistical approach to tennis match totals):
  1. Each player's chance of winning a point on serve, from ATP serve/return stats
     (shrunk toward the tour average when the sample is small).
  2. Combine the two players with Barnett & Clarke (2005): server's serve strength
     minus the receiver's return strength, both measured relative to the tour average:
         p_A = avg + (serveA - avg) - (returnB - (1 - avg))
  3. Point-by-point Markov model of tennis scoring (O'Malley 2008; Newton & Keller
     2005): exact game and tiebreak outcome distributions, then Monte Carlo over
     sets to get the full distribution of total points, games and sets.
  4. Optional hybrid: keep the stats' overall serve level (which drives how long a
     match is) but shift the gap between the players so their match-win chance
     equals a better-calibrated win model (the active Tennis IQ model). Serve stats are good
     at "how long", rating models are better at "who wins".
"""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from functools import lru_cache
from math import comb

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression, QuantileRegressor
from sklearn.metrics import log_loss, roc_auc_score

import atp
import config

STATS_DIR = config.STATS_DIR
# Tour average share of serve points won, by court (ATP, roughly)
TOUR_SPW = {"outdoor_hard": 0.640, "indoor_hard": 0.652, "clay": 0.618, "grass": 0.662}
STATS_SURFACE = {"outdoor_hard": "hard", "indoor_hard": "hard", "clay": "clay", "grass": "grass"}
SHRINK_GAMES = 80  # service games of evidence needed to trust a player's own number half-way


# ---------------------------------------------------------------- player serve/return stats

def _fetch_stats(pid: str, year: int, surface: str) -> dict | None:
    """ATP serve/return numbers for one player-season. Past seasons never change, so
    they're kept on disk; the current season goes through the shared 6-hour cache."""
    url = f"{atp.BASE}/en/-/www/stats/{pid.lower()}/{year}/{surface}?v=1"
    if year == date.today().year:
        try:
            data = atp.fetch(url, ttl=6 * 3600)
        except Exception:
            return None
    else:
        f = STATS_DIR / f"{pid.upper()}_{year}_{surface}.json"
        try:
            data = json.loads(f.read_text())
        except (FileNotFoundError, ValueError):
            try:
                data = atp.request(url)
            except Exception:
                return None
            config.write_atomic(f, json.dumps(data))
    st = (data or {}).get("Stats") or {}
    sv, rt = st.get("ServiceRecordStats") or {}, st.get("ReturnRecordStats") or {}
    if not sv.get("ServiceGamesPlayed"):
        return None
    return {"spw": sv["ServicePointsWonPercentage"] / 100, "rpw": rt["ReturnPointsWonPercentage"] / 100,
            "games": sv["ServiceGamesPlayed"]}


def player_rates(pid: str, court: str, years: list[int]) -> dict | None:
    """Serve / return points-won estimate for one player on one court type.
    Pools surface-specific and all-surface stats from the given seasons and shrinks
    the result toward the tour average; returns None when ATP has no stats."""
    avg = TOUR_SPW[court]
    surface = STATS_SURFACE[court]
    pools = []
    for y in years:
        for surf, weight in ((surface, 1.0), ("all", 0.5)):
            s = _fetch_stats(pid, y, surf)
            if s:
                # convert all-surface numbers onto this court by shifting with the tour averages
                shift = avg - 0.640 if surf == "all" else 0.0
                pools.append((s["spw"] + shift, s["rpw"] - shift, s["games"] * weight))
    if not pools:
        return None
    n = sum(w for *_, w in pools)
    spw = sum(s * w for s, _, w in pools) / n
    rpw = sum(r * w for _, r, w in pools) / n
    k = n / (n + SHRINK_GAMES)
    return {"spw": avg + k * (spw - avg), "rpw": (1 - avg) + k * (rpw - (1 - avg)), "games": round(n)}


def combine(a: dict | None, b: dict | None, court: str) -> tuple[float, float]:
    """Barnett–Clarke: each player's chance of winning a point on their own serve."""
    avg = TOUR_SPW[court]
    a = a or {"spw": avg, "rpw": 1 - avg}
    b = b or {"spw": avg, "rpw": 1 - avg}
    pa = avg + (a["spw"] - avg) - (b["rpw"] - (1 - avg))
    pb = avg + (b["spw"] - avg) - (a["rpw"] - (1 - avg))
    return float(np.clip(pa, 0.35, 0.92)), float(np.clip(pb, 0.35, 0.92))


# ---------------------------------------------------------------- exact game / tiebreak models

@lru_cache(maxsize=4096)
def game_dist(p: float) -> tuple:
    """(P(server holds), {points: prob of holding in that many}, {points: prob of being broken})."""
    p = round(p, 3)
    q = 1 - p
    win, lose = {}, {}
    # games decided before deuce
    for k in range(3):  # opponent won k points
        win[4 + k] = comb(3 + k, k) * p ** 4 * q ** k
        lose[4 + k] = comb(3 + k, k) * q ** 4 * p ** k
    # deuce (3-3 reached after 6 points), then pairs of points
    deuce = comb(6, 3) * p ** 3 * q ** 3
    split = 2 * p * q
    for extra in range(0, 30):
        reach = deuce * split ** extra
        win[8 + 2 * extra] = win.get(8 + 2 * extra, 0) + reach * p * p
        lose[8 + 2 * extra] = lose.get(8 + 2 * extra, 0) + reach * q * q
    return sum(win.values()), win, lose


@lru_cache(maxsize=4096)
def tiebreak_dist(pa: float, pb: float) -> tuple:
    """Tiebreak with A serving first: (P(A wins), points distribution if A wins, if B wins)."""
    pa, pb = round(pa, 3), round(pb, 3)

    def a_serves(n):  # point n (0-based): A serves points 0, 3,4, 7,8, ...
        return n == 0 or ((n - 1) // 2) % 2 == 1

    state = {(0, 0): 1.0}
    win, lose = {}, {}
    for n in range(12):
        nxt = {}
        for (x, y), pr in state.items():
            p = pa if a_serves(n) else 1 - pb
            for (dx, dy), pp in (((1, 0), p), ((0, 1), 1 - p)):
                s = (x + dx, y + dy)
                if s[0] == 7 and s[1] <= 5:
                    win[sum(s)] = win.get(sum(s), 0) + pr * pp
                elif s[1] == 7 and s[0] <= 5:
                    lose[sum(s)] = lose.get(sum(s), 0) + pr * pp
                else:
                    nxt[s] = nxt.get(s, 0) + pr * pp
        state = nxt
    # 6-6 after 12 points: every two points each player serves once
    at66 = state.get((6, 6), 0.0)
    a_pair = pa * (1 - pb)          # A wins both points of a pair
    b_pair = (1 - pa) * pb          # B wins both
    tie = 1 - a_pair - b_pair
    for extra in range(30):
        reach = at66 * tie ** extra
        win[14 + 2 * extra] = win.get(14 + 2 * extra, 0) + reach * a_pair
        lose[14 + 2 * extra] = lose.get(14 + 2 * extra, 0) + reach * b_pair
    return sum(win.values()), win, lose


def _sampler(dist: dict):
    keys = np.array(list(dist.keys()))
    probs = np.array(list(dist.values()))
    return keys, probs / probs.sum()


# ---------------------------------------------------------------- match simulation

def simulate(pa: float, pb: float, best_of: int = 3, n: int = 6000, seed: int = 7) -> dict:
    """Monte Carlo over sets using exact game/tiebreak distributions."""
    rng = np.random.default_rng(seed)
    ga, wa, la = game_dist(pa)   # A serving
    gb, wb, lb = game_dist(pb)   # B serving
    ta, twa, tla = tiebreak_dist(pa, pb)      # A serves first in the tiebreak
    tb_, twb, tlb = tiebreak_dist(pb, pa)     # B serves first
    # Pre-sample point counts in bulk for speed
    pools = {}
    for name, dist in (("wa", wa), ("la", la), ("wb", wb), ("lb", lb),
                       ("twa", twa), ("tla", tla), ("twb", twb), ("tlb", tlb)):
        k, p = _sampler(dist)
        pools[name] = iter(rng.choice(k, size=n * 40, p=p).tolist())
    u = iter(rng.random(n * 80).tolist())
    need = best_of // 2 + 1

    pts_out = np.zeros(n, dtype=int)
    games_out = np.zeros(n, dtype=int)
    sets_out = np.zeros(n, dtype=int)
    a_won = np.zeros(n, dtype=bool)
    for i in range(n):
        sa = sb = 0
        server_a = next(u) < 0.5  # coin toss
        pts = games = 0
        while sa < need and sb < need:
            x = y = 0
            while True:
                if x == 6 and y == 6:
                    if server_a:
                        if next(u) < ta:
                            x += 1; pts += next(pools["twa"])
                        else:
                            y += 1; pts += next(pools["tla"])
                    else:
                        if next(u) < tb_:
                            y += 1; pts += next(pools["twb"])
                        else:
                            x += 1; pts += next(pools["tlb"])
                    server_a = not server_a
                    games += 1
                    break
                if server_a:
                    if next(u) < ga:
                        x += 1; pts += next(pools["wa"])
                    else:
                        y += 1; pts += next(pools["la"])
                else:
                    if next(u) < gb:
                        y += 1; pts += next(pools["wb"])
                    else:
                        x += 1; pts += next(pools["lb"])
                server_a = not server_a
                games += 1
                if (x >= 6 or y >= 6) and abs(x - y) >= 2:
                    break
            if x > y:
                sa += 1
            else:
                sb += 1
        pts_out[i], games_out[i], sets_out[i], a_won[i] = pts, games, sa + sb, sa > sb
    return {"points": pts_out, "games": games_out, "sets": sets_out, "a_won": a_won}


def match_win_prob(pa: float, pb: float, best_of: int = 3) -> float:
    """Exact-ish P(A wins) (set DP averaged over who serves first)."""
    ga, gb = game_dist(pa)[0], game_dist(pb)[0]
    ta = tiebreak_dist(pa, pb)[0]
    tb_ = 1 - tiebreak_dist(pb, pa)[0]
    set_p = []
    for a_first in (True, False):
        dp = {(0, 0): 1.0}
        won = 0.0
        for _ in range(13):
            nxt = {}
            for (x, y), pr in dp.items():
                a_serving = ((x + y) % 2 == 0) == a_first
                if x == 6 and y == 6:
                    won += pr * (ta if a_serving else tb_)
                    continue
                p = ga if a_serving else 1 - gb
                for s, pp in (((x + 1, y), p), ((x, y + 1), 1 - p)):
                    if (s[0] >= 6 or s[1] >= 6) and abs(s[0] - s[1]) >= 2:
                        if s[0] > s[1]:
                            won += pr * pp
                    else:
                        nxt[s] = nxt.get(s, 0) + pr * pp
            dp = nxt
        set_p.append(won)
    s = sum(set_p) / 2
    if best_of == 3:
        return s * s * (3 - 2 * s)
    return s ** 3 * (10 - 15 * s + 6 * s * s)


def calibrate(pa: float, pb: float, target: float, best_of: int = 3) -> tuple[float, float]:
    """Shift the serve gap so P(A wins) == target, keeping pa + pb (match length) fixed."""
    lo, hi = -0.25, 0.25
    for _ in range(30):
        mid = (lo + hi) / 2
        if match_win_prob(np.clip(pa + mid, .35, .92), np.clip(pb - mid, .35, .92), best_of) < target:
            lo = mid
        else:
            hi = mid
    d = (lo + hi) / 2
    return float(np.clip(pa + d, .35, .92)), float(np.clip(pb - d, .35, .92))


def summarize(sim: dict, best_of: int = 3) -> dict:
    pts, games, sets, aw = sim["points"], sim["games"], sim["sets"], sim["a_won"]
    need = best_of // 2 + 1
    lines = {}
    med = int(np.median(pts))
    for line in (med - 10.5, med - 5.5, med - 0.5, med + 4.5, med + 9.5):
        lines[str(line)] = round(100 * float((pts > line).mean()), 1)
    scorelines = {}
    set_p = {s: float((sets == s).mean()) for s in range(need, best_of + 1)}
    scale = {s: 1.0 for s in set_p}
    if "p3" in sim:  # re-weight 2-set vs 3-set outcomes to the calibrated third-set chance
        raw3, cal3 = sim["p3"]
        scale = {2: (1 - cal3) / max(1e-9, 1 - raw3), 3: cal3 / max(1e-9, raw3)}
        set_p = {2: 1 - cal3, 3: cal3}
    for s in range(need, best_of + 1):
        scorelines[f"a{need}{s - need}"] = round(100 * float(((sets == s) & aw).mean()) * scale[s], 1)
        scorelines[f"b{need}{s - need}"] = round(100 * float(((sets == s) & ~aw).mean()) * scale[s], 1)
    return {
        "points": {"median": med,
                   "range80": [int(np.percentile(pts, 10)), int(np.percentile(pts, 90))],
                   "over": lines,
                   "hist": np.histogram(pts, bins=range(int(pts.min()) // 10 * 10, int(pts.max()) // 10 * 10 + 20, 10))[0].tolist(),
                   "histStart": int(pts.min()) // 10 * 10},
        "games": {"median": int(np.median(games)),
                  "range80": [int(np.percentile(games, 10)), int(np.percentile(games, 90))]},
        "sets": {str(s): round(100 * v, 1) for s, v in set_p.items()},
        "scorelines": scorelines,
    }


def estimate(a: str, b: str, court: str, win_prob: float | None, best_of: int = 3) -> dict:
    """Points / games / sets estimate for a match between ATP ids a and b."""
    yr = date.today().year
    with ThreadPoolExecutor(2) as pool:
        ra, rb = pool.map(lambda pid: player_rates(pid, court, [yr, yr - 1]), (a, b))
    pa, pb = combine(ra, rb, court)
    if win_prob is not None:
        pa, pb = calibrate(pa, pb, win_prob, best_of)
    sim = simulate(pa, pb, best_of)
    rep = report()
    calibrated = False
    if rep and best_of == 3:
        # Corrections learned in the backtest: the raw model runs long
        a0, a1 = rep["calibration"]["points"]
        g0, g1 = rep["calibration"]["games"]
        c0, c1 = rep["calibration"]["three"]
        sim["points"] = np.round(a0 + a1 * sim["points"]).astype(int)
        sim["games"] = np.round(g0 + g1 * sim["games"]).astype(int)
        p3 = float(np.clip((sim["sets"] == 3).mean(), .01, .99))
        p3c = 1 / (1 + np.exp(-(c0 + c1 * np.log(p3 / (1 - p3)))))
        sim["p3"] = (p3, float(p3c))
        calibrated = True
    return {**summarize(sim, best_of), "calibrated": calibrated,
            "serve": {"a": round(100 * pa, 1), "b": round(100 * pb, 1)}, "stats": {"a": ra, "b": rb}}


# ---------------------------------------------------------------- strict backtest

REPORT_PATH = config.MODEL_DIR / "points_report.json"
HAWKEYE_CACHE = config.VAR / "hawkeye_points.json"


def _actual_points(key: str) -> int | None:
    """Total points played in a match, from ATP's Hawkeye stats ('2026-540-MS001-...')."""
    year, event, match = key.split("-")[:3]
    try:
        data = atp.request(f"{atp.BASE}/-/Hawkeye/MatchStats/Complete/{year}/{event}/{match.upper()}")
        sets = data["Match"]["PlayerTeam"]["SetScores"]
        return int(sets[0]["Stats"]["PointStats"]["TotalPointsWon"]["Divisor"])
    except Exception:
        return None


def _season_rows(ds, year: int):
    df = ds.df
    return df[(df.year == year) & df.level.isin(["1000", "500", "250", "WC"]) & df.complete
              & (df.is_bo5_actual == False) & df.court.isin(list(TOUR_SPW))]  # noqa: E712


def test_year(ds) -> int:
    """Latest season with enough finished tour matches to score on (early in January
    the new season is too thin, so it falls back to last year)."""
    y = int(ds.df.year.max())
    return y if len(_season_rows(ds, y)) >= 300 else y - 1


def needs_backtest(ds) -> bool:
    rep = report()
    return rep is None or rep.get("test_year") != test_year(ds)


def _eval_season(ds, year: int, sample: int, seed: int):
    """Predict `year`'s tour-level best-of-3 matches using only the season before's stats."""
    rows = _season_rows(ds, year)
    rows = rows.sample(min(sample, len(rows)), random_state=seed)
    try:
        cache = json.loads(HAWKEYE_CACHE.read_text())
    except (FileNotFoundError, ValueError):
        cache = {}
    todo = [k for k in rows.key if k not in cache]
    with ThreadPoolExecutor(6) as pool:
        for k, v in zip(todo, pool.map(_actual_points, todo)):
            cache[k] = v
    config.write_atomic(HAWKEYE_CACHE, json.dumps(cache))
    ids = set(rows.p1) | set(rows.p2)
    with ThreadPoolExecutor(6) as pool:  # warm the stats cache
        list(pool.map(lambda x: player_rates(x[0], x[1], [year - 1]), [(i, c) for i in ids for c in set(rows.court)]))
    out = []
    for _, r in rows.iterrows():
        ra, rb = player_rates(r.p1, r.court, [year - 1]), player_rates(r.p2, r.court, [year - 1])
        if not ra or not rb or cache.get(r.key) is None:
            continue
        pa, pb = calibrate(*combine(ra, rb, r.court), float(r.elo_prob), 3)
        sim = simulate(pa, pb, 3, n=2500, seed=11)
        out.append({"points": cache[r.key], "games": r.total_games, "three": int(r.went_deciding),
                    "closeness": 0.5 - abs(r.elo_prob - 0.5),
                    "m_pts": float(sim["points"].mean()), "m_games": float(sim["games"].mean()),
                    "m_three": float((sim["sets"] == 3).mean())})
    return pd.DataFrame(out)


def backtest(ds, sample: int = 700, seed: int = 1) -> dict:
    """Fit corrections on one season, then score once on the next (never seen) season."""
    ty = test_year(ds)
    val = _eval_season(ds, ty - 1, sample, seed)
    te = _eval_season(ds, ty, sample, seed)

    # Corrections learned on the validation season only
    # Median (not mean) regression: totals are skewed by three-setters, and the median
    # is what minimises the average miss
    lin_p = QuantileRegressor(quantile=0.5, alpha=0, solver="highs").fit(val[["m_pts"]], val.points)
    lin_g = QuantileRegressor(quantile=0.5, alpha=0, solver="highs").fit(val[["m_games"]], val.games)
    logit = lambda x: np.log(np.clip(x, .01, .99) / (1 - np.clip(x, .01, .99)))  # noqa: E731
    platt = LogisticRegression().fit(logit(val.m_three.to_numpy()).reshape(-1, 1), val.three)
    cal = {"points": [float(lin_p.intercept_), float(lin_p.coef_[0])],
           "games": [float(lin_g.intercept_), float(lin_g.coef_[0])],
           "three": [float(platt.intercept_[0]), float(platt.coef_[0][0])]}

    te["c_pts"] = lin_p.predict(te[["m_pts"]])
    te["c_games"] = lin_g.predict(te[["m_games"]])
    te["c_three"] = platt.predict_proba(logit(te.m_three.to_numpy()).reshape(-1, 1))[:, 1]
    mae = lambda p, a: round(float(np.abs(np.asarray(p) - np.asarray(a)).mean()), 2)  # noqa: E731
    base_pts, base_games, base3 = float(val.points.median()), float(val.games.median()), float(val.three.mean())

    # Over/under vs a hindsight line built from the test season itself, using only how
    # lopsided each match was. An unrealistically sharp line, on purpose.
    bins = pd.qcut(te.closeness, 5, labels=False, duplicates="drop")
    line = te.points.groupby(bins).transform("median").to_numpy() + 0.5
    act, pred = te.points.to_numpy(), te.c_pts.to_numpy()
    ou = []
    for edge in (2, 5, 10):
        over, under = pred >= line + edge, pred <= line - edge
        n = int(over.sum() + under.sum())
        pick = over | under
        ou.append({"edge": edge, "bets": n,
                   "hit": round(100 * float(((act > line) & over | (act < line) & under)[pick].mean()), 1) if n >= 100 else None,
                   "always_under": round(100 * float((act[pick] < line[pick]).mean()), 1) if n >= 100 else None})

    rep = {
        "test_year": ty, "val_year": ty - 1, "matches": int(len(te)), "val_matches": int(len(val)),
        "calibration": cal,
        "points": {
            "actual_mean": round(float(te.points.mean()), 1),
            "baseline_mae": mae(np.full(len(te), base_pts), te.points),
            "raw_mae": mae(te.m_pts, te.points), "model_mae": mae(te.c_pts, te.points),
            "raw_bias": round(float((te.m_pts - te.points).mean()), 1),
            "model_bias": round(float((te.c_pts - te.points).mean()), 1),
            "corr": round(float(np.corrcoef(te.c_pts, te.points)[0, 1]), 3),
            "over_under": ou,
        },
        "games": {
            "actual_mean": round(float(te.games.mean()), 2),
            "baseline_mae": mae(np.full(len(te), base_games), te.games),
            "raw_mae": mae(te.m_games, te.games), "model_mae": mae(te.c_games, te.games),
        },
        "sets": {
            "actual_three": round(100 * float(te.three.mean()), 1),
            "raw_three": round(100 * float(te.m_three.mean()), 1),
            "model_three": round(100 * float(te.c_three.mean()), 1),
            "baseline_log_loss": round(float(log_loss(te.three, np.full(len(te), base3))), 4),
            "raw_log_loss": round(float(log_loss(te.three, te.m_three.clip(.01, .99))), 4),
            "model_log_loss": round(float(log_loss(te.three, te.c_three)), 4),
            "auc": round(float(roc_auc_score(te.three, te.c_three)), 3),
        },
    }
    rep["verdict"] = _verdict(rep)
    config.write_atomic(REPORT_PATH, json.dumps(rep, indent=1))
    return rep


def report() -> dict | None:
    try:
        return json.loads(REPORT_PATH.read_text())
    except (FileNotFoundError, ValueError):
        return None


def _verdict(r: dict) -> list[str]:
    p, g, s = r["points"], r["games"], r["sets"]
    pct = lambda m, b: f"{100 * (b - m) / b:+.1f}%"  # noqa: E731
    out = [
        f"Raw textbook model (independent points): {p['raw_mae']} points off on average with a {p['raw_bias']:+} point "
        f"bias, and it called a third set {s['raw_three']}% of the time vs {s['actual_three']}% in reality. Real matches "
        f"are more lopsided than independent points assume, so the raw model is too long.",
        f"After the correction learned on {r['val_year']} and tested once on {r['test_year']} ({r['matches']} matches): "
        f"points {p['model_mae']} off vs {p['baseline_mae']} for always guessing the usual total "
        f"({pct(p['model_mae'], p['baseline_mae'])}); games {g['model_mae']} vs {g['baseline_mae']} "
        f"({pct(g['model_mae'], g['baseline_mae'])}); third set log loss {s['model_log_loss']} vs {s['baseline_log_loss']} "
        f"(AUC {s['auc']}, where 0.5 = coin flip).",
    ]
    good = [o for o in p["over_under"] if o["hit"] is not None]
    if good:
        o = max(good, key=lambda o: o["hit"])
        out.append(f"Over/under on points against a line that already knows how lopsided each match is: best setting hit "
                   f"{o['hit']}% on {o['bets']} matches (always-under on the same matches: {o['always_under']}%). "
                   f"Break-even at -110 is 52.4%. Settings with fewer than 100 bets aren't counted: too few to mean anything.")
    out.append("Bottom line: the number of sets and tiebreaks, which drives the point total, is close to a coin flip between "
               "two pros, and no public-stats model predicts it much better than the average. Read the estimate as "
               "the most likely shape of the match, not a betting edge.")
    return out
