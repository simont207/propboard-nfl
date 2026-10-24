"""Prop-Line (https://prop-line.com) player-prop odds, converted into the same `lines` format the site
already builds from SportsGameOdds, so EV, best price, alt-line chips and Top Props all keep working
unchanged:

    lines["<norm name>|<our market>|<our game id>"] = {
        "line", "fair_line", "over", "under", "fair_over", "fair_under",
        "books": [{"book", "line", "over", "under"}, ...],
        "alts":  [{"t", "over": {"book", "odds"}, "under": {...}}, ...],
    }

Why it is cheaper than SGO: Prop-Line counts REQUESTS, not games. One bulk call returns every game in a
sport with every market asked for, so refreshing NFL + NBA + NHL is about 3-4 requests against a free
allowance of 1,000/day.
"""
import datetime
import json
import os
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

import requests

API = "https://api.prop-line.com/v1"
KEY_FILE = Path(__file__).parent / "data" / "proline_key.txt"

# Real sportsbooks a bettor can use: shown in the Odds panel and eligible for "best price". DFS apps
# (PrizePicks, Underdog...) pay fixed multipliers and exchanges (Kalshi, Novig...) work differently, so
# neither is a comparable price; both are ignored.
BETTABLE = ["fanduel", "draftkings", "betmgm", "caesars", "espnbet", "fanatics", "hardrock", "betrivers",
            "bovada", "thescorebet", "unibet", "betway"]
# Sharpest first. Only used to work out the no-vig ("fair") price. Pinnacle can't be bet from the US, so it
# never appears in the books list.
REFERENCE = ["pinnacle", "bovada"]
MAX_AGE_S = 6 * 3600           # a market whose last update is older than this is treated as stale
# "Fair price" guards. Multiplicative no-vig math is unreliable for longshots (it hands them too much
# probability), and a single soft book's own prices are not a trustworthy yardstick -- both produced
# absurd +70% to +190% "EV" in testing, so fair odds are only claimed when they are believable.
SHARP_RANGE = (0.10, 0.90)     # Pinnacle / Bovada: the true probability must sit in this range
CONSENSUS_RANGE = (0.20, 0.80) # average of several books: tighter, since there is no sharp anchor
CONSENSUS_MIN = 3              # books needed for a consensus

_RUNG = re.compile(r"(\d+)\s*\+")
_TEAM_SUFFIX = re.compile(r"\s*\([A-Za-z]{2,4}\)\s*$")


def load_key():
    return (os.environ.get("PROPLINE_API_KEY", "").strip()
            or (KEY_FILE.read_text().strip() if KEY_FILE.exists() else "") or None)


def _get(path, key, **params):
    last = None
    for attempt in range(3):
        try:
            r = requests.get(f"{API}{path}", headers={"X-API-Key": key}, params=params, timeout=180)
        except requests.RequestException as e:
            last = str(e)
            time.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code == 200:
            return r.json(), r.headers
        if r.status_code in (429, 500, 502, 503, 504):
            last = f"HTTP {r.status_code}"
            time.sleep(2 * (attempt + 1))
            continue
        try:
            msg = r.json().get("detail", {}).get("message") or r.text[:200]
        except Exception:
            msg = r.text[:200]
        raise RuntimeError(f"Prop-Line HTTP {r.status_code}: {msg}")
    raise RuntimeError(f"Prop-Line request failed: {last}")


# ------------------------------------------------------------------ odds math ---
def american_to_prob(o):
    return 100 / (o + 100) if o > 0 else -o / (-o + 100)


def prob_to_american(p):
    if p is None or p <= 0.0001 or p >= 0.9999:
        return None
    return int(round(-p / (1 - p) * 100)) if p >= 0.5 else int(round((1 - p) / p * 100))


def _devig(over, under):
    """Multiplicative no-vig: scale both implied probabilities so they sum to 1."""
    po, pu = american_to_prob(over), american_to_prob(under)
    return po / (po + pu), pu / (po + pu)


