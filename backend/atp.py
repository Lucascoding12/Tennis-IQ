"""Fetching from atptour.com, with one shared cache.

atptour.com sits behind Cloudflare, so every request goes through curl_cffi with a
Chrome TLS fingerprint. `fetch` retries transient failures, remembers failures for
a couple of minutes (so a missing headshot isn't re-requested on every page view),
falls back to the last good copy when a source is down, and keeps at most
CACHE_SIZE entries so a long-running server doesn't grow without bound.
"""
import json
import re
import threading
import unicodedata
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

from curl_cffi import requests

import config

BASE = "https://www.atptour.com"
REFRESH = 600          # seconds before player data is re-pulled from ATP
FAIL_TTL = 120         # seconds a failed request is remembered
CACHE_SIZE = 3000
_cache: OrderedDict[str, tuple[float, object, bool]] = OrderedDict()  # url -> (time, value, ok)
_lock = threading.Lock()


def request(url: str, kind: str = "json", attempts: int = 3):
    """One uncached GET with retries; kind is 'json', 'text' (raw JSON + parsed) or 'bytes'.
    JSON is parsed before returning, so a Cloudflare HTML page counts as a failure."""
    err = None
    for attempt in range(attempts):
        try:
            resp = requests.get(url, impersonate="chrome", timeout=20)
            resp.raise_for_status()
            if kind == "bytes":
                return resp.content, resp.headers.get("content-type", "image/png")
            data = resp.json()
            return (resp.text, data) if kind == "text" else data
        except Exception as e:
            err = e
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status and 400 <= status < 500:
                break  # not found / forbidden / rate-limited: retrying now won't help
            time.sleep(0.5 * (attempt + 1))
    raise err


def fetch(url: str, ttl: int, kind: str = "json"):
    """Cached GET. Serves the stale copy if a refresh fails."""
    with _lock:
        hit = _cache.get(url)
        if hit:
            _cache.move_to_end(url)
    now = time.time()
    if hit and now - hit[0] < (ttl if hit[2] else FAIL_TTL):
        if hit[2]:
            return hit[1]
        raise RuntimeError(f"recently failed: {url}")
    try:
        value, ok = request(url, kind), True
    except Exception:
        if hit and hit[2]:
            return hit[1]  # stale but better than an error page
        value, ok = None, False
    with _lock:
        _cache[url] = (now, value, ok)
        _cache.move_to_end(url)
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    if not ok:
        raise RuntimeError(f"fetch failed: {url}")
    return value


# ---------------------------------------------------------------- rankings & players

def rankings() -> list[dict]:
    """Current singles rankings (~2,300 players). Refreshed hourly."""
    return fetch(f"{BASE}/en/-/www/rank/sglroll/2500?v=1", ttl=3600)


_ranks: dict = {}


def current_ranks() -> dict[str, int]:
    rows = rankings()
    if _ranks.get("src") is not rows:
        _ranks.update(src=rows, map={p["PlayerId"].upper(): p["RankNo"] for p in rows})
    return _ranks["map"]


def search(q: str, limit: int = 8) -> list[dict]:
    q = q.strip().lower()
    if not q:
        return []
    out = []
    for p in rankings():
        name = p["Name"].lower()
        if q in name:
            starts = any(part.startswith(q) for part in name.split())  # prefer first/last-name prefix matches
            out.append((0 if starts else 1, p["RankNo"], p))
    out.sort(key=lambda t: (t[0], t[1]))
    return [{"id": p["PlayerId"].upper(), "name": p["Name"], "rank": p["RankNo"], "country": p["CountryCode"]}
            for _, _, p in out[:limit]]


def profile(pid: str) -> dict:
    h = fetch(f"{BASE}/en/-/www/players/hero/{pid.lower()}?v=1", ttl=REFRESH)
    return {
        "id": pid.upper(), "name": f'{h["FirstName"]} {h["LastName"]}',
        "country": h.get("NatlId"), "countryName": h.get("Nationality"),
        "age": h.get("Age"), "proYear": h.get("ProYear"),
        "rank": h.get("SglRank"), "highRank": h.get("SglHiRank"),
        "highRankDate": (h.get("SglHiRankDate") or "")[:10] or None,
        "plays": (h.get("PlayHand") or {}).get("Description"),
        "backhand": (h.get("BackHand") or {}).get("Description"),
        "heightCm": h.get("HeightCm"), "titles": h.get("SglCareerTitles"),
    }


