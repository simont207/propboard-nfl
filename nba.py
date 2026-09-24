"""NBA data pipeline for PropBoard.

Same approach as cfb.py: ESPN's public box-score endpoints, one request per game, completed games
cached to disk forever. The 2026-27 season hasn't started yet (checked live: one preseason game on
the schedule, regular season not until mid-October), so history is bootstrapped entirely from last
season (2025-26) until this season's own games start providing real data -- same principle as the
NFL side blending SEASON-1 + SEASON, just via a per-game fetch since there's no bulk file for NBA
the way nflverse provides for the NFL.

Unlike CFB, ESPN's NBA box score gives real position data per player directly (no stat-dominance
guessing needed) and a single flat per-player stat line (no passing/rushing/receiving split) --
confirmed against a real live completed game before writing any of this, not assumed:
keys = [minutes, points, fieldGoalsMade-fieldGoalsAttempted, threePointFieldGoalsMade-...,
        freeThrowsMade-..., rebounds, assists, turnovers, steals, blocks, offensiveRebounds,
        defensiveRebounds, fouls, plusMinus]
"""
import json
import time
from pathlib import Path

import pandas as pd
import requests

BASE = Path(__file__).parent
DATA = BASE / "data" / "nba"
GAMES_DIR = DATA / "games"
GAMES_DIR.mkdir(parents=True, exist_ok=True)

ESPN_HOSTS = ["site.api.espn.com", "site.web.api.espn.com"]   # 2nd host: site.api.* 403s from GH Actions
ESPN_PATH = "apis/site/v2/sports/basketball/nba"
HTTP = {"User-Agent": "Mozilla/5.0 PropBoard"}
LAST_SEASON = 2026     # ESPN's "season.year" label for the 2025-26 season (the most recently completed one)

MARKETS = {                  # label, min recent minutes/game to qualify for a line
    "pts": ("Points", 12),
    "reb": ("Rebounds", 12),
    "ast": ("Assists", 12),
    "fg3m": ("3-Pointers Made", 12),
    "fg3a": ("3-Point Attempts", 12),
    "fgm": ("Field Goals Made", 12),
    "fga": ("Field Goal Attempts", 12),
    "ftm": ("Free Throws Made", 12),
    "oreb": ("Offensive Rebounds", 12),
    "dreb": ("Defensive Rebounds", 12),
    "stl": ("Steals", 12),
    "blk": ("Blocks", 12),
    "to": ("Turnovers", 12),
    "pra": ("Pts+Reb+Ast", 15),
    "pr": ("Pts+Reb", 15),
    "pa": ("Pts+Ast", 15),
    "ra": ("Reb+Ast", 12),
    "stocks": ("Steals+Blocks", 12),
    "dd": ("Double-Double", 15),
    "td": ("Triple-Double", 20),
}
TD_MARKETS = {"dd", "td"}     # binary yes/no markets -- fixed 0.5 line, same convention as app.py/cfb.py's any_td
POSITIONS = ("G", "F", "C")


def _get(path, **params):
    """site.api.espn.com 403s from GitHub Actions runners (same block NFL/CFB hit); site.web.api.espn.com
    serves the identical response and isn't blocked there, so try that host second, not first --
    locally both work, but site.api is the one ESPN's own apps use, so it's the safer default."""
    last = None
    for host in ESPN_HOSTS:
        try:
            r = requests.get(f"https://{host}/{ESPN_PATH}/{path}", params=params, headers=HTTP, timeout=25)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
    raise last


def scoreboard(date=None):
    params = {"dates": date} if date else {}
    return _get("scoreboard", **params)