def _valid_price(p):
    return isinstance(p, (int, float)) and abs(p) >= 100


# ------------------------------------------------------- event <-> our game matching ---
def _code(url):
    m = re.search(r"/([A-Za-z0-9]+)\.png", url or "")
    return m.group(1).lower() if m else None


def _iso(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def _tn(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _same_team(g, side, event):
    """One side of a game: the ESPN logo file name agrees (NBA, most NHL) OR the full team name agrees
    (NFL, whose logos are numeric ids on Prop-Line's side, and Utah, which each feed spells differently)."""
    ev_logo = _code(event.get(f"{side}_team_logo_url"))
    ours = {_code(g.get(f"{side}_logo")), g[side].lower()} - {None}
    if ev_logo and ev_logo in ours:
        return True
    return bool(_tn(g.get(f"{side}_name")) and _tn(g.get(f"{side}_name")) == _tn(event.get(f"{side}_team")))


def match_game(event, games):
    """Prop-Line event -> our game, by team (logo code or full name) and kickoff time. Returns None for
    games we don't show."""
    try:
        start = _iso(event["commence_time"])
    except Exception:
        return None
    best, best_gap = None, None
    for g in games:
        if not (_same_team(g, "home", event) and _same_team(g, "away", event)):
            continue
        try:
            gap = abs((_iso(g["date"]) - start).total_seconds())
        except Exception:
            gap = 0
        if gap <= 36 * 3600 and (best is None or gap < best_gap):
            best, best_gap = g, gap
    return best


# ------------------------------------------------------------------- parsing ---
def _parse_outcome(o):
    """-> (kind, side, point). kind: 'ou' (Over/Under at a point), 'rung' (N+ milestone => over N-0.5)."""
    name = (o.get("name") or "").strip()
    low = name.lower()
    if low in ("over", "under"):
        pt = o.get("point")
        return ("ou", low, float(pt)) if pt is not None else None
    if low in ("yes", "no"):
        return ("ou", "over" if low == "yes" else "under", 0.5)
    m = _RUNG.search(name)
    if m:
        return ("rung", "over", float(m.group(1)) - 0.5)
    return None


def _fresh(market, now):
    if market.get("suspended_at"):
        return False
    lu = market.get("last_update")
    if not lu:
        return True
    try:
        return (now - _iso(lu).timestamp()) <= MAX_AGE_S
    except Exception:
        return True


def build_lines(events, games, market_map, binary, norm, period=None, now=None):
    """Turn Prop-Line events into site-format `lines`. `market_map` maps a Prop-Line market key to our
    market key; `binary` is the set of our yes/no markets (anytime TD, double-double...)."""
    now = now or time.time()
    lines, matched, skipped_live = {}, 0, 0
    for e in events:
        if e.get("live") or not e.get("bookmakers"):
            skipped_live += 1 if e.get("live") else 0
            continue
        try:
            if _iso(e["commence_time"]).timestamp() <= now:
                skipped_live += 1
                continue
        except Exception:
            continue
        g = match_game(e, games)
        if not g:
            continue
        matched += 1
        # per[(player, mkey)] -> {"main": {book: {line, over, under}}, "alts": {t: {side: (price, book)}}}
        per = defaultdict(lambda: {"main": {}, "alts": defaultdict(dict)})
        for b in e["bookmakers"]:
            bk = b.get("key")
            if bk not in BETTABLE and bk not in REFERENCE:
                continue
            for m in b.get("markets") or []:
                if (m.get("period") or None) != period:
                    continue
                mkey = market_map.get(m.get("key"))
                if not mkey or not _fresh(m, now):
                    continue
                lt = m.get("line_type")
                anytime = "anytime" in (m.get("description") or "").lower()
                for o in m.get("outcomes") or []:
                    player, price = o.get("description"), o.get("price")
                    if not player or not _valid_price(price):
                        continue
                    player = _TEAM_SUFFIX.sub("", player)
                    parsed = _parse_outcome(o)
                    if not parsed and anytime and o.get("point") is None:
                        parsed = ("ou", "over", 0.5)      # FanDuel/Bovada: the outcome IS the player, price = yes
                    if not parsed:
                        continue
                    kind, side, pt = parsed
                    if mkey in binary and not (kind == "ou" and pt == 0.5):
                        continue        # anytime-TD style markets: only the yes/no (0.5) form counts
                    slot = per[(norm(player), mkey)]
                    if kind == "ou" and lt == "main":
                        ent = slot["main"].setdefault(bk, {"line": pt, "over": None, "under": None})
                        if ent["line"] == pt:
                            ent[side] = price
                    elif bk in BETTABLE and not (mkey in binary):
                        cur = slot["alts"][pt].get(side)
                        if cur is None or price > cur[0]:
                            slot["alts"][pt][side] = (price, bk)
        for (name, mkey), slot in per.items():
            ent = _entry(slot)
            if ent:
                lines[f"{name}|{mkey}|{g['id']}"] = ent
    return lines, matched, skipped_live


def _entry(slot):
    main = slot["main"]
    bet = {b: v for b, v in main.items() if b in BETTABLE and v["line"] is not None
           and (v["over"] is not None or v["under"] is not None)}
    if not bet:
        return None
    # the line most sportsbooks agree on; ties go to the line the sharp reference book is quoting
    counts = Counter(v["line"] for v in bet.values())
    ref_line = next((main[r]["line"] for r in REFERENCE if r in main), None)
    modal = sorted(counts, key=lambda L: (-counts[L], 0 if L == ref_line else 1, L))[0]
    at = [v for v in bet.values() if v["line"] == modal]
    over = max((v["over"] for v in at if v["over"] is not None), default=None)
    under = max((v["under"] for v in at if v["under"] is not None), default=None)

    po = pu = None
    fair_src = None
    for r in REFERENCE:
        v = main.get(r)
        if v and v["line"] == modal and v["over"] is not None and v["under"] is not None:
            p1, p2 = _devig(v["over"], v["under"])
            if SHARP_RANGE[0] <= p1 <= SHARP_RANGE[1]:
                po, pu, fair_src = p1, p2, r
            break
    if po is None and fair_src is None:
        two = [v for v in bet.values() if v["line"] == modal and v["over"] is not None and v["under"] is not None]
        if len(two) >= CONSENSUS_MIN:      # average of several sportsbooks' no-vig prices
            pairs = [_devig(v["over"], v["under"]) for v in two]
            c1, c2 = sum(p[0] for p in pairs) / len(pairs), sum(p[1] for p in pairs) / len(pairs)
            if CONSENSUS_RANGE[0] <= c1 <= CONSENSUS_RANGE[1]:
                po, pu, fair_src = c1, c2, f"consensus of {len(two)}"

    order = ["fanduel", "draftkings", "betmgm", "caesars", "espnbet", "fanatics"]
    books = sorted(({"book": b, "line": v["line"], "over": v["over"], "under": v["under"]} for b, v in bet.items()),
                   key=lambda x: order.index(x["book"]) if x["book"] in order else 99)
    alts = []
    for t in sorted(slot["alts"]):
        sides = slot["alts"][t]
        item = {"t": t}
        for s in ("over", "under"):
            if s in sides:
                item[s] = {"book": sides[s][1], "odds": sides[s][0]}
        alts.append(item)
    return {"line": modal, "fair_line": modal if po is not None else None, "over": over, "under": under,
            "fair_over": prob_to_american(po) if po is not None else None,
            "fair_under": prob_to_american(pu) if pu is not None else None, "fair_src": fair_src,
            "books": books, "alts": alts}


# --------------------------------------------------------------------- pulling ---
def pull(sport_key, games, market_map, binary, norm, period_map=None, key=None):
    """Fetch one sport and return {"pulled", "events", "lines", "requests", "remaining"}.
    `period_map` optionally maps Prop-Line market keys to our first-quarter markets (fetched with
    period=q1; one extra request)."""
    key = key or load_key()
    if not key:
        raise RuntimeError("No Prop-Line key found at data/proline_key.txt.")
    lines, events, requests_used, remaining = {}, 0, 0, None
    passes = [(market_map, None, None)]
    if period_map:
        passes.append((period_map, "q1", "q1"))
    for mmap, api_period, tag in passes:
        params = {"markets": ",".join(mmap)}
        if api_period:
            params["period"] = api_period
        data, headers = _get(f"/sports/{sport_key}/odds", key, **params)
        requests_used += 1
        remaining = headers.get("x-daily-remaining", remaining)
        evs = data if isinstance(data, list) else data.get("data", [])
        got, matched, _ = build_lines(evs, games, mmap, binary, norm, period=tag)
        lines.update(got)
        events = max(events, matched)
    return {"pulled": time.time(), "events": events, "lines": lines, "requests": requests_used,
            "remaining": remaining}


# ------------------------------------------------------------- loading for the site ---
LINES_MAX_AGE = 30 * 3600      # lines older than this are dropped rather than shown as if they were current


def _read(path):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return None


def load_merged(sgo_path, proline_path, now=None):
    """What the sport modules call as `load_sgo()`: Prop-Line's lines win; any SportsGameOdds lines that
    are still fresh fill in the markets Prop-Line doesn't carry (e.g. NFL targets)."""
    now = now or time.time()
    sgo, pl = _read(sgo_path) or {}, _read(proline_path) or {}
    lines = {}
    for src in (sgo, pl):                       # later source overwrites earlier: Prop-Line wins
        if src.get("pulled") and now - src["pulled"] <= LINES_MAX_AGE:
            lines.update(src.get("lines") or {})
    pulled = max([t for t in (sgo.get("pulled"), pl.get("pulled")) if t], default=None)
    return {"pulled": pulled, "remaining": sgo.get("remaining"), "lines": lines,
            "events": (pl.get("events") or 0) + (sgo.get("events") or 0),
            "proline_pulled": pl.get("pulled")}


# ------------------------------------------------------ putting lines onto prop rows ---
def best_book_price(books, side):
    """Among books quoting the same line AS EACH OTHER (the line most books agree on), whichever pays the
    best odds for that side. None if fewer than 2 books agree on a line: comparing odds across different
    lines isn't apples to apples."""
    lined = []
    for b in books:
        if b.get("line") is None or b.get(side) is None:
            continue
        try:
            lined.append((float(b["line"]), b))
        except (TypeError, ValueError):
            continue
    if len(lined) < 2:
        return None
    counts = Counter(t for t, _ in lined)
    modal_line, n = counts.most_common(1)[0]
    if n < 2:
        return None
    return max((b for t, b in lined if t == modal_line), key=lambda b: b[side])


def reset_line(row):
    """Back to the estimated line (no real book data)."""
    row.update(line=row["est"], src="est", over=None, under=None, books=[], ev_over=None, ev_under=None,
               fair_line=None, real_alts=[], best_over=None, best_under=None)


def apply_line(row, sg, td_markets, ev_pct):
    """Write one pulled-line entry onto a prop row -- the same rules for every sport, whether the entry came
    from Prop-Line or SportsGameOdds."""
    if not (sg and sg.get("books")):
        return
    est = row["est"]
    line = sg["line"] if sg["line"] is not None else est
    row.update(line=line, src="book", over=sg["over"], under=sg["under"], books=sg["books"])
    # EV only where the book's line is close enough to the fair line that comparing their odds head-to-head
    # is a fair read (a yes/no market has no numeric line, so it is always comparable). A flat 1.0 gap isn't
    # tight enough for low-count markets (TDs, sacks, FGs, steals): a 0.5-1.0 gap there is most of the
    # market's whole scale, so the gap must also be a modest fraction of the fair line's own size.
    fl = sg.get("fair_line")
    gap = abs(line - fl) if fl is not None else None
    comparable = row["market"] in td_markets or (gap is not None and gap <= 1.0 and gap <= max(abs(fl), 1.0) / 3)
    if comparable:
        row["ev_over"] = ev_pct(sg["over"], sg["fair_over"])
        row["ev_under"] = ev_pct(sg["under"], sg["fair_under"])
    row["fair_line"] = fl
    row["real_alts"] = [{"t": a["t"], "over_odds": a["over"]["odds"], "over_book": a["over"]["book"]}
                        for a in sg.get("alts") or [] if a.get("over")]
    row["best_over"] = best_book_price(row["books"], "over")
    row["best_under"] = best_book_price(row["books"], "under")


def reapply(board, sgo, norm, td_markets, ev_pct, line_hist=None, injuries=None):
    """Re-apply freshly pulled lines to an already-built board in place (well under a second), instead of
    rebuilding it from scratch (injury status too, when an injury map is given). Rows with a fixed line (src 'fixed', e.g. the Q1 5+ yards markets) are left
    alone."""
    for row in board["props"]:
        if row.get("src") not in ("est", "book"):
            continue
        reset_line(row)
        key = f"{norm(row['player'])}|{row['market']}|{row['game']}"
        apply_line(row, sgo["lines"].get(key), td_markets, ev_pct)
        if line_hist is not None:
            row["line_history"] = line_hist.get(key)
        if injuries is not None:
            inj = injuries.get(str(row.get("pid")))
            row["inj"], row["inj_note"] = (inj["status"], inj["note"]) if inj else (None, None)
    board["odds_pulled"] = sgo.get("pulled")
    board["proline_pulled"] = sgo.get("proline_pulled")


# ----------------------------------------------------------------------- injuries ---
# ESPN's injury report, relayed by Prop-Line (free, refreshed every ~10 min, carries ESPN's own athlete id --
# the same id the NBA and NHL boards use). Mapped onto the three tags the page already understands.
INJURY_TAG = {"out": "Out", "injured_list": "Out", "suspended": "Out",
              "doubtful": "Doubtful", "questionable": "Questionable", "day_to_day": "Questionable"}
INJURIES_MAX_AGE = 24 * 3600


def _injury_note(p):
    what = (p.get("injury_type") or "").strip()
    if p.get("detail"):
        what = f"{what} ({p['detail'].strip()})" if what else p["detail"].strip()
    bits = [what or "injury"]
    if p.get("expected_return"):
        bits.append(f"expected back {p['expected_return']}")
    return ", ".join(bits)


def pull_injuries(sport_key, key=None):
    """One request. -> {"pulled", "players": {espn_athlete_id: {"status", "note", "name", "team"}}}"""
    key = key or load_key()
    if not key:
        raise RuntimeError("No Prop-Line key found at data/proline_key.txt.")
    data, headers = _get(f"/sports/{sport_key}/injuries", key)
    players = {}
    for p in data.get("players", []):
        tag = INJURY_TAG.get(p.get("status_code"))
        aid = p.get("espn_athlete_id")
        if tag and aid:
            players[str(aid)] = {"status": tag, "note": _injury_note(p), "name": p.get("player_name"),
                                 "team": p.get("team_abbr")}
    return {"pulled": time.time(), "players": players, "requests": 1, "remaining": headers.get("x-daily-remaining")}


def load_injuries(path, now=None):
    """Current injury map from the saved report, or {} if it's missing or older than a day."""
    d = _read(path) or {}
    if not d.get("pulled") or (now or time.time()) - d["pulled"] > INJURIES_MAX_AGE:
        return {}
    return d.get("players") or {}
