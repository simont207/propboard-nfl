"""NBA data pipeline for PropBoards.

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
import re
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

# --------------------------------------------------- SportsGameOdds (real lines, once/day) ---
# Same account/API app.py's NFL integration already uses. Checked live against SGO's real NBA
# coverage before writing any of this: 10 of our 19 markets have real O/U (or yn for dd/td) lines --
# fg3a/fgm/fga/ftm/oreb/dreb/stl/to/stocks don't (no matching statID found), those stay EST-only.
BOOK_ORDER = ["fanduel", "draftkings", "betmgm", "caesars", "espnbet", "fanatics"]
SGO_API = "https://api.sportsgameodds.com/v2"
SGO_KEY_FILE = BASE / "data" / "sgo_key.txt"   # shared key file, same account as NFL's
SGO_FILE = DATA / "sgo_odds.json"
LINE_HISTORY_FILE = DATA / "line_history.json"
SGO_MARKETS = {
    ("points", "game"): "pts", ("rebounds", "game"): "reb", ("assists", "game"): "ast",
    ("threePointersMade", "game"): "fg3m", ("blocks", "game"): "blk",
    ("points+rebounds+assists", "game"): "pra", ("points+rebounds", "game"): "pr",
    ("points+assists", "game"): "pa", ("rebounds+assists", "game"): "ra",
    ("doubleDouble", "game"): "dd", ("tripleDouble", "game"): "td",
}


def norm_name(n):
    n = re.sub(r"[^a-z ]", "", (n or "").lower().replace(".", ""))
    return re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", n).strip()


def load_sgo_key():
    try:
        return SGO_KEY_FILE.read_text().strip() or None
    except Exception:
        return None


def load_sgo():
    try:
        return json.loads(SGO_FILE.read_text())
    except Exception:
        return {"pulled": None, "remaining": None, "lines": {}}


def _sgo_american(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def record_line_history(lines):
    try:
        hist = json.loads(LINE_HISTORY_FILE.read_text())
    except Exception:
        hist = {}
    now = time.time()
    for key, entry in lines.items():
        if entry.get("line") is None:
            continue
        points = hist.setdefault(key, [])
        if points and points[-1]["line"] == entry["line"]:
            continue
        points.append({"ts": now, "line": entry["line"]})
        del points[:-20]
    LINE_HISTORY_FILE.write_text(json.dumps(hist))


def load_line_history():
    try:
        return json.loads(LINE_HISTORY_FILE.read_text())
    except Exception:
        return {}


def american_to_prob(odds):
    if odds is None:
        return None
    return 100 / (odds + 100) if odds > 0 else -odds / (-odds + 100)


def ev_pct(book_odds, fair_odds):
    if book_odds is None or fair_odds is None:
        return None
    p_fair = american_to_prob(fair_odds)
    d_book = 1 + (book_odds / 100 if book_odds > 0 else 100 / abs(book_odds))
    return round((p_fair * d_book - 1) * 100, 1)


def pull_sgo(key, games=None):
    """Mirrors app.py's NFL pull_sgo (same account/API, see there for the fuller rationale on each
    guard). dd/td are binary yn markets sharing statIDs the same way NFL's any_td does -- same gate."""
    import datetime
    params_extra = {}
    if games:
        last = max(g["date"] for g in games)[:10]
        end = (datetime.date.fromisoformat(last) + datetime.timedelta(days=1)).isoformat()
        params_extra = {"startsAfter": min(g["date"] for g in games)[:10], "startsBefore": end}
    events, cursor = [], None
    for _ in range(10):
        params = {"apiKey": key, "leagueID": "NBA", "oddsAvailable": "true", "includeAltLines": "true",
                  **params_extra}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(f"{SGO_API}/events", params=params, timeout=45)
        if r.status_code != 200:
            try:
                msg = r.json().get("error", f"HTTP {r.status_code}")
            except Exception:
                msg = f"HTTP {r.status_code}"
            raise RuntimeError(msg)
        d = r.json()
        events.extend(d.get("data", []))
        cursor = d.get("nextCursor")
        if not cursor:
            break

    game_by_teams = {}
    if games:
        for g in games:
            game_by_teams[(g["home"], g["away"])] = g["id"]

    lines = {}
    for ev in events:
        players = ev.get("players", {})
        teams = ev.get("teams", {})
        home_abbr = (teams.get("home", {}).get("names") or {}).get("short")
        away_abbr = (teams.get("away", {}).get("names") or {}).get("short")
        our_game_id = game_by_teams.get((home_abbr, away_abbr)) or ev["eventID"]
        for o in ev.get("odds", {}).values():
            mkey = SGO_MARKETS.get((o.get("statID"), o.get("periodID")))
            pid = o.get("playerID")
            if not mkey or not pid or pid not in players:
                continue
            bt = o.get("betTypeID")
            if (mkey in TD_MARKETS) != (bt == "yn"):
                continue
            side = o.get("sideID")
            slot = "over" if side in ("over", "yes") else "under" if side in ("under", "no") else None
            if not slot:
                continue
            name = norm_name(players[pid].get("name", ""))
            entry = lines.setdefault(f"{name}|{mkey}|{our_game_id}", {
                "line": None, "fair_line": None, "over": None, "under": None,
                "fair_over": None, "fair_under": None, "books": {}, "alts": [],
            })
            if o.get("bookOverUnder") is not None:
                entry["line"] = float(o["bookOverUnder"])
            if o.get("fairOverUnder") is not None:
                entry["fair_line"] = float(o["fairOverUnder"])
            am, fair_am = _sgo_american(o.get("bookOdds")), _sgo_american(o.get("fairOdds"))
            if am is not None:
                entry[slot] = am
            if fair_am is not None:
                entry[f"fair_{slot}"] = fair_am
            for book, b in (o.get("byBookmaker") or {}).items():
                if not b.get("available"):
                    continue
                bam = _sgo_american(b.get("odds"))
                if bam is None:
                    continue
                be = entry["books"].setdefault(book, {"book": book, "line": b.get("overUnder"), "over": None, "under": None})
                be[slot] = bam
                if b.get("overUnder") is not None:
                    be["line"] = b.get("overUnder")

    for entry in lines.values():
        if not entry["books"] and (entry["over"] is not None or entry["under"] is not None):
            entry["books"]["sgo"] = {"book": "sgo consensus", "line": entry["line"],
                                      "over": entry["over"], "under": entry["under"]}
        entry["books"] = sorted(entry["books"].values(),
                                 key=lambda b: BOOK_ORDER.index(b["book"]) if b["book"] in BOOK_ORDER else 99)

    record_line_history(lines)
    out = {"pulled": time.time(), "events": len(events), "lines": lines}
    SGO_FILE.write_text(json.dumps(out))
    return out


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
            # NBA's scoreboard competitor objects have no "logos" field at all (unlike CFB's, where the
            # same lookup works) -- confirmed by checking a real response before assuming a fix; ESPN's
            # standard per-team logo CDN URL works directly off the abbreviation instead.
            def logo_url(abbr):
                return f"https://a.espncdn.com/i/teamlogos/nba/500/{abbr.lower()}.png" if abbr else None
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


