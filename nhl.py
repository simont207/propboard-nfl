"""NHL data pipeline for PropBoards.

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
import re
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
    # Penalty Minutes, Minutes on Ice, and Powerplay Minutes on Ice were tried and dropped (2026-09-30,
    # Simon's call) -- none of them are real sportsbook-bettable prop types, so they had no business
    # being standalone board rows regardless of how interesting the underlying data is. `minutes`/
    # `pp_toi` are still computed and used internally (the volume-floor gate above, and the Supporting
    # Stats card's "Shots on Goal"/"Powerplay Goals" tabs still work off real per-game data), just no
    # longer exposed as their own market.
    # No volume floor -- 0 is a real, common answer (most skaters see little or no power play time),
    # not a data gap, same reasoning as NFL's volume-floor removal.
    "pp_goals": ("Powerplay Goals", 0),
}
GOALIE_MARKETS = {
    "saves": ("Saves", 20),
    "goals_against": ("Goals Against", 20),
}
MARKETS = {**SKATER_MARKETS, **GOALIE_MARKETS}
POSITIONS = ("F", "D", "G")

# --------------------------------------------------- SportsGameOdds (real lines, once/day) ---
# Same account/API app.py's NFL integration already uses. Checked live against SGO's real NHL
# coverage before writing any of this: 'goals' and 'penaltyMinutes' have no plain O/U line in their
# data (no matching statID found across a 30-event sample) -- those 2 of our 9 markets stay EST-only,
# the other 7 (assists/blocks/hits/points/sog/saves/goals_against) get real book lines.
BOOK_ORDER = ["fanduel", "draftkings", "betmgm", "caesars", "espnbet", "fanatics"]
SGO_API = "https://api.sportsgameodds.com/v2"
SGO_KEY_FILE = BASE / "data" / "sgo_key.txt"   # shared key file, same account as NFL's
SGO_FILE = DATA / "sgo_odds.json"
LINE_HISTORY_FILE = DATA / "line_history.json"
SGO_MARKETS = {
    ("assists", "game"): "assists", ("blocks", "game"): "blocks", ("hits", "game"): "hits",
    ("points", "game"): "points", ("shots_onGoal", "game"): "sog",
    ("goalie_saves", "game"): "saves", ("goalie_goalsAgainst", "game"): "goals_against",
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
    guard). No TD_MARKETS-style binary market exists in our own NHL MARKETS dict, so every real
    market here is a plain 'ou' line -- no yn/ou conflation to gate against."""
    import datetime
    params_extra = {}
    if games:
        last = max(g["date"] for g in games)[:10]
        end = (datetime.date.fromisoformat(last) + datetime.timedelta(days=1)).isoformat()
        params_extra = {"startsAfter": min(g["date"] for g in games)[:10], "startsBefore": end}
    events, cursor = [], None
    for _ in range(10):
        params = {"apiKey": key, "leagueID": "NHL", "oddsAvailable": "true", "includeAltLines": "true",
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
            if not mkey or not pid or pid not in players or o.get("betTypeID") != "ou":
                continue
            side = o.get("sideID")
            slot = "over" if side == "over" else "under" if side == "under" else None
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

    # Power-play goals per athlete this game. Not a boxscore stat -- ESPN doesn't break goals down by
    # strength state in the per-player totals, but each scoring PLAY carries a real strength.text tag
    # ("Even Strength"/"Power Play"/"Shorthanded"/"Empty Net", confirmed against live games before
    # writing this), so it's counted here from the play-by-play instead.
    pp_goals = {}
    for play in summary.get("plays") or []:
        if play.get("type", {}).get("text") != "Goal":
            continue
        if (play.get("strength") or {}).get("text") != "Power Play":
            continue
        for part in play.get("participants") or []:
            if part.get("type") == "scorer":
                aid = (part.get("athlete") or {}).get("id")
                if aid:
                    pp_goals[aid] = pp_goals.get(aid, 0) + 1

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
                        "pp_toi": 0.0, "pp_goals": 0.0,
                    })
                else:
                    g, a_ = num("goals"), num("assists")
                    base.update({
                        "goals": g, "assists": a_, "points": g + a_, "sog": num("shotsTotal"),
                        "blocks": num("blockedShots"), "hits": num("hits"), "pim": num("penaltyMinutes"),
                        "saves": 0.0, "goals_against": 0.0,
                        "pp_toi": _toi_minutes(stats.get("powerPlayTimeOnIce")),
                        "pp_goals": float(pp_goals.get(aid, 0)),
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


def load_rest_days(df, games):
    """Days between an upcoming game and that team's own most recent game in our loaded box-score
    history -- unlike the NFL site (which reads a dedicated schedule CSV), the per-team game dates
    needed here are already sitting in `df` from building the rest of the board, so no new data
    source is needed. NHL plays far more often than NFL (back-to-backs are routine), so the labels
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


def load_recent_participation(df, latest, n=15):
    """Per-player recent participation: a dot per the team's last N games (played/missed), not a
    full-season view like the NFL site's week-based timeline. NHL plays an 82-game season with
    frequent back-to-backs and real load-management rest (not just injury), so a full season would be
    both visually unwieldy and a much noisier "missed" signal than NFL's version -- a recent window is
    the actually useful read here. No injury-report history sub-list -- no injury data source is
    integrated for this sport, so `history` stays empty rather than guessing at a reason."""
    team_games = {team: sorted(grp["sw"].unique())[-n:] for team, grp in df.groupby("team")}
    out = {}
    for aid, p in latest.iterrows():
        dates = team_games.get(p.team, [])
        if not dates:
            continue
        p_played = set(df[df.athlete_id == aid]["sw"])
        dots = [{"week": i + 1, "status": "played" if d in p_played else "missed",
                  "label": f"{int(d[5:7])}/{int(d[8:10])}"} for i, d in enumerate(dates)]
        out[aid] = {"dots": dots, "history": [], "title": f"Last {len(dates)} Games Played"}
    return out


POS_GROUP = {"Centers": "F", "Left Wings": "F", "Right Wings": "F", "Wings": "F",
             "Defense": "D", "Defensemen": "D", "Goalies": "G"}
def load_current_rosters(games):
    """Current full roster per team straight from ESPN -- a separate, faster-moving source than our
    own box-score history (which only reflects players who've actually played a game), used to catch
    a recently-traded/signed player before his new team's box scores start showing him. Only fetched
    for teams with an upcoming game, since that's the only case bootstrapping matters for. One request
    per team (~32), fetched fresh every build rather than cached -- the whole point is freshness, and
    it's a small cost next to the box-score fetches that already dominate build time. Unlike NBA's
    flat athlete list, NHL's roster endpoint groups athletes by position NAME (confirmed against a
    real team before writing this), not an abbreviation -- mapped via POS_GROUP to the same F/D/G
    scheme parse_boxscore() already uses."""
    teams = sorted({g["home"] for g in games} | {g["away"] for g in games})
    out = {}
    for team in teams:
        try:
            d = _get(f"teams/{team.lower()}/roster")
        except Exception as e:
            print(f"  NHL roster fetch failed for {team}: {e}")
            continue
        for grp in d.get("athletes", []):
            pos = POS_GROUP.get(grp.get("position"))
            if not pos:
                continue
            for a in grp.get("items", []):
                aid = a.get("id")
                if not aid:
                    continue
                out[aid] = {"team": team, "pos": pos, "name": a.get("fullName"),
                            "headshot": (a.get("headshot") or {}).get("href")}
    return out


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
    rest = load_rest_days(df, games)
    sgo = load_sgo()
    line_hist = load_line_history()

    latest = df.sort_values("sw").groupby("athlete_id").tail(1).set_index("athlete_id")
    # A player who was recently traded or claimed off waivers has either zero box-score rows under
    # his new team (invisible until his new team's games start flowing in) or -- more often in-season
    # for this sport than NFL -- a stale `latest` row still pointing at his OLD team. ESPN's own
    # team-roster endpoint is a separate, faster-moving source of truth for CURRENT team, so bootstrap/
    # reassign from it. `hist` below is looked up by athlete_id alone (no team filter), so his real
    # game log under his old team still powers his line/history under his new one -- nothing
    # backfilled, just real games re-tagged to his current team, same principle as the NFL site's fix.
    roster = load_current_rosters(games)
    if roster:
        fallback = []
        for aid, r in roster.items():
            cur_team = latest.loc[aid, "team"] if aid in latest.index else None
            if cur_team == r["team"]:
                continue
            if aid not in latest.index:
                continue   # never had a real game logged at all -- nothing to bootstrap from
            fallback.append({"athlete_id": aid, "team": r["team"], "pos": r["pos"],
                              "name": r["name"], "headshot": r["headshot"]})
        if fallback:
            fb = pd.DataFrame(fallback).set_index("athlete_id")
            latest = pd.concat([latest[~latest.index.isin(fb.index)], fb])
    timelines = load_recent_participation(df, latest)
    rows = []
    for g in games:
        for team, opp in ((g["home"], g["away"]), (g["away"], g["home"])):
            cand = latest[latest.team == team]
            for aid, p in cand.iterrows():
                hist = df[df.athlete_id == aid]
                pos = p.pos
                relevant = GOALIE_MARKETS if pos == "G" else SKATER_MARKETS
                recent_min = hist.minutes.tail(8).mean()
                # No volume floor -- every player gets every market regardless of recent ice time,
                # matching the NFL site's identical call (Simon: "remove it entirely, show every
                # player"). `recent_min` is still computed and stored as `vol` below for display/
                # sorting; it just no longer gates whether a prop appears at all.
                for mkey, (label, vmin) in relevant.items():
                    hl = hist.tail(20)
                    if hl.empty:
                        continue
                    est = float(int(hl[mkey].tail(8).mean())) + 0.5
                    rk = ranks.get((opp, pos, mkey))
                    row = {
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
                        "ev_over": None, "ev_under": None, "fair_line": None, "real_alts": [],
                        "line_history": line_hist.get(f"{norm_name(p['name'])}|{mkey}|{g['id']}"),
                        "opp_rest": rest.get((opp, g["id"])),
                        "timeline": timelines.get(aid),
                    }
                    sg = sgo["lines"].get(f"{norm_name(p['name'])}|{mkey}|{g['id']}")
                    if sg and sg["books"]:
                        line = sg["line"] if sg["line"] is not None else est
                        row.update(line=line, src="book", over=sg["over"], under=sg["under"], books=sg["books"])
                        comparable = sg["fair_line"] is not None and abs(line - sg["fair_line"]) <= 1.0
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