def headshot(pid: str) -> tuple[bytes, str]:
    return fetch(f"{BASE}/-/media/alias/player-headshot/{pid.lower()}", ttl=7 * 86400, kind="bytes")


def _score(m: dict, flip: bool = False) -> str:
    """Set scores from the player's side ('6-4 7-6(5)'), or the opponent's when flip."""
    sets = []
    for i in range(1, 6):
        a, b = m.get(f"Set{i}Player"), m.get(f"Set{i}Opponent")
        if a is None or b is None:
            break
        if flip:
            a, b = b, a
        tie = m.get(f"Set{i}Tie")
        sets.append(f"{a}-{b}" + (f"({tie})" if tie is not None else ""))
    return " ".join(sets)


def court(surface: str, in_out: str) -> str:
    """Bucket a tournament into the court types the site reports on."""
    s = (surface or "").lower()
    if s in ("hard", "carpet"):
        return "indoor_hard" if in_out == "I" or s == "carpet" else "outdoor_hard"
    return s if s in ("clay", "grass") else "other"


def _stats_key(url: str | None) -> str | None:
    """'.../match-stats/archive/2026/540/ms001' -> '2026/540/MS001'."""
    m = re.search(r"(\d{4})/(\w+)/(\w+)$", url or "")
    return f"{m[1]}/{m[2]}/{m[3].upper()}" if m else None


def matches(pid: str) -> list[dict]:
    """Every singles match in a player's career, most recent first."""
    try:
        data = fetch(f"{BASE}/en/-/www/activity/sgl/{pid.lower()}/?v=1", ttl=REFRESH)
    except Exception:
        saved = config.ACTIVITY_DIR / f"{pid.upper()}.json"
        if not saved.exists():
            raise
        data = json.loads(saved.read_text())  # last crawled copy
    ranks = current_ranks()
    today = date.today()
    out = []
    for year in data.get("Activity") or []:
        for t in year.get("Tournaments") or []:
            event_date = (t.get("EventDate") or "")[:10]
            if not event_date or datetime.fromisoformat(event_date).date() > today:
                continue
            for m in t.get("Matches") or []:
                if m.get("IsBye") or m.get("WinLoss") not in ("W", "L"):
                    continue
                opp = (m.get("OpponentId") or "").upper()
                out.append({
                    "date": event_date, "endDate": (t.get("PlayEndDate") or "")[:10] or None,
                    "tournament": t.get("EventDisplayName") or t.get("EventName"),
                    "level": t.get("EventType"), "surface": t.get("Surface"),
                    "court": court(t.get("Surface"), t.get("InOutdoor")),
                    "round": (m.get("Round") or {}).get("ShortName"),
                    "roundId": int((m.get("Round") or {}).get("Id") or 0),
                    "result": m["WinLoss"], "opponentId": opp,
                    "opponent": f'{m.get("OpponentFirstName") or ""} {m.get("OpponentLastName") or ""}'.strip(),
                    "opponentRankNow": ranks.get(opp), "score": _score(m),
                    "stats": _stats_key(m.get("MatchStatsUrl")),
                })
    out.sort(key=lambda m: (m["date"], m["roundId"]), reverse=True)
    return out


def h2h(p1: str, p2: str) -> list[dict]:
    data = fetch(f"{BASE}/en/-/www/h2h/{p1.lower()}/{p2.lower()}?v=1", ttl=REFRESH)
    out = []
    for t in (data.get("Tournaments") or []) + (data.get("OtherTournaments") or []):
        for m in t.get("Matches") or []:
            if m.get("Winner"):
                out.append({
                    "year": t.get("EventYear"), "tournament": t.get("EventDisplayName") or t.get("EventName"),
                    "level": t.get("EventType"), "surface": t.get("Surface"),
                    "court": court(t.get("Surface"), t.get("InOutdoor")),
                    "round": (m.get("Round") or {}).get("ShortName"), "winner": m["Winner"].upper(),
                    "score": m.get("ResultString"), "note": m.get("Reason"),
                    "stats": _stats_key(m.get("MatchStatsUrl")),
                })
    out.sort(key=lambda m: m["year"] or 0, reverse=True)
    return out


