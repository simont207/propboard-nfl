"""NHL data pipeline for PropBoard.

Same approach as nba.py/cfb.py: ESPN's public box-score endpoints, one request per game, completed
games cached to disk forever. Unlike NBA, the 2026-27 season has already started preseason play
(checked live: games in progress as of this build) -- but regular-season games don't start until
October, so history is still bootstrapped from last season (2025-26) same as the other sports, and
this season's own regular-season games will blend in automatically once they exist.

ESPN's NHL box score is even cleaner than NBA's for position data: players are split into separate
`forwards` / `defenses` / `goalies` statistic categories per team (confirmed against a real live
completed game before writing any of this -- `skaters` is a 3rd, always-empty category, not a
duplicate list to worry about), so position comes for free from which category a player is in,
no inference or explicit-field lookup needed. Skater and goalie stat keys are completely different
sets (goalies track saves/goalsAgainst, not goals/assists), so they're parsed separately.
"""
import json
import time
from pathlib import Path

import pandas as pd
import requests

BASE = Path(__file__).parent
DATA = BASE / "data" / "nhl"
GAMES_DIR = DATA / "games"
GAMES_DIR.mkdir(parents=True, exist_ok=True)

ESPN_HOSTS = ["site.api.espn.com", "site.web.api.espn.com"]   # 2nd host: site.api.* 403s from GH Actions
ESPN_PATH = "apis/site/v2/sports/hockey/nhl"
HTTP = {"User-Agent": "Mozilla/5.0 PropBoard"}
LAST_SEASON = 2026     # ESPN's "season.year" label for the 2025-26 season (the most recently completed one)

SKATER_MARKETS = {           # label, min recent minutes/game to qualify for a line
    "goals": ("Goals", 12),
    "assists": ("Assists", 12),
    "points": ("Points", 12),
    "sog": ("Shots on Goal", 12),
    "blocks": ("Blocked Shots", 12),
    "hits": ("Hits", 12),
    "pim": ("Penalty Minutes", 12),
}
GOALIE_MARKETS = {
    "saves": ("Saves", 20),
    "goals_against": ("Goals Against", 20),
}
MARKETS = {**SKATER_MARKETS, **GOALIE_MARKETS}
POSITIONS = ("F", "D", "G")


def _get(path, **params):
    """site.api.espn.com 403s from GitHub Actions runners (same block NFL/CFB/NBA hit); site.web.api.
    espn.com serves the identical response and isn't blocked there, so try that host second, not
    first -- locally both work, but site.api is the one ESPN's own apps use, so it's the safer default."""
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
    """Every date with >=1 game in `season_year`, from the scoreboard's own calendar. Cached to disk
    once fetched since a past season's calendar never changes. Anchored to Jan 15 of season_year (ESPN
    labels a season by its ending year, and mid-January always falls inside the regular season) --
    calling scoreboard() with no date returns the CURRENT season's calendar, wrong for a prior season."""
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


def _toi_minutes(v):    # "MM:SS" -> minutes as a float, e.g. "19:48" -> 19.8
    try:
        m, s = v.split(":")
        return float(m) + float(s) / 60
    except (ValueError, AttributeError):
        return 0.0