def season_game_days(season_year):
    """Every date with >=1 game in `season_year` (preseason through Finals), from the scoreboard's
    own calendar -- far cheaper than checking all ~250 calendar days one at a time for whether it
    has a game. Cached to disk once fetched since a past season's calendar never changes.

    The calendar returned depends on which date you ask the scoreboard for (it reflects whatever
    season that date belongs to) -- calling scoreboard() with no date returns the CURRENT season's
    calendar, which is wrong when season_year is a prior season. Anchor to Jan 15 of season_year
    (ESPN labels a season by its ending year, e.g. the 2025-26 season = year 2026, and mid-January
    always falls inside the regular season, never in the neighboring season's calendar by mistake)."""
    cache = DATA / f"calendar_{season_year}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    sb = scoreboard(date=f"{season_year}0115")
    league = (sb.get("leagues") or [{}])[0]
    cal = [d[:10].replace("-", "") for d in league.get("calendar", [])]
    cache.write_text(json.dumps(cal))
    return cal


def game_summary(game_id):
    cache = GAMES_DIR / f"{game_id}.json"
    if cache.exists():
        try:
            data = json.loads(cache.read_text())
            if data.get("_final"):
                return data
        except Exception:
            pass
    try:
        data = _get("summary", event=game_id)
    except Exception as e:
        print(f"  boxscore fetch failed for {game_id}: {e}")
        return None
    comp = (data.get("header", {}).get("competitions") or [{}])[0]
    data["_final"] = comp.get("status", {}).get("type", {}).get("state") == "post"
    cache.write_text(json.dumps(data))
    return data


def parse_boxscore(summary):
    """One dict per (game, athlete). NBA's box score is a single flat stat line per player (unlike
    CFB's split passing/rushing/receiving categories), keyed by name via the response's own `keys`
    array rather than positional index -- more robust than guessing column order."""
    bx = summary.get("boxscore") or {}
    header = summary.get("header", {})
    comp = (header.get("competitions") or [{}])[0]
    game_id = str(header.get("id"))
    date = comp.get("date")
    abbrs = {c["team"].get("abbreviation"): c["homeAway"] for c in comp.get("competitors", [])}
    rows = []
    for team_block in bx.get("players", []):
        team = team_block["team"].get("abbreviation")
        opp = next((a for a in abbrs if a != team), None)
        for cat in team_block.get("statistics", []):
            keys = cat.get("keys") or []
            for ath in cat.get("athletes", []):
                a = ath.get("athlete") or {}
                aid = a.get("id")
                if not aid or ath.get("didNotPlay"):
                    continue
                stats = dict(zip(keys, ath.get("stats") or []))
                try:
                    minutes = float(stats.get("minutes") or 0)
                except ValueError:
                    minutes = 0.0
                if minutes <= 0:
                    continue

                def num(key):
                    try:
                        return float(stats.get(key) or 0)
                    except ValueError:
                        return 0.0

                def madeatt(key):     # "X-Y" made-attempted strings, e.g. threePointFieldGoalsMade-...Attempted
                    v = stats.get(key) or "0-0"
                    try:
                        m, att = v.split("-")
                        return float(m), float(att)
                    except (ValueError, IndexError):
                        return 0.0, 0.0

                pts, reb, ast = num("points"), num("rebounds"), num("assists")
                stl, blk, to = num("steals"), num("blocks"), num("turnovers")
                fgm, fga = madeatt("fieldGoalsMade-fieldGoalsAttempted")
                fg3m, fg3a = madeatt("threePointFieldGoalsMade-threePointFieldGoalsAttempted")
                ftm, fta = madeatt("freeThrowsMade-freeThrowsAttempted")
                oreb, dreb = num("offensiveRebounds"), num("defensiveRebounds")
                double_digit = sum(1 for v in (pts, reb, ast, stl, blk) if v >= 10)
                pos = ((a.get("position") or {}).get("abbreviation") or "F")
                pos = "G" if pos in ("G", "PG", "SG") else "C" if pos == "C" else "F"
                rows.append({
                    "game_id": game_id, "date": date, "team": team, "opp": opp,
                    "athlete_id": aid, "name": a.get("displayName"),
                    "headshot": (a.get("headshot") or {}).get("href"), "pos": pos, "minutes": minutes,
                    "pts": pts, "reb": reb, "ast": ast, "fg3m": fg3m, "fg3a": fg3a,
                    "fgm": fgm, "fga": fga, "ftm": ftm, "oreb": oreb, "dreb": dreb,
                    "stl": stl, "blk": blk, "to": to,
                    "pra": pts + reb + ast, "pr": pts + reb, "pa": pts + ast, "ra": reb + ast,
                    "stocks": stl + blk, "dd": 1.0 if double_digit >= 2 else 0.0,
                    "td": 1.0 if double_digit >= 3 else 0.0,
                })
    return rows