def load_rest_days(df, games):
    """Days between an upcoming game and that team's own most recent game in our loaded box-score
    history -- unlike the NFL site (which reads a dedicated schedule CSV), the per-team game dates
    needed here are already sitting in `df` from building the rest of the board, so no new data
    source is needed. NBA plays far more often than NFL (back-to-backs are routine), so the labels
    are tuned to that cadence instead of reusing the NFL site's week-based language."""
    import datetime
    by_team = {team: sorted(grp["sw"].unique()) for team, grp in df.groupby("team")}
    out = {}
    for gm in games:
        kickoff_utc = datetime.datetime.fromisoformat(gm["date"].replace("Z", "+00:00"))
        kickoff = (kickoff_utc - datetime.timedelta(hours=5)).date()
        for side in ("home", "away"):
            team = gm[side]
            prior = [datetime.date.fromisoformat(d) for d in by_team.get(team, []) if datetime.date.fromisoformat(d) < kickoff]
            if not prior:
                continue
            days = (kickoff - max(prior)).days
            label = ("Back-to-back" if days == 0 else "1 day of rest" if days == 1 else
                     "Extended rest" if days >= 4 else f"{days} days of rest")
            out[(team, gm["id"])] = {"days": days, "label": label}
    return out


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
    rest = load_rest_days(df, games)
    sgo = load_sgo()
    line_hist = load_line_history()

    latest = df.sort_values("sw").groupby("athlete_id").tail(1).set_index("athlete_id")
    rows = []
    for g in games:
        for team, opp in ((g["home"], g["away"]), (g["away"], g["home"])):
            cand = latest[latest.team == team]
            for aid, p in cand.iterrows():
                hist = df[(df.athlete_id == aid) & (df.team == team)]
                pos = p.pos
                recent_min = hist.minutes.tail(8).mean()
                # No volume floor -- every player gets every market regardless of recent playing time,
                # matching the NFL site's identical call (Simon: "remove it entirely, show every
                # player"). `recent_min` is still computed and stored as `vol` below for display/
                # sorting; it just no longer gates whether a prop appears at all.
                for mkey, (label, vmin) in MARKETS.items():
                    hl = hist.tail(20)
                    if hl.empty:
                        continue
                    est = 0.5 if mkey in TD_MARKETS else float(int(hl[mkey].tail(8).mean())) + 0.5
                    rk = ranks.get((opp, pos, mkey))
                    row = {
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
                        "ev_over": None, "ev_under": None, "fair_line": None, "real_alts": [],
                        "line_history": line_hist.get(f"{norm_name(p['name'])}|{mkey}|{g['id']}"),
                        "opp_rest": rest.get((opp, g["id"])),
                    }
                    sg = sgo["lines"].get(f"{norm_name(p['name'])}|{mkey}|{g['id']}")
                    if sg and sg["books"]:
                        line = sg["line"] if sg["line"] is not None else est
                        row.update(line=line, src="book", over=sg["over"], under=sg["under"], books=sg["books"])
                        comparable = mkey in TD_MARKETS or (sg["fair_line"] is not None and abs(line - sg["fair_line"]) <= 1.0)
                        if comparable:
                            row["ev_over"] = ev_pct(sg["over"], sg["fair_over"])
                            row["ev_under"] = ev_pct(sg["under"], sg["fair_under"])
                        row["fair_line"] = sg["fair_line"]
                    rows.append(row)

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

    # Individual player-game results vs each defense -- unlike NFL's version, every parsed boxscore
    # row here already has a real 0+ value for every stat (no NaN to filter out), so this is just the
    # last 8 games, newest first, with no qualifying-game distinction needed.
    recent_players = {}
    for mkey in MARKETS:
        for pos in POSITIONS:
            sub = df[df.pos == pos].sort_values("sw")
            for def_team, grp in sub.groupby("opp"):
                rows_rp = grp.tail(8).iloc[::-1]
                recent_players[f"{def_team}|{pos}|{mkey}"] = [
                    [x.name, x.team, round(float(getattr(x, mkey)), 1), x.sw] for x in rows_rp.itertuples()]

    board = {"season": LAST_SEASON, "games": games, "props": rows, "dvp": dvp, "recent": recent,
             "recent_players": recent_players,
             "inj_week": 0, "built": time.time(), "has_key": bool(load_sgo_key()),
             "odds_pulled": sgo.get("pulled"), "odds_remaining": None}
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