def duration(key: str | None) -> int | None:
    """Match length in minutes from ATP's Hawkeye stats feed (None if not recorded)."""
    if not key:
        return None
    try:
        match = fetch(f"{BASE}/-/Hawkeye/MatchStats/Complete/{key}", ttl=30 * 86400).get("Match") or {}
    except Exception:
        return None
    t = match.get("MatchTimeTotal") or match.get("MatchTime")
    if not t:
        return None
    h, m, *_ = (int(x) for x in t.split(":"))
    return h * 60 + m or None


# ---------------------------------------------------------------- this week's tournaments
# The career-history feed lags a few days behind, and ATP has no public live-scores
# feed, so this week's results and order of play are read from the tournament pages
# (rendered on the server). Pages are rate-limited harder than the JSON feeds, so
# each is read at most every PAGE_TTL seconds, and after a 429 we back off entirely.

PAGE_TTL = 300
COOLDOWN = 180
_cooldown = {"until": 0.0}


def _page(path: str) -> str:
    url = BASE + path
    with _lock:
        hit = _cache.get(url)
    if hit and hit[2] and time.time() - hit[0] < PAGE_TTL:
        return hit[1]
    if time.time() < _cooldown["until"]:
        if hit and hit[2]:
            return hit[1]
        raise RuntimeError("atptour.com asked us to slow down")
    resp = requests.get(url, impersonate="chrome", timeout=20)
    if resp.status_code == 429:
        _cooldown["until"] = time.time() + COOLDOWN
    if resp.status_code != 200 or "<html" not in resp.text[:2000].lower():
        if hit and hit[2]:
            return hit[1]  # serve the last good copy
        raise RuntimeError(f"atptour.com page failed ({resp.status_code}): {path}")
    with _lock:
        _cache[url] = (time.time(), resp.text, True)
        _cache.move_to_end(url)
    return resp.text


def calendar() -> list[dict]:
    """Tour + Challenger events: name, level, court type and their results/schedule pages."""
    out = []
    for tour in ("tour", "challenger"):
        try:
            data = fetch(f"{BASE}/en/-/tournaments/calendar/{tour}?v=1", ttl=6 * 3600)
        except Exception:
            continue
        for month in data.get("TournamentDates") or []:
            for t in month.get("Tournaments") or []:
                out.append({"name": t.get("Name"), "live": t.get("IsLive"), "level": t.get("Type"),
                            "court": court(t.get("Surface"), "I" if t.get("IndoorOutdoor") == "Indoor" else "O"),
                            "results": t.get("ScoresUrl"), "schedule": t.get("ScheduleUrl")})
    return out


_PLAYER = re.compile(r'/en/players/[^/"]+/(\w{4})/overview">\s*(.*?)\s*</a>', re.S)


def _text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def _side(html: str) -> dict | None:
    """One player's block: id, winner flag, set scores. None for doubles teams."""
    players = _PLAYER.findall(html)
    if len(players) != 1:
        return None
    sets = []
    for cell in re.findall(r'<div class="score-item">(.*?)</div>', html, re.S):
        nums = [int(x) for x in re.findall(r"<span>(\d+)</span>", cell)]
        if nums:
            sets.append((nums[0], nums[1] if len(nums) > 1 else None))  # (games, tiebreak points)
    return {"id": players[0][0].upper(), "winner": 'class="winner"' in html, "sets": sets}


def _results(path: str) -> list[dict]:
    out = []
    for day_block in _page(path).split('class="atp_accordion-item"')[1:]:
        day = re.search(r"<h4>\s*\w+, (\d+ \w+, \d{4})", day_block)
        iso = datetime.strptime(day[1], "%d %B, %Y").date().isoformat() if day else date.today().isoformat()
        for block in day_block.split('<div class="match">')[1:]:
            sides = [_side(x) for x in block.split('<div class="stats-item">')[1:3]]
            if len(sides) != 2 or None in sides or not any(s["winner"] for s in sides):
                continue
            head = [_text(x) for x in re.findall(r"<span>(.*?)</span>", block.split('class="match-content"')[0], re.S)]
            length = re.match(r"(\d+):(\d+)", head[1]) if len(head) > 1 else None
            out.append({"state": "finished", "date": iso, "round": head[0].split(" - ")[0] if head else "",
                        "minutes": int(length[1]) * 60 + int(length[2]) if length else None, "players": sides})
    return out