def parse_boxscore(summary):
    """One dict per (game, athlete). Skaters (forwards+defenses) and goalies use different stat key
    sets, so they're built as two separate row shapes rather than forced into one."""
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
            name = cat.get("name")
            if name not in ("forwards", "defenses", "goalies"):
                continue
            pos = "F" if name == "forwards" else "D" if name == "defenses" else "G"
            keys = cat.get("keys") or []
            for ath in cat.get("athletes", []):
                a = ath.get("athlete") or {}
                aid = a.get("id")
                if not aid:
                    continue
                stats = dict(zip(keys, ath.get("stats") or []))
                minutes = _toi_minutes(stats.get("timeOnIce"))
                if minutes <= 0:
                    continue

                def num(key):
                    try:
                        return float(stats.get(key) or 0)
                    except ValueError:
                        return 0.0

                base = {
                    "game_id": game_id, "date": date, "team": team, "opp": opp,
                    "athlete_id": aid, "name": a.get("displayName"),
                    "headshot": (a.get("headshot") or {}).get("href"), "pos": pos, "minutes": minutes,
                }
                if pos == "G":
                    base.update({
                        "saves": num("saves"), "goals_against": num("goalsAgainst"),
                        "goals": 0.0, "assists": 0.0, "points": 0.0, "sog": 0.0,
                        "blocks": 0.0, "hits": 0.0, "pim": num("penaltyMinutes"),
                    })
                else:
                    g, a_ = num("goals"), num("assists")
                    base.update({
                        "goals": g, "assists": a_, "points": g + a_, "sog": num("shotsTotal"),
                        "blocks": num("blockedShots"), "hits": num("hits"), "pim": num("penaltyMinutes"),
                        "saves": 0.0, "goals_against": 0.0,
                    })
                rows.append(base)
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
def upcoming_games(days_ahead=14):
    """Next `days_ahead` days of scheduled games from today."""
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

            def logo_url(abbr):     # ESPN's NHL scoreboard competitor objects have no "logos" field
                return f"https://a.espncdn.com/i/teamlogos/nhl/500/{abbr.lower()}.png" if abbr else None
            games.append({
                "id": e["id"], "state": comp["status"]["type"]["state"], "date": e["date"],
                "home": home["team"].get("abbreviation"), "away": away["team"].get("abbreviation"),
                "home_name": home["team"].get("displayName"), "away_name": away["team"].get("displayName"),
                "home_logo": logo_url(home["team"].get("abbreviation")),
                "away_logo": logo_url(away["team"].get("abbreviation")),
                "spread": odds.get("details"), "total": odds.get("overUnder"),
                "home_spread": odds.get("spread"),
            })
    games.sort(key=lambda g: g["date"])
    return games


def defense_ranks(df):
    """Rank all 32 teams by average allowed per game to each position group (F/D/G)."""
    ranks, dvp = {}, {}
    n = 32
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
    print("NHL: fetching last season's history (2025-26)...")
    df = load_history(LAST_SEASON)
    if df.empty:
        print("NHL: no history rows, skipping.")
        return {"season": LAST_SEASON, "games": [], "props": [], "dvp": {}, "recent": {}, "built": time.time()}
    df["sw"] = df.date.str[:10]      # sortable date key, since NHL has no "week"
    df = df.sort_values(["sw"]).reset_index(drop=True)
    ranks, dvp = defense_ranks(df)

    print("NHL: fetching upcoming schedule...")
    games = upcoming_games()

    latest = df.sort_values("sw").groupby("athlete_id").tail(1).set_index("athlete_id")
    rows = []
    for g in games:
        for team, opp in ((g["home"], g["away"]), (g["away"], g["home"])):
            cand = latest[latest.team == team]
            for aid, p in cand.iterrows():
                hist = df[(df.athlete_id == aid) & (df.team == team)]
                pos = p.pos
                relevant = GOALIE_MARKETS if pos == "G" else SKATER_MARKETS
                recent_min = hist.minutes.tail(8).mean()
                for mkey, (label, vmin) in relevant.items():
                    if not recent_min >= vmin:
                        continue
                    hl = hist.tail(20)
                    if hl.empty:
                        continue
                    est = float(int(hl[mkey].tail(8).mean())) + 0.5
                    rk = ranks.get((opp, pos, mkey))
                    rows.append({
                        "id": f"{aid}|{mkey}|{g['id']}", "pid": aid, "player": p["name"],
                        "pos": pos, "team": team, "opp": opp, "home": 1 if team == g["home"] else 0,
                        "game": g["id"], "img": p.headshot if isinstance(p.headshot, str) else None,
                        "market": mkey, "label": label,
                        "est": est, "line": est, "src": "est", "over": None, "under": None,
                        "books": [], "inj": None, "opp_rank": rk,
                        # LAST_SEASON used uniformly, not parsed from each game's own date -- an NHL
                        # season spans two calendar years, so using the raw date year would wrongly
                        # split one season's games across two season-buckets in the frontend's window
                        # tabs (same fix already needed for nba.py, applied here from the start)
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