def load_history(season_year, max_days=None):
    """Every regular-season box score from `season_year` (preseason/playoffs excluded -- different
    rotations and opponent dynamics than the regular-season sample we want). Cached forever per
    completed game, so a rebuild only fetches games that are genuinely new since last time."""
    days = season_game_days(season_year)
    if max_days:
        days = days[:max_days]
    rows = []
    for i, day in enumerate(days):
        try:
            sb = scoreboard(date=day)
        except Exception as e:
            print(f"  {day}: scoreboard failed: {e}")
            continue
        ids = [e["id"] for e in sb.get("events", [])
               if e["competitions"][0]["status"]["type"]["state"] == "post"
               and e.get("season", {}).get("type") == 2]      # 2 = regular season
        if not ids:
            continue
        print(f"  {day} ({i+1}/{len(days)}): {len(ids)} regular-season games")
        for gid in ids:
            s = game_summary(gid)
            if s:
                rows.extend(parse_boxscore(s))
            time.sleep(0.05)
    return pd.DataFrame(rows)


# ------------------------------------------------------------------------ board ---
def ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def upcoming_games(days_ahead=14):
    """Next `days_ahead` days of scheduled games from today. The 2026-27 season hasn't started as of
    this build (checked live: a single preseason game on the books, regular season mid-October) --
    this will legitimately show very little until then, same as it would for anyone using the site."""
    import datetime
    games, seen = [], set()
    today = datetime.date.today()
    for i in range(days_ahead):
        day = (today + datetime.timedelta(days=i)).strftime("%Y%m%d")
        try:
            sb = scoreboard(date=day)
        except Exception as e:
            print(f"  upcoming {day} failed: {e}")
            continue
        for e in sb.get("events", []):
            comp = e["competitions"][0]
            if comp["status"]["type"]["state"] == "post" or e["id"] in seen:
                continue
            seen.add(e["id"])
            home = next(c for c in comp["competitors"] if c["homeAway"] == "home")
            away = next(c for c in comp["competitors"] if c["homeAway"] == "away")
            odds = (comp.get("odds") or [{}])[0]
            games.append({
                "id": e["id"], "state": comp["status"]["type"]["state"], "date": e["date"],
                "home": home["team"].get("abbreviation"), "away": away["team"].get("abbreviation"),
                "home_name": home["team"].get("displayName"), "away_name": away["team"].get("displayName"),
                "home_logo": (home["team"].get("logos") or [{}])[0].get("href"),
                "away_logo": (away["team"].get("logos") or [{}])[0].get("href"),
                "spread": odds.get("details"), "total": odds.get("overUnder"),
                "home_spread": odds.get("spread"),
            })
    games.sort(key=lambda g: g["date"])
    return games


def defense_ranks(df):
    """Rank all 30 teams by average allowed per game to each position group (G/F/C)."""
    ranks, dvp = {}, {}
    n = 30
    for mkey in MARKETS:
        for pos in POSITIONS:
            sub = df[df.pos == pos]
            per_game = (sub.groupby(["opp", "game_id"])[mkey].sum().reset_index()
                        .groupby("opp")[mkey].mean())
            if per_game.empty:
                continue
            rk = per_game.rank(ascending=False, method="min").astype(int)
            dvp[f"{pos}|{mkey}"] = {"league": round(float(per_game.mean()), 2), "n": n,
                                     "teams": {t: [int(rk[t]), round(float(per_game[t]), 2)] for t in per_game.index}}
            for team, r in rk.items():
                ranks[(team, pos, mkey)] = int(r)
    return ranks, dvp