def _schedule(path: str) -> list[dict]:
    out = []
    for block in _page(path).split('<div class="schedule" ')[1:]:
        parts = re.split(r'<div class="(?:player|opponent)">', block)
        sides = [_side(x) for x in parts[1:3]]
        when = re.search(r'data-datetime="([^"]+)"', block)  # UTC
        if len(sides) != 2 or None in sides or not when:
            continue
        rnd = re.search(r'<div class="schedule-type">\s*(.*?)\s*</div>', block, re.S)
        score = re.search(r'schedule-cta-score">(.*?)</span>', block, re.S)
        started = bool(score and re.search(r"\d", score[1]))
        start = datetime.fromisoformat(when[1]).replace(tzinfo=timezone.utc)
        out.append({"state": "inprogress" if started else "notstarted", "start": int(start.timestamp()),
                    "round": _text(rnd[1]) if rnd else "", "players": sides})
    return out


def _info(t: dict) -> dict:
    return {"tournament": t["name"], "tour": "Challenger" if t["level"] == "CH" else "ATP",
            "level": t["level"] or "250", "court": t["court"]}


def week_matches() -> list[dict]:
    """This week's singles matches at every live ATP and Challenger event. Per event:
    ATP's tournament pages (start times, everything); if those are blocked, ESPN's
    scoreboard for tour events, and the draw (match-stats feed) for Challengers."""
    live = [t for t in calendar() if t["live"]]
    out, blocked = [], []
    for t in live:
        ms = _event_from_pages(t)
        if ms is None:
            blocked.append(t)
        else:
            out += ms
    if blocked:
        espn = _week_from_espn(blocked)
        covered = {m["tournament"] for m in espn}
        out += espn
        for t in blocked:
            if t["name"] not in covered:
                out += _event_from_draw(t)
    return out


def _event_from_pages(t: dict) -> list[dict] | None:
    """Results + order of play from the event's pages; None if ATP blocked them."""
    try:
        done = _results(t["results"]) if t["results"] else []
        todo = _schedule(t["schedule"]) if t["schedule"] else []
    except Exception:
        return None
    played = [{p["id"] for p in m["players"]} for m in done]
    todo = [m for m in todo if {p["id"] for p in m["players"]} not in played]  # order of play repeats finished ones
    return [{**m, **_info(t)} for m in done + todo]


_draw_done: dict[str, dict] = {}  # finished matches never change, so each is fetched once
_ROUND_BY_SLOT = [(1, "Final"), (2, "Semifinal"), (4, "Quarterfinal"), (8, "Round of 16"), (16, "Round of 32")]


def _draw_match(year: int, event: str, slot: int) -> dict | None:
    key = f"{year}/{event}/MS{slot:03d}"
    if key in _draw_done:
        return _draw_done[key]
    try:
        m = (fetch(f"{BASE}/-/Hawkeye/MatchStats/Complete/{key}", ttl=PAGE_TTL) or {}).get("Match")
    except Exception:
        return None
    winner = (m or {}).get("WinningPlayerId")
    if not winner:
        return None
    sides = []
    for team in ("PlayerTeam", "OpponentTeam"):
        t = m.get(team) or {}
        pid = ((t.get("Player") or {}).get("PlayerId") or "").upper()
        sets = [(int(x["SetScore"]), x.get("TieBreakScore")) for x in t.get("SetScores") or []
                if x.get("SetNumber") and x.get("SetScore") is not None]
        sides.append({"id": pid, "winner": pid == winner.upper(), "sets": sets})
    length = re.match(r"(\d+):(\d+)", m.get("MatchTime") or "")
    _draw_done[key] = {"slot": slot, "players": sides, "round": m.get("RoundName") or "",
                       "minutes": int(length[1]) * 60 + int(length[2]) if length else None}
    with _lock:
        _cache.pop(f"{BASE}/-/Hawkeye/MatchStats/Complete/{key}", None)  # kept in _draw_done instead
    return _draw_done[key]