def build_board():
    print("NBA: fetching last season's history (2025-26)...")
    df = load_history(LAST_SEASON)
    if df.empty:
        print("NBA: no history rows, skipping.")
        return {"season": LAST_SEASON, "games": [], "props": [], "dvp": {}, "recent": {}, "built": time.time()}
    df["sw"] = df.date.str[:10]      # sortable date key, since NBA has no "week"
    df = df.sort_values(["sw"]).reset_index(drop=True)
    ranks, dvp = defense_ranks(df)

    print("NBA: fetching upcoming schedule...")
    games = upcoming_games()

    latest = df.sort_values("sw").groupby("athlete_id").tail(1).set_index("athlete_id")
    rows = []
    for g in games:
        for team, opp in ((g["home"], g["away"]), (g["away"], g["home"])):
            cand = latest[latest.team == team]
            for aid, p in cand.iterrows():
                hist = df[(df.athlete_id == aid) & (df.team == team)]
                pos = p.pos
                recent_min = hist.minutes.tail(8).mean()
                for mkey, (label, vmin) in MARKETS.items():
                    if not recent_min >= vmin:
                        continue
                    hl = hist.tail(20)
                    if hl.empty:
                        continue
                    est = 0.5 if mkey in TD_MARKETS else float(int(hl[mkey].tail(8).mean())) + 0.5
                    rk = ranks.get((opp, pos, mkey))
                    rows.append({
                        "id": f"{aid}|{mkey}|{g['id']}", "pid": aid, "player": p["name"],
                        "pos": pos, "team": team, "opp": opp, "home": 1 if team == g["home"] else 0,
                        "game": g["id"], "img": p.headshot if isinstance(p.headshot, str) else None,
                        "market": mkey, "label": label,
                        "est": est, "line": est, "src": "est", "over": None, "under": None,
                        "books": [], "inj": None, "opp_rank": rk,
                        # NBA has no "week" -- LAST_SEASON used uniformly for every game since an NBA
                        # season spans two calendar years (games in both 2025 and 2026 are "season 2026"
                        # by ESPN's own label); using the game date's raw year would wrongly split one
                        # season's games across two season-buckets in the frontend's window-tab filters
                        "log": [[LAST_SEASON, 1, r.opp, float(getattr(r, mkey)), r.sw,
                                 1 if r.team == g["home"] else (0 if r.team == g["away"] else None),
                                 None, None] for r in hl.itertuples()],
                        "vol": round(float(recent_min), 1),
                    })

    recent = {}
    for mkey in MARKETS:
        for pos in POSITIONS:
            sub = df[df.pos == pos]
            for def_team, grp in sub.groupby("opp"):
                byday = grp.groupby(["game_id", "sw"]).agg(total=(mkey, "sum")).reset_index().sort_values("sw")
                entries = []
                for _, row in byday.tail(5).iterrows():
                    game_rows = grp[grp.game_id == row.game_id]
                    off_team = game_rows.team.iloc[0] if len(game_rows) else ""
                    top = game_rows.loc[game_rows[mkey].idxmax()] if len(game_rows) else None
                    entries.append([LAST_SEASON, 1, off_team, round(float(row.total), 1),
                                     top["name"] if top is not None else "",
                                     round(float(top[mkey]), 1) if top is not None else 0])
                recent[f"{def_team}|{pos}|{mkey}"] = entries

    board = {"season": LAST_SEASON, "games": games, "props": rows, "dvp": dvp, "recent": recent,
             "inj_week": 0, "built": time.time(), "has_key": False, "odds_pulled": None, "odds_remaining": None}
    return _clean_nans(board)


def _clean_nans(obj):
    """A stray pandas NaN anywhere in here (e.g. a missing headshot/name) breaks strict JSON encoding
    downstream -- final safety net rather than chasing each source field by hand."""
    import math
    if isinstance(obj, dict):
        return {k: _clean_nans(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_nans(v) for v in obj]
    if isinstance(obj, float) and math.isnan(obj):
        return None
    return obj