def _event_from_draw(t: dict) -> list[dict]:
    """Challenger fallback: finished main-draw matches from the match-stats feed, plus the
    next matches whose two feeder matches are both done (winners of slot 2k and 2k+1 meet
    in slot k). No start times in this mode."""
    event = re.search(r"/(\d+)/(?:results|daily-schedule)", t["results"] or t["schedule"] or "")
    if not event:
        return []
    year = date.today().year

    def load(ks):
        with ThreadPoolExecutor(4) as pool:
            return dict(zip(ks, pool.map(lambda k: _draw_match(year, event[1], k), ks)))
    slots = load(list(range(1, 16)))  # final .. round of 16
    # round-of-32 slots only matter for round-of-16 matches still to be played
    slots.update(load([j for k in range(8, 16) if not slots[k] for j in (2 * k, 2 * k + 1)]))
    out, today = [], date.today().isoformat()
    for k, m in slots.items():
        if m:
            out.append({"state": "finished", "date": today, "round": m["round"], "minutes": m["minutes"],
                        "players": m["players"], **_info(t)})
        elif k < 16 and slots.get(2 * k) and slots.get(2 * k + 1):
            feeders = [slots[2 * k], slots[2 * k + 1]]
            players = [{"id": next(p["id"] for p in f["players"] if p["winner"]), "winner": False, "sets": []} for f in feeders]
            rnd = next(name for start, name in reversed(_ROUND_BY_SLOT) if k >= start)
            out.append({"state": "notstarted", "start": None, "round": rnd, "players": players, **_info(t)})
    return out


# ---------------------------------------------------------------- ESPN backup

def _norm(name: str) -> str:
    name = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    return " ".join(name.replace("-", " ").replace(".", " ").split())


def player_id(name: str) -> str | None:
    """ATP id for a player's display name, via the rankings list ('Jannik Sinner' or 'J. Sinner')."""
    idx = _ranks.get("names")
    if _ranks.get("names_src") is not rankings():
        idx = {}
        for p in sorted(rankings(), key=lambda p: -p["RankNo"]):  # better-ranked players win collisions
            n = _norm(p["Name"]).split()
            idx[" ".join(n)] = idx[f"{n[0][0]} {n[-1]}"] = p["PlayerId"].upper()
        _ranks.update(names=idx, names_src=rankings())
    n = _norm(name).split()
    return (idx.get(" ".join(n)) or idx.get(f"{n[0][0]} {n[-1]}")) if n else None


def _week_from_espn(live: list[dict]) -> list[dict]:
    try:
        board = fetch("https://site.api.espn.com/apis/site/v2/sports/tennis/atp/scoreboard", ttl=PAGE_TTL)
    except Exception:
        return []
    out = []
    for ev in board.get("events") or []:
        name = _norm(ev.get("name"))
        t = next((t for t in live if _norm(t["name"]) == name), None) or \
            next((t for t in live if t["level"] != "CH" and set(_norm(t["name"]).split()) & set(name.split())), None)
        if t is None:
            continue
        info = _info(t)
        for g in ev.get("groupings") or []:
            if "Singles" not in g["grouping"]["displayName"]:
                continue
            for c in g.get("competitions") or []:
                comps = sorted(c.get("competitors") or [], key=lambda x: x.get("order", 0))
                ids = [player_id((x.get("athlete") or {}).get("displayName", "")) for x in comps]
                if len(comps) != 2 or None in ids:
                    continue  # TBD slot or a name we can't match
                sides = [{"id": pid, "winner": bool(x.get("winner")),
                          "sets": [(int(ls.get("value", 0)), ls.get("tiebreak")) for ls in x.get("linescores") or []]}
                         for pid, x in zip(ids, comps)]
                state = c["status"]["type"]["state"]
                when = datetime.fromisoformat(c["date"].replace("Z", "+00:00"))
                rnd = (c.get("round") or {}).get("displayName", "")
                if state == "post" and any(x["winner"] for x in sides):
                    for x, o in ((sides[0], sides[1]), (sides[1], sides[0])):  # ESPN puts tiebreaks on both sides
                        x["sets"] = [(g_, tb if g_ < og else None) for (g_, tb), (og, _) in zip(x["sets"], o["sets"])]
                    out.append({"state": "finished", "date": when.date().isoformat(), "round": rnd, "minutes": None,
                                "players": sides, **info})
                elif state in ("pre", "in"):
                    out.append({"state": "notstarted" if state == "pre" else "inprogress", "start": int(when.timestamp()),
                                "round": rnd, "players": sides, **info})
    return out
