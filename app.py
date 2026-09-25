"""PropBoard NFL - a props.cash-style player prop finder.

Free data: nflverse weekly player stats + injuries, ESPN schedule/game lines.
Optional: The Odds API key (paste it in the page) for real sportsbook prop lines.
"""
import json
import os
import re
import threading
import time
from pathlib import Path

import pandas as pd
import requests
from flask import Flask, jsonify, render_template, request

BASE = Path(__file__).parent
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)
CONFIG_FILE = DATA / "config.json"
ODDS_FILE = DATA / "odds.json"

SEASON = 2026
NFLVERSE = "https://github.com/nflverse/nflverse-data/releases/download"
ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
ODDS_API = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl"
HTTP = {"User-Agent": "Mozilla/5.0 PropBoard"}

ESPN_TO_NFLVERSE = {"WSH": "WAS", "LAR": "LA"}

app = Flask(__name__)
lock = threading.Lock()
_board_cache = {"t": 0, "data": None}


# ---------------------------------------------------------------- markets ---
# key -> label, positions that get this prop, (volume column, min avg volume)
MARKETS = {
    "pass_yds": ("Pass Yds", ["QB"], ("attempts", 15)),
    "pass_tds": ("Pass TDs", ["QB"], ("attempts", 15)),
    "rush_yds": ("Rush Yds", ["QB", "RB", "WR"], ("carries", 6)),
    "rec_yds": ("Rec Yds", ["WR", "TE", "RB"], ("targets", 3.5)),
    "rec": ("Receptions", ["WR", "TE", "RB"], ("targets", 3.5)),
    "rush_rec_yds": ("Rush+Rec Yds", ["RB", "WR", "TE"], ("touches", 9)),
    "any_td": ("Anytime TD", ["RB", "WR", "TE"], ("touches", 6)),
    # First-quarter 5+ yard props: the line is fixed at 4.5, so "Over" means 5 or more.
    "q1_rec": ("Q1 Rec 5+", ["WR", "TE", "RB"], ("targets", 3.5)),
    "q1_rush": ("Q1 Rush 5+", ["RB"], ("carries", 6)),
    # --- added 2026-09-22 ---
    "pass_att": ("Pass Attempts", ["QB"], ("attempts", 15)),
    "pass_comp": ("Pass Completions", ["QB"], ("attempts", 15)),
    "pass_int": ("Interceptions", ["QB"], ("attempts", 15)),
    "pass_rush_yds": ("Pass+Rush Yds", ["QB"], ("attempts", 15)),
    "rush_att": ("Rush Attempts", ["QB", "RB", "WR"], ("carries", 6)),
    "targets_m": ("Targets", ["WR", "TE", "RB"], ("targets", 3.5)),
    "long_pass": ("Longest Pass", ["QB"], ("attempts", 15)),
    "long_rush": ("Longest Rush", ["QB", "RB", "WR"], ("carries", 6)),
    "long_rec": ("Longest Reception", ["WR", "TE", "RB"], ("targets", 3.5)),
    "q1_pass_yds": ("1Q Pass Yds", ["QB"], ("attempts", 15)),
    "q1_rush_yds": ("1Q Rush Yds", ["QB", "RB", "WR"], ("carries", 6)),
    "q1_rec_yds": ("1Q Rec Yds", ["WR", "TE", "RB"], ("targets", 3.5)),
    "q1_any_td": ("1Q TD", ["RB", "WR", "TE"], ("touches", 6)),
    "tackles": ("Tackles", ["LB", "DB", "DL"], ("tackle_vol", 2.5)),
    "assists": ("Assists", ["LB", "DB", "DL"], ("tackle_vol", 2.5)),
    "tackles_ast": ("Tackles+Assists", ["LB", "DB", "DL"], ("tackle_vol", 2.5)),
    "sacks": ("Sacks", ["LB", "DB", "DL"], ("tackle_vol", 2.5)),
    "kick_pts": ("Kicking Points", ["K"], ("kick_vol", 1.5)),
    "fg_made_m": ("Field Goals", ["K"], ("kick_vol", 1.5)),
    "xp_made_m": ("Extra Points", ["K"], ("kick_vol", 1.5)),
}
FIXED_LINES = {"q1_rec": 4.5, "q1_rush": 4.5}
ODDS_MARKETS = {
    "player_pass_yds": "pass_yds",
    "player_pass_tds": "pass_tds",
    "player_rush_yds": "rush_yds",
    "player_reception_yds": "rec_yds",
    "player_receptions": "rec",
    "player_rush_reception_yds": "rush_rec_yds",
    "player_anytime_td": "any_td",
}
BOOK_ORDER = ["fanduel", "draftkings", "betmgm", "caesars", "espnbet", "fanatics"]


# ----------------------------------------------------------------- config ---
def load_config():
    try:
        cfg = json.loads(CONFIG_FILE.read_text())
    except Exception:
        cfg = {}
    if os.environ.get("ODDS_API_KEY"):                 # e.g. a GitHub secret on the public build
        cfg["odds_key"] = os.environ["ODDS_API_KEY"]
    return cfg


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg))


# ------------------------------------------------------------ data files ---
def download(url, dest, max_age_hours):
    """Download url -> dest unless a fresh copy is already there."""
    if dest.exists() and time.time() - dest.stat().st_mtime < max_age_hours * 3600:
        return True
    try:
        r = requests.get(url, headers=HTTP, timeout=60)
        r.raise_for_status()
        dest.write_bytes(r.content)
        return True
    except Exception as e:
        print(f"download failed {url}: {e}")
        return dest.exists()


ZONES = ["deep_left", "deep_middle", "deep_right", "mid_left", "mid_middle", "mid_right",
         "short_left", "short_middle", "short_right", "behind_left", "behind_middle", "behind_right"]


def _zone_of(air_yards):
    if air_yards < 0:
        return "behind"
    if air_yards < 10:
        return "short"
    if air_yards < 20:
        return "mid"
    return "deep"


def _build_q1_tables():
    """Reduce big play-by-play files to small per-game/per-player tables (cached, big file deleted):
    first-quarter yards/TDs (Q1 markets), longest-play (a game's single best pass/rush/catch, any
    quarter), and target/catch counts by field zone (depth bucket x left/middle/right — the receiving
    zone chart on a player's page)."""
    for yr, age in ((SEASON - 1, 24 * 30), (SEASON, 3)):
        small, small_def = DATA / f"q1_{yr}.parquet", DATA / f"q1def_{yr}.parquet"
        zones, zones_def = DATA / f"zones_{yr}.parquet", DATA / f"zonesdef_{yr}.parquet"
        fresh = all(f.exists() and time.time() - f.stat().st_mtime < age * 3600
                    for f in (small, small_def, zones, zones_def))
        if fresh:
            continue
        big = DATA / f"pbp_{yr}.parquet"
        if not download(f"{NFLVERSE}/pbp/play_by_play_{yr}.parquet", big, 0):
            continue
        p = pd.read_parquet(big, columns=[
            "game_id", "qtr", "play_type", "epa", "defteam",
            "receiver_player_id", "receiving_yards", "rusher_player_id", "rushing_yards",
            "passer_player_id", "passing_yards", "pass_touchdown", "rush_touchdown", "td_player_id",
            "pass_location", "air_yards", "complete_pass", "pass_attempt"])

        # longest single play this game, any quarter (a sack/no-gain doesn't count as anyone's "longest")
        long_rec = (p[p.receiving_yards > 0].groupby(["game_id", "receiver_player_id"]).receiving_yards.max()
                    .rename("long_rec").rename_axis(["game_id", "player_id"]))
        long_rush = (p[p.rushing_yards > 0].groupby(["game_id", "rusher_player_id"]).rushing_yards.max()
                     .rename("long_rush").rename_axis(["game_id", "player_id"]))
        long_pass = (p[p.passing_yards > 0].groupby(["game_id", "passer_player_id"]).passing_yards.max()
                     .rename("long_pass").rename_axis(["game_id", "player_id"]))

        q1p = p[p.qtr == 1]
        rec = (q1p.dropna(subset=["receiver_player_id"])
               .groupby(["game_id", "receiver_player_id"]).receiving_yards.sum()
               .rename("q1_rec").rename_axis(["game_id", "player_id"]))
        rush = (q1p.dropna(subset=["rusher_player_id"])
                .groupby(["game_id", "rusher_player_id"]).rushing_yards.sum()
                .rename("q1_rush").rename_axis(["game_id", "player_id"]))
        pas = (q1p.dropna(subset=["passer_player_id"])
               .groupby(["game_id", "passer_player_id"]).passing_yards.sum()
               .rename("q1_pass").rename_axis(["game_id", "player_id"]))
        # scorer only (receiver on a pass TD, rusher on a run TD) — excludes the passer, return/pick-6 TDs,
        # matching how the full-game any_td market is already defined (rushing_tds + receiving_tds)
        td_rows = q1p[(q1p.pass_touchdown == 1) | (q1p.rush_touchdown == 1)]
        q1_td = (td_rows.dropna(subset=["td_player_id"]).groupby(["game_id", "td_player_id"]).size()
                 .rename("q1_any_td").rename_axis(["game_id", "player_id"]))

        pd.concat([rec, rush, pas, q1_td, long_rec, long_rush, long_pass], axis=1).reset_index().to_parquet(small)
        d = q1p[q1p.epa.notna() & q1p.defteam.notna() & q1p.play_type.isin(["pass", "run"])]
        (d.groupby(["defteam", "play_type"]).epa.agg(["sum", "count"]).reset_index()
         .to_parquet(small_def))

        tgt = p[(p.pass_attempt == 1)].dropna(subset=["receiver_player_id", "pass_location", "air_yards"]).copy()
        tgt["zone"] = tgt.air_yards.apply(_zone_of) + "_" + tgt.pass_location
        (tgt.groupby(["receiver_player_id", "zone"])
         .agg(targets=("complete_pass", "size"), catches=("complete_pass", "sum"))
         .reset_index().rename(columns={"receiver_player_id": "player_id"}).to_parquet(zones))
        (tgt.groupby(["defteam", "zone"])
         .agg(targets=("complete_pass", "size"), catches=("complete_pass", "sum"))
         .reset_index().to_parquet(zones_def))
        big.unlink()


def _read_all(prefix):
    frames = [pd.read_parquet(f) for yr in (SEASON - 1, SEASON)
              if (f := DATA / f"{prefix}_{yr}.parquet").exists()]
    return pd.concat(frames, ignore_index=True) if frames else None


def load_q1():
    """First-quarter yards/TDs and game-long longest-play, per player per game, from play-by-play."""
    _build_q1_tables()
    q1 = _read_all("q1")
    cols = ["game_id", "player_id", "q1_rec", "q1_rush", "q1_pass", "q1_any_td",
            "long_rec", "long_rush", "long_pass"]
    return q1 if q1 is not None else pd.DataFrame(columns=cols)


def load_zones():
    """Target/catch counts by field zone (depth bucket x left/middle/right), for a player and for each
    defense allowing them — the receiving zone chart on a player's page. Rank 1 = highest catch rate
    allowed in that zone = the weakest defense there, matching the site's existing "1st = allows the
    most" convention elsewhere."""
    _build_q1_tables()
    zp, zd = _read_all("zones"), _read_all("zonesdef")
    if zp is None or zd is None:
        return {}, {}
    zp = zp.groupby(["player_id", "zone"])[["targets", "catches"]].sum().reset_index()
    zd = zd.groupby(["defteam", "zone"])[["targets", "catches"]].sum().reset_index()

    zones_player = {}
    for pid, grp in zp.groupby("player_id"):
        total = int(grp.targets.sum())
        if total < 8:
            continue
        zones_player[pid] = {"total": total, "zones": {
            r.zone: {"targets": int(r.targets), "pct": round(r.targets / total * 100, 1),
                      "catches": int(r.catches),
                      "catch_rate": round(r.catches / r.targets * 100) if r.targets else None}
            for r in grp.itertuples() if r.zone in ZONES}}

    zones_def = {team: {r.zone: {"targets": int(r.targets), "catches": int(r.catches),
                                  "catch_rate": round(r.catches / r.targets * 100) if r.targets else None}
                         for r in grp.itertuples() if r.zone in ZONES}
                 for team, grp in zd.groupby("defteam")}
    for z in ZONES:
        rates = {t: zones_def[t][z]["catch_rate"] for t in zones_def
                 if z in zones_def[t] and zones_def[t][z]["catch_rate"] is not None}
        for i, t in enumerate(sorted(rates, key=lambda t: -rates[t]), 1):
            zones_def[t][z]["rank"] = i
    return zones_player, zones_def


def load_q1_def():
    """Opponent first-quarter EPA/play allowed on passes and runs (both seasons combined)."""
    d = _read_all("q1def")
    if d is None:
        return {}
    d = d.groupby(["defteam", "play_type"])[["sum", "count"]].sum()
    return (d["sum"] / d["count"]).unstack()          # rows = defense, columns = pass / run


def load_stats():
    frames = []
    for yr, age in ((SEASON - 1, 24 * 30), (SEASON, 3)):
        f = DATA / f"pw{yr}.parquet"
        if download(f"{NFLVERSE}/stats_player/stats_player_week_{yr}.parquet", f, age):
            frames.append(pd.read_parquet(f))
    df = pd.concat(frames, ignore_index=True)
    # Collapse related positions so the position filter stays usable (a raw 16-way defensive split isn't):
    # DL = DE/DT/NT, LB = ILB/MLB/OLB, DB = CB/S/SAF/FS. K and the offensive skill positions stay as-is.
    POS_MAP = {"FB": "RB", "DE": "DL", "DT": "DL", "NT": "DL",
               "ILB": "LB", "MLB": "LB", "OLB": "LB", "CB": "DB", "S": "DB", "SAF": "DB", "FS": "DB"}
    df = df[df.position.isin(["QB", "RB", "FB", "WR", "TE", "K", *POS_MAP])].copy()
    df["position"] = df.position.replace(POS_MAP)
    df["pass_yds"] = df.passing_yards
    df["pass_tds"] = df.passing_tds
    df["pass_att"] = df.attempts
    df["pass_comp"] = df.completions
    df["pass_int"] = df.passing_interceptions
    df["pass_rush_yds"] = df.passing_yards + df.rushing_yards
    df["rush_yds"] = df.rushing_yards
    df["rush_att"] = df.carries
    df["rec_yds"] = df.receiving_yards
    df["rec"] = df.receptions
    df["targets_m"] = df.targets
    df["rush_rec_yds"] = df.rushing_yards + df.receiving_yards
    df["any_td"] = df.rushing_tds + df.receiving_tds
    df["touches"] = df.carries + df.targets
    # defense/kicking: not in the weekly file's own per-market columns, so build the volume proxy here too
    df["tackles"] = df.def_tackles_solo.fillna(0)
    df["assists"] = df.def_tackle_assists.fillna(0)
    df["tackles_ast"] = df.def_tackles_solo.fillna(0) + df.def_tackle_assists.fillna(0)
    df["sacks"] = df.def_sacks.fillna(0)
    df["tackle_vol"] = df.def_tackles_solo.fillna(0) + df.def_tackle_assists.fillna(0)
    df["kick_pts"] = df.fg_made.fillna(0) * 3 + df.pat_made.fillna(0)
    df["fg_made_m"] = df.fg_made.fillna(0)
    df["xp_made_m"] = df.pat_made.fillna(0)
    df["kick_vol"] = df.fg_att.fillna(0) + df.pat_att.fillna(0)

    q1 = load_q1()
    df = df.merge(q1, on=["game_id", "player_id"], how="left")
    known = df.game_id.isin(set(q1.game_id))          # games we have play-by-play for
    df["q1_rec_tgt"] = df.q1_rec.notna() & known      # had at least one Q1 target / carry
    df["q1_rush_att"] = df.q1_rush.notna() & known
    fill0 = ["q1_rec", "q1_rush", "q1_pass", "q1_any_td"]
    df.loc[known, fill0] = df.loc[known, fill0].fillna(0)
    # q1_rush/q1_rec double as the source for BOTH the fixed "5+" markets (q1_rush, q1_rec) and the
    # normal-line yardage markets (q1_rush_yds, q1_rec_yds) — same numbers, two different display treatments
    df["q1_rush_yds"] = df["q1_rush"]
    df["q1_rec_yds"] = df["q1_rec"]
    df["q1_pass_yds"] = df["q1_pass"]
    # long_pass/long_rush/long_rec are left as real NaN (not 0) on a game with no qualifying play — the
    # board loop already drops those games from a player's history rather than counting them as a "0 yard"
    # longest play, which would understate how often he actually clears a given longest-play line
    return df.sort_values(["season", "week"]).reset_index(drop=True)


def load_schedule():
    """Historical + current game lines (spread_line > 0 means the home team is favored)."""
    f = DATA / "games.csv"
    url = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
    if not download(url, f, 12):
        return {}
    g = pd.read_csv(f, usecols=["game_id", "gameday", "home_team", "spread_line", "total_line"])
    g = g[g.game_id.str[:4].astype(int) >= SEASON - 1]
    return {r.game_id: (r.gameday, r.home_team, r.spread_line, r.total_line) for r in g.itertuples()}


def load_injuries():
    f = DATA / f"inj{SEASON}.parquet"
    if not download(f"{NFLVERSE}/injuries/injuries_{SEASON}.parquet", f, 3):
        return pd.DataFrame(), 0, {}
    raw = pd.read_parquet(f).sort_values("week")
    latest = raw.groupby("gsis_id").report_status.last().to_dict()   # newest report, even if blank
    inj = raw[raw.report_status.notna()]
    return inj, int(raw.week.max()) if len(raw) else 0, latest


# ------------------------------------------------------------------- ESPN ---
ESPN_CDN = "https://cdn.espn.com/core/nfl/scoreboard"


def espn_scoreboard(week=None):
    """ESPN's main feed refuses some cloud servers (GitHub's included), so fall back to their CDN copy."""
    params = {"week": week, "seasontype": 2, "dates": SEASON} if week else None
    try:
        data = requests.get(ESPN, params=params, headers=HTTP, timeout=20).json()
        if "events" in data:
            return data
    except Exception as e:
        print("ESPN main feed unavailable, using the CDN copy:", str(e)[:80])
    cdn = {"xhr": 1, "limit": 50}
    if week:
        cdn.update(week=week, year=SEASON, seasontype=2)
    return requests.get(ESPN_CDN, params=cdn, headers=HTTP, timeout=30).json()["content"]["sbData"]


def get_games():
    """Upcoming/in-progress games from the current NFL week and the next one."""
    try:
        cur = espn_scoreboard()
    except Exception as e:
        print("ESPN failed:", e)
        return []
    week = cur["week"]["number"]
    pages = [cur]
    try:
        pages.append(espn_scoreboard(week + 1))
    except Exception:
        pass
    games, seen = [], set()
    for wk, page in zip((week, week + 1), pages):
        for ev in page.get("events", []):
            comp = ev["competitions"][0]
            state = comp["status"]["type"]["state"]
            if state == "post" or ev["id"] in seen:
                continue
            seen.add(ev["id"])
            home = next(c for c in comp["competitors"] if c["homeAway"] == "home")
            away = next(c for c in comp["competitors"] if c["homeAway"] == "away")
            odds = (comp.get("odds") or [{}])[0]
            games.append({
                "id": ev["id"], "week": wk, "state": state, "date": ev["date"],
                "home": home["team"]["abbreviation"], "away": away["team"]["abbreviation"],
                "home_name": home["team"].get("displayName"), "away_name": away["team"].get("displayName"),
                "home_logo": home["team"].get("logo"), "away_logo": away["team"].get("logo"),
                "spread": odds.get("details"), "total": odds.get("overUnder"),
                "home_spread": odds.get("spread"),
            })
    games.sort(key=lambda g: g["date"])
    return games


# ---------------------------------------------------------- sportsbook API ---
def norm_name(n):
    n = re.sub(r"[^a-z ]", "", (n or "").lower().replace(".", ""))
    return re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", n).strip()


def load_odds():
    try:
        return json.loads(ODDS_FILE.read_text())
    except Exception:
        return {"pulled": None, "remaining": None, "lines": {}}


# --------------------------------------------------- SportsGameOdds (NFL only) ---
# Real book lines, real alt-line prices, and a fair (no-vig) line for EV — see nfl-props-site.md
# memory for how this was scoped. Manual pull only (button in the app): 1 credit per game, ~16-20
# for a full NFL slate, 2,500/month on the free "amateur" tier — nowhere near enough to run on the
# 3-hourly auto-rebuild, so this never runs automatically.
SGO_KEY_FILE = DATA / "sgo_key.txt"
SGO_FILE = DATA / "sgo_odds.json"
SGO_API = "https://api.sportsgameodds.com/v2"
SGO_MARKETS = {                    # (SportsGameOdds statID, periodID) -> our market key
    ("passing_yards", "game"): "pass_yds", ("passing_touchdowns", "game"): "pass_tds",
    ("passing_attempts", "game"): "pass_att", ("passing_completions", "game"): "pass_comp",
    ("passing_interceptions", "game"): "pass_int", ("passing_longestCompletion", "game"): "long_pass",
    ("passing+rushing_yards", "game"): "pass_rush_yds", ("passing_yards", "1q"): "q1_pass_yds",
    ("rushing_yards", "game"): "rush_yds", ("rushing_attempts", "game"): "rush_att",
    ("rushing_longestRush", "game"): "long_rush", ("rushing_yards", "1q"): "q1_rush_yds",
    ("receiving_yards", "game"): "rec_yds", ("receiving_receptions", "game"): "rec",
    ("receiving_targets", "game"): "targets_m", ("receiving_longestReception", "game"): "long_rec",
    ("receiving_yards", "1q"): "q1_rec_yds", ("rushing+receiving_yards", "game"): "rush_rec_yds",
    ("touchdowns", "game"): "any_td",     # betTypeID 'yn' (yes/no), not 'ou' — handled separately below
    ("touchdowns", "1q"): "q1_any_td",    # same yn/ou split, same gate
    ("defense_soloTackles", "game"): "tackles", ("defense_assistedTackles", "game"): "assists",
    ("defense_combinedTackles", "game"): "tackles_ast", ("defense_sacks", "game"): "sacks",
    ("kicking_totalPoints", "game"): "kick_pts", ("fieldGoals_made", "game"): "fg_made_m",
    ("extraPoints_kicksMade", "game"): "xp_made_m",
}
TD_MARKETS = {"any_td", "q1_any_td"}
ZONE_MARKETS = {"rec_yds", "rec", "targets_m", "long_rec"}    # receiving-only -- zone edge is about routes/targets
EXTRA_BOOKS = ["bovada", "pointsbet", "unibet", "williamhill"]   # SGO books not already in BOOK_ORDER
for _b in EXTRA_BOOKS:
    if _b not in BOOK_ORDER:
        BOOK_ORDER.append(_b)


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


def pull_sgo(key, games=None):
    """One /events call per ~10 games (paginated); each EVENT returned costs 1 credit regardless of
    how many markets/books come back with it. Scoped to our own board's date window (this week +
    next) via startsAfter/startsBefore — without that, SportsGameOdds returns every event with odds
    posted anywhere in the season (60+ games, most of them weeks away), ~4x the credits for games we
    don't even show."""
    params_extra = {}
    if games:
        import datetime
        last = max(g["date"] for g in games)[:10]
        end = (datetime.date.fromisoformat(last) + datetime.timedelta(days=1)).isoformat()
        params_extra = {"startsAfter": min(g["date"] for g in games)[:10], "startsBefore": end}
    events, cursor = [], None
    for _ in range(6):                              # a full NFL week is ~16 games = 2 pages
        params = {"apiKey": key, "leagueID": "NFL", "oddsAvailable": "true", "includeAltLines": "true",
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

    # Map each SGO event to OUR game id by (home, away) team codes — an oddID like
    # "touchdowns-X-game-yn-yes" repeats byte-for-byte for the same player across a different game
    # (e.g. his team's game this week vs. next week, both inside our pull window), so without this a
    # player with games in both weeks gets his two games' odds silently merged into one (caught by a
    # nonsense +198% EV that traced back to two different games' prices overwriting each other).
    game_by_teams = {}
    if games:
        for g in games:
            home, away = ESPN_TO_NFLVERSE.get(g["home"], g["home"]), ESPN_TO_NFLVERSE.get(g["away"], g["away"])
            game_by_teams[(home, away)] = g["id"]

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
            # Many statIDs (not just "touchdowns") carry TWO parallel markets: a milestone yes/no bet
            # ("will he reach X yards: yes/no") and the real over/under line+alt-ladder we actually want.
            # For the two genuinely-binary markets (any_td, q1_any_td) it's the reverse — the yes/no
            # IS the market (2+ TDs 'ou' is a different market we don't have). Without this gate, whichever
            # one the API happens to list second silently overwrites the other's odds under the same key.
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
                "fair_over": None, "fair_under": None, "books": {}, "alts": {},
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
                for alt in b.get("altLines") or []:
                    if not alt.get("available") or alt.get("overUnder") is None:
                        continue
                    aam = _sgo_american(alt.get("odds"))
                    if aam is None:
                        continue
                    try:
                        t = float(alt["overUnder"])
                    except (TypeError, ValueError):
                        continue
                    ae = entry["alts"].setdefault(t, {})
                    if slot not in ae or book == "fanduel":     # prefer fanduel's price when several books tie
                        ae[slot] = {"book": book, "odds": aam}

    for entry in lines.values():
        # Thinner markets (defense/kicking especially) often carry a top-level consensus price with no
        # byBookmaker breakdown at all — real data, just not attributed to one book. Surface it rather
        # than silently falling back to our own EST, but label it honestly as a blend, not a named book.
        if not entry["books"] and (entry["over"] is not None or entry["under"] is not None):
            entry["books"]["sgo"] = {"book": "sgo consensus", "line": entry["line"],
                                      "over": entry["over"], "under": entry["under"]}
        entry["books"] = sorted(entry["books"].values(),
                                 key=lambda b: BOOK_ORDER.index(b["book"]) if b["book"] in BOOK_ORDER else 99)
        entry["alts"] = [{"t": t, **sides} for t, sides in sorted(entry["alts"].items())]

    out = {"pulled": time.time(), "events": len(events), "lines": lines}
    SGO_FILE.write_text(json.dumps(out))
    return out


def american_to_prob(odds):
    if odds is None:
        return None
    return 100 / (odds + 100) if odds > 0 else -odds / (-odds + 100)


def ev_pct(book_odds, fair_odds):
    """EV% of a $1 bet: uses the fair (no-vig) odds' implied probability as the 'true' one."""
    if book_odds is None or fair_odds is None:
        return None
    p_fair = american_to_prob(fair_odds)
    d_book = 1 + (book_odds / 100 if book_odds > 0 else 100 / abs(book_odds))
    return round((p_fair * d_book - 1) * 100, 1)


def pull_odds(api_key):
    """One pull = 1 call per game x 7 markets. Only runs when the user clicks the button."""
    r = requests.get(f"{ODDS_API}/events", params={"apiKey": api_key}, timeout=30)
    r.raise_for_status()
    events = r.json()
    lines, remaining = {}, None
    for ev in events:
        resp = requests.get(
            f"{ODDS_API}/events/{ev['id']}/odds",
            params={"apiKey": api_key, "regions": "us", "oddsFormat": "american",
                    "markets": ",".join(ODDS_MARKETS)},
            timeout=30)
        remaining = resp.headers.get("x-requests-remaining", remaining)
        if resp.status_code != 200:
            print("odds error", resp.status_code, resp.text[:200])
            if resp.status_code in (401, 429):
                raise RuntimeError(resp.json().get("message", "Odds API refused the request"))
            continue
        for bk in resp.json().get("bookmakers", []):
            for mk in bk.get("markets", []):
                mkey = ODDS_MARKETS.get(mk["key"])
                if not mkey:
                    continue
                per = {}
                for o in mk.get("outcomes", []):
                    who = norm_name(o.get("description"))
                    side = o["name"].lower()
                    d = per.setdefault(who, {"line": o.get("point", 0.5)})
                    if side in ("over", "yes"):
                        d["over"] = o["price"]
                        d["line"] = o.get("point", 0.5)
                    elif side in ("under", "no"):
                        d["under"] = o["price"]
                for who, d in per.items():
                    d["book"] = bk["key"]
                    lines.setdefault(f"{who}|{mkey}", []).append(d)
    out = {"pulled": time.time(), "remaining": remaining, "lines": lines}
    ODDS_FILE.write_text(json.dumps(out))
    return out


# ------------------------------------------------------------ board build ---
def ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def defense_ranks(df):
    """How much each defense allows per game to each position (rank 1 = allows the most)."""
    ranks, dvp, recent = {}, {}, {}
    for mkey, (_, positions, _) in MARKETS.items():
        for pos in positions:
            sub = df[df.position == pos].copy()
            sub[mkey] = sub[mkey].fillna(0)          # per-game NaN (no qualifying play) -> 0 for this defense-allowed rollup only
            sub = sub.sort_values(mkey, ascending=False)
            per_game = (sub.groupby(["opponent_team", "season", "week"])
                        .agg(total=(mkey, "sum"), top=(mkey, "max"),
                             name=("player_display_name", "first"), off=("team", "first"))
                        .reset_index().sort_values(["season", "week"]))
            last = per_game.groupby("opponent_team").tail(17)
            avg = last.groupby("opponent_team").total.mean()
            rk = avg.rank(ascending=False, method="min").astype(int)
            dvp[f"{pos}|{mkey}"] = {
                "league": round(float(avg.mean()), 2), "n": 32,
                "teams": {t: [int(rk[t]), round(float(avg[t]), 2)] for t in avg.index},
            }
            for team, r in rk.items():
                ranks[(team, pos, mkey)] = int(r)
            for team, grp in per_game.groupby("opponent_team"):
                recent[f"{team}|{pos}|{mkey}"] = [
                    [int(x.season), int(x.week), x.off, round(float(x.total), 1), x.name, round(float(x.top), 1)]
                    for x in grp.tail(5).itertuples()]
    return ranks, dvp, recent


def usage_shares(cur, latest, games):
    """Recent touch share (carries+targets, last 6 games) among a team's RBs/WRs/TEs — matchup grade alone
    can't tell a workhorse from the backup sharing his own team's grade, so show who actually gets the ball."""
    teams_in_play = {ESPN_TO_NFLVERSE.get(t, t) for g in games for t in (g["home"], g["away"])}
    recent_touches = cur.sort_values("week").groupby("player_id").touches.apply(lambda s: s.tail(6).mean())
    pool = latest[latest.position.isin(["RB", "WR", "TE"]) & latest.team.isin(teams_in_play)]
    out = {}
    for (team, pos), grp in pool.groupby(["team", "position"]):
        vals = [(p.player_display_name, round(float(recent_touches.get(pid, 0)), 1))
                for pid, p in grp.iterrows() if recent_touches.get(pid, 0) > 0]
        if len(vals) < 2:
            continue
        vals.sort(key=lambda x: -x[1])
        out[f"{team}|{pos}"] = vals
    return out


def _favorable_pct(zp, zd):
    """% of a player's own targets that land in zones where a given defense ranks in the weak third
    (rank <= 10 of 32) — the one number both zone_fit() and zone_edge() below are built from."""
    if not zp or not zd:
        return None
    return round(sum(v["pct"] for z, v in zp["zones"].items() if zd.get(z, {}).get("rank", 99) <= 10), 1)


def zone_fit(games, latest, zones_player, zones_def):
    """Rank each team's WR/TE/RBs by how much of their OWN target share lands in zones where this
    week's specific opponent is weak — the zone chart alone only answers "how does this one player fit,"
    not "which of these guys should I even be looking at" without clicking through every player."""
    out = {}
    for g in games:
        for team, opp in ((g["home"], g["away"]), (g["away"], g["home"])):
            t, o = ESPN_TO_NFLVERSE.get(team, team), ESPN_TO_NFLVERSE.get(opp, opp)
            zd = zones_def.get(o)
            if not zd:
                continue
            pool = latest[(latest.team == t) & latest.position.isin(["WR", "TE", "RB"])]
            vals = []
            for pid, r in pool.iterrows():
                zp = zones_player.get(pid)
                if not zp or zp["total"] < 8:
                    continue
                vals.append((r.player_display_name, _favorable_pct(zp, zd)))
            if len(vals) < 2:
                continue
            vals.sort(key=lambda x: -x[1])
            out[f"{t}|{o}"] = vals
    return out


def zone_edge(pid, opp, zones_player, zones_def):
    """Same favorable-zone% as zone_fit(), but for one player against one specific opponent — attached
    directly to his receiving prop rows so it's sortable/filterable on the board, not just visible after
    clicking into his own page."""
    zp = zones_player.get(pid)
    if not zp or zp["total"] < 8:
        return None
    return _favorable_pct(zp, zones_def.get(opp))


def game_entry(sched, r, mkey):
    """[season, week, opp, value, date, was_home, fav_margin, total] for one past game."""
    day, home_team, spread, total = sched.get(r.game_id, (None, None, None, None))
    was_home = 1 if r.team == home_team else 0
    fav = None if pd.isna(spread) else float(spread if was_home else -spread)
    return [int(r.season), int(r.week), r.opponent_team, float(getattr(r, mkey)),
            day, was_home, fav, None if pd.isna(total) else float(total)]


# ------------------------------------------------- Q1 5+ sharpness tiers ---
# Port of ~/nfl-q1-bot/nfl_q1_engine.py: only each team's top WR and top RB get a tier.
# The engine ranks defenses 1 = allows the MOST Q1 EPA (weakest) but then rewards the HIGH rank
# numbers, i.e. it favors tough defenses. A 2025 backtest shows weak defenses give more Q1 5+ hits
# (WR 75% vs 63%), so the term is flipped here. Set to False to reproduce the original engine exactly.
FIX_EPA_RANK_DIRECTION = True
TIERS = [(0.80, "ELITE"), (0.72, "STRONG"), (0.63, "LEAN")]


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def tier_of(score):
    return next((name for cut, name in TIERS if score >= cut), None)


def load_roster():
    f = DATA / f"roster{SEASON}.parquet"
    if not download(f"{NFLVERSE}/rosters/roster_{SEASON}.parquet", f, 24):
        return pd.DataFrame()
    r = pd.read_parquet(f).sort_values("week")
    return r.groupby("gsis_id").tail(1)[["gsis_id", "team", "position", "pfr_id", "full_name"]]


def load_snap_share():
    """Average offensive snap share (0-1) per pfr id across both seasons."""
    frames = []
    for yr, age in ((SEASON - 1, 24 * 30), (SEASON, 3)):
        f = DATA / f"snaps{yr}.parquet"
        if download(f"{NFLVERSE}/snap_counts/snap_counts_{yr}.parquet", f, age):
            frames.append(pd.read_parquet(f, columns=["pfr_player_id", "offense_pct"]))
    if not frames:
        return {}
    sn = pd.concat(frames).dropna(subset=["offense_pct"])
    return sn.groupby("pfr_player_id").offense_pct.mean().to_dict()


def load_snap_avg():
    """Recent average offensive snaps/game, keyed by gsis_id (bridged from pfr_id via the roster)."""
    roster = load_roster()
    if roster.empty:
        return {}
    pfr_to_gsis = dict(zip(roster.pfr_id, roster.gsis_id))
    frames = []
    for yr, age in ((SEASON - 1, 24 * 30), (SEASON, 3)):
        f = DATA / f"snaps{yr}.parquet"
        if download(f"{NFLVERSE}/snap_counts/snap_counts_{yr}.parquet", f, age):
            frames.append(pd.read_parquet(f, columns=["pfr_player_id", "season", "week",
                                                        "offense_snaps", "defense_snaps"]))
    if not frames:
        return {}
    sn = pd.concat(frames)
    # a player accumulates one side or the other, essentially never both -- sum is safe and avoids
    # showing "0.0 snaps" for defensive players just because offense_snaps alone is 0 for them
    sn["snaps"] = sn.offense_snaps.fillna(0) + sn.defense_snaps.fillna(0)
    sn["sw"] = sn.season * 100 + sn.week
    avg = sn.sort_values("sw").groupby("pfr_player_id").snaps.apply(lambda s: s.tail(4).mean())
    return {pfr_to_gsis[pfr]: round(float(v), 1) for pfr, v in avg.items() if pfr in pfr_to_gsis}


def roster_activity(inj, inj_week, cur, snap_avg):
    """Questionable/Doubtful/Out players this week, grouped by team, with recent usage context —
    matchup grade alone doesn't say whether a banged-up starter is even likely to play/keep his role."""
    if not len(inj) or inj_week <= 0:
        return {}
    touch_avg = (cur.sort_values("week").groupby("player_id").touches
                 .apply(lambda s: s.tail(4).mean()).to_dict())
    img_by_pid = cur.sort_values("week").groupby("player_id").headshot_url.last().to_dict()
    week_rows = inj[(inj.week == inj_week) & inj.report_status.isin(["Questionable", "Doubtful", "Out"])]
    out = {}
    for r in week_rows.itertuples():
        touches = touch_avg.get(r.gsis_id)
        snaps = snap_avg.get(r.gsis_id)
        img = img_by_pid.get(r.gsis_id)
        out.setdefault(r.team, []).append({
            "pid": r.gsis_id, "name": r.full_name, "pos": r.position, "status": r.report_status,
            "snaps": snaps, "touches": round(float(touches), 1) if touches is not None and touches > 0 else None,
            "img": None if img is None or pd.isna(img) else img,
        })
    return out


def load_starting_qbs():
    f = DATA / f"depth{SEASON}.parquet"
    if not download(f"{NFLVERSE}/depth_charts/depth_charts_{SEASON}.parquet", f, 3):
        return {}
    d = pd.read_parquet(f)
    d = d[d.pos_abb == "QB"].sort_values(["dt", "pos_rank"], ascending=[False, True])
    return {r.team: (r.gsis_id, r.player_name) for r in d.groupby("team").head(1).itertuples()}


def q1_signals(df, kind):
    """Per player: recency-weighted Q1 5+ hit rate, early involvement, total yards, aDOT."""
    if kind == "rec":
        had, yd, tot, vol, pos = "q1_rec_tgt", "q1_rec", "receiving_yards", "targets", "WR"
    else:
        had, yd, tot, vol, pos = "q1_rush_att", "q1_rush", "rushing_yards", "carries", "RB"
    out = {}
    for pid, grp in df[df.position == pos].groupby("player_id"):
        # Every game he played counts. A game with no Q1 target/carry is a miss (0 yards). The original
        # engine skipped those games, so a player with two straight Q1 shutouts still showed a 100% hit
        # rate. Counting them predicts better in a 2025 backtest (AUC .593 vs .570, Brier .262 vs .308).
        q = grp[grp[yd].notna()]
        if len(q) < 3:
            continue
        hits = (q[yd] >= 5).astype(float).tolist()
        games_any = int((grp[vol] > 0).sum())
        out[pid] = {
            "g": len(q), "hit": 0.6 * sum(hits[-5:]) / len(hits[-5:]) + 0.4 * sum(hits) / len(hits),
            "inv": int(grp[had].sum()) / max(games_any, 1), "tot": float(grp[tot].sum()),
            "adot": (float(grp.receiving_air_yards.sum() / grp.targets.sum())
                     if kind == "rec" and grp.targets.sum() > 0 else None),
        }
    return out


def q1_tiers(df, games, inj_latest):
    """{(gsis_id, market, game_id): tier info} for each team's best WR and RB in each upcoming game."""
    roster, snap, qbs, dfe = load_roster(), load_snap_share(), load_starting_qbs(), load_q1_def()
    if roster.empty or len(dfe) == 0:
        return {}
    ranks = {"pass": dfe["pass"].rank(ascending=False, method="min"),      # 1 = allows the most EPA
             "run": dfe["run"].rank(ascending=False, method="min")}
    best = {}
    for kind, market, pos in (("rec", "q1_rec", "WR"), ("rush", "q1_rush", "RB")):
        sig = q1_signals(df, kind)
        cand = roster[(roster.position == pos) & roster.gsis_id.isin(sig)].copy()
        cand["tot"] = cand.gsis_id.map(lambda g: sig[g]["tot"])
        for team, grp in cand.sort_values("tot", ascending=False).groupby("team"):
            top = grp.iloc[0]
            best[(team, market)] = (top.gsis_id, top.full_name, sig[top.gsis_id],
                                    snap.get(top.pfr_id, 0.6))

    def out_status(gid):
        return inj_latest.get(gid) in ("Out", "Doubtful")

    result = {}
    for g in games:
        if g["state"] != "pre":
            continue
        total = float(g["total"]) if g.get("total") else 44.0
        hs = g.get("home_spread")
        for side, team, opp in (("home", g["home"], g["away"]), ("away", g["away"], g["home"])):
            t, d = ESPN_TO_NFLVERSE.get(team, team), ESPN_TO_NFLVERSE.get(opp, opp)
            if d not in dfe.index:
                continue
            impl = total / 2 if hs is None else total / 2 - (hs if side == "home" else -hs) / 2
            impn = clamp((impl - 16) / (30 - 16))
            for market, ptype in (("q1_rec", "pass"), ("q1_rush", "run")):
                if (t, market) not in best:
                    continue
                gid, name, sg, sn = best[(t, market)]
                rank = int(ranks[ptype][d])
                rterm = (33 - rank) / 32 if FIX_EPA_RANK_DIRECTION else rank / 32
                status = inj_latest.get(gid)
                status = status if isinstance(status, str) else None
                info = {"name": name, "rank": rank, "impl": round(impl, 1), "status": status,
                        "qb_out": False, "qb": None, "tier": None, "score": None, "parts": []}
                if out_status(gid):
                    info["tier"] = "OUT"
                    result[(gid, market, g["id"])] = info
                    continue
                if market == "q1_rec":
                    adotn = clamp((14 - (sg["adot"] or 8)) / 12)
                    parts = [("Q1 5+ hit rate (recent-weighted)", f"{sg['hit'] * 100:.0f}%", 0.44, sg["hit"]),
                             ("Snap share", f"{sn * 100:.0f}%", 0.18, sn),
                             ("Early involvement", f"{sg['inv'] * 100:.0f}%", 0.10, sg["inv"]),
                             ("Opp Q1 pass defense", f"allows {ordinal(rank)}-most EPA", 0.12, rterm),
                             ("Team implied total", f"{impl:.1f}", 0.12, impn),
                             ("Short-target profile (aDOT)", f"{sg['adot'] or 8:.1f} yds", 0.04, adotn)]
                    qb = qbs.get(t)
                    if qb:
                        info["qb"], info["qb_out"] = qb[1], out_status(qb[0])
                else:
                    parts = [("Q1 5+ hit rate (recent-weighted)", f"{sg['hit'] * 100:.0f}%", 0.42, sg["hit"]),
                             ("Snap share", f"{sn * 100:.0f}%", 0.16, sn),
                             ("Early involvement", f"{sg['inv'] * 100:.0f}%", 0.12, sg["inv"]),
                             ("Opp Q1 run defense", f"allows {ordinal(rank)}-most EPA", 0.16, rterm),
                             ("Team implied total", f"{impl:.1f}", 0.14, impn)]
                score = sum(w * v for _, _, w, v in parts)
                if info["qb_out"]:
                    score *= 0.55
                info["score"] = round(score, 3)
                info["tier"] = tier_of(score)
                info["parts"] = [[lab, disp, w, round(w * v, 3)] for lab, disp, w, v in parts]
                result[(gid, market, g["id"])] = info
    return result


def build_board():
    df = load_stats()
    games = get_games()
    inj, inj_week, inj_latest = load_injuries()
    odds = load_odds()
    sgo = load_sgo()
    ranks, dvp, recent = defense_ranks(df)
    tiers = q1_tiers(df, games, inj_latest)
    sched = load_schedule()

    status = {}
    if len(inj):
        for _, r in inj.iterrows():
            status[(r.gsis_id, int(r.week))] = r.report_status

    cur = df[df.season == SEASON]
    latest = cur.sort_values("week").groupby("player_id").tail(1).set_index("player_id")
    usage = usage_shares(cur, latest, games)
    roster_act = roster_activity(inj, inj_week, cur, load_snap_avg())
    zones_player, zones_def = load_zones()
    zf = zone_fit(games, latest, zones_player, zones_def)

    rows = []
    for g in games:
        for team, opp, home in ((g["home"], g["away"], 1), (g["away"], g["home"], 0)):
            t, o = ESPN_TO_NFLVERSE.get(team, team), ESPN_TO_NFLVERSE.get(opp, opp)
            for pid, p in latest[latest.team == t].iterrows():
                hist = df[df.player_id == pid]
                pos = p.position
                for mkey, (label, positions, (vcol, vmin)) in MARKETS.items():
                    if pos not in positions:
                        continue
                    vol = hist[vcol].tail(8).mean()
                    if not vol >= vmin:
                        continue
                    hl = hist.dropna(subset=[mkey]).tail(30)
                    if hl.empty:
                        continue
                    vals = hl[mkey].tolist()
                    est = float(int(hl[mkey].tail(10).mean())) + 0.5
                    if mkey in TD_MARKETS:
                        est = 0.5
                    if mkey in FIXED_LINES:
                        est = FIXED_LINES[mkey]
                    inj_status = status.get((pid, g["week"]))
                    if g["week"] > inj_week:
                        inj_status = None
                    row = {
                        "id": f"{pid}|{mkey}|{g['id']}", "pid": pid, "player": p.player_display_name,
                        "pos": pos, "team": t, "opp": o, "home": home, "game": g["id"],
                        "img": None if pd.isna(p.headshot_url) else p.headshot_url, "market": mkey, "label": label,
                        "est": est, "line": est, "src": "fixed" if mkey in FIXED_LINES else "est", "over": None, "under": None,
                        "books": [], "inj": inj_status,
                        "opp_rank": ranks.get((o, pos, mkey)),
                        "log": [game_entry(sched, r, mkey) for r in hl.itertuples()],
                        "vol": round(float(vol), 1),
                        "tier": tiers.get((pid, mkey, g["id"])),
                        "ev_over": None, "ev_under": None, "real_alts": [], "fair_line": None,
                        "zone_edge": zone_edge(pid, o, zones_player, zones_def) if mkey in ZONE_MARKETS else None,
                    }
                    books = odds["lines"].get(f"{norm_name(p.player_display_name)}|{mkey}")
                    if books:
                        books.sort(key=lambda b: BOOK_ORDER.index(b["book"])
                                   if b["book"] in BOOK_ORDER else 99)
                        row.update(line=books[0]["line"], src="book", over=books[0].get("over"),
                                   under=books[0].get("under"), books=books)

                    sg = sgo["lines"].get(f"{norm_name(p.player_display_name)}|{mkey}|{g['id']}")
                    if sg and sg["books"]:
                        line = sg["line"] if sg["line"] is not None else est
                        row.update(line=line, src="book", over=sg["over"], under=sg["under"], books=sg["books"])
                        # EV only where the book's own line is close enough to the fair line that comparing
                        # their odds head-to-head is a fair apples-to-apples read (a TD_MARKETS prop has no
                        # numeric line at all, so it's always comparable). A big gap means different props.
                        comparable = mkey in TD_MARKETS or (sg["fair_line"] is not None and abs(line - sg["fair_line"]) <= 1.0)
                        if comparable:
                            row["ev_over"] = ev_pct(sg["over"], sg["fair_over"])
                            row["ev_under"] = ev_pct(sg["under"], sg["fair_under"])
                        row["fair_line"] = sg["fair_line"]
                        row["real_alts"] = [{"t": a["t"], "over_odds": a.get("over", {}).get("odds"),
                                              "over_book": a.get("over", {}).get("book")}
                                             for a in sg["alts"] if a.get("over")]
                    rows.append(row)

    return {
        "season": SEASON, "games": games, "props": rows, "dvp": dvp, "recent": recent, "usage": usage,
        "roster_activity": roster_act, "zones_player": zones_player, "zones_def": zones_def, "zone_fit": zf,
        "has_key": bool(load_config().get("odds_key")),
        "odds_pulled": odds["pulled"], "odds_remaining": odds["remaining"],
        "has_sgo_key": bool(load_sgo_key()), "sgo_pulled": sgo["pulled"],
        "inj_week": inj_week, "built": time.time(),
    }


def get_board(force=False):
    with lock:
        if force or not _board_cache["data"] or time.time() - _board_cache["t"] > 300:
            _board_cache["data"] = build_board()
            _board_cache["t"] = time.time()
        return _board_cache["data"]


_cfb_lock = threading.Lock()
_cfb_cache = {"t": 0, "data": None}


def get_cfb_board(force=False):
    """NCAAF fetches ~250 individual game box scores (see cfb.py), so this is cached longer
    than the NFL board — cheap free data, but no reason to redo the work every 5 minutes."""
    import cfb
    with _cfb_lock:
        if force or not _cfb_cache["data"] or time.time() - _cfb_cache["t"] > 1800:
            _cfb_cache["data"] = cfb.build_board()
            _cfb_cache["t"] = time.time()
        return _cfb_cache["data"]


_nba_lock = threading.Lock()
_nba_cache = {"t": 0, "data": None}


def get_nba_board(force=False):
    """NBA history is a full prior season of individual game box scores (see nba.py) — cached even
    longer than CFB, since last season's games never change at all until this season starts
    producing its own (checked live: it hasn't yet), so there's nothing new to pick up in between."""
    import nba
    with _nba_lock:
        if force or not _nba_cache["data"] or time.time() - _nba_cache["t"] > 3600:
            _nba_cache["data"] = nba.build_board()
            _nba_cache["t"] = time.time()
        return _nba_cache["data"]


_nhl_lock = threading.Lock()
_nhl_cache = {"t": 0, "data": None}


def get_nhl_board(force=False):
    """Same reasoning as get_nba_board — a full prior season of box scores, cached for an hour since
    a rebuild still re-checks every calendar day's schedule even with every game itself cached."""
    import nhl
    with _nhl_lock:
        if force or not _nhl_cache["data"] or time.time() - _nhl_cache["t"] > 3600:
            _nhl_cache["data"] = nhl.build_board()
            _nhl_cache["t"] = time.time()
        return _nhl_cache["data"]


# ------------------------------------------------------------------ routes ---
@app.route("/")
def index():
    return render_template("index.html", public=False)


@app.route("/api/board")
def api_board():
    return jsonify(get_board())


@app.route("/api/board-ncaaf")
def api_board_ncaaf():
    return jsonify(get_cfb_board())


@app.route("/api/board-nba")
def api_board_nba():
    return jsonify(get_nba_board())


@app.route("/api/board-nhl")
def api_board_nhl():
    return jsonify(get_nhl_board())


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    for f in DATA.glob(f"*{SEASON}.parquet"):
        f.unlink()
    return jsonify(get_board(force=True))


@app.route("/api/settings", methods=["POST"])
def api_settings():
    key = (request.json or {}).get("odds_key", "").strip()
    cfg = load_config()
    cfg["odds_key"] = key
    save_config(cfg)
    return jsonify(ok=True, has_key=bool(key))


@app.route("/api/pull-odds", methods=["POST"])
def api_pull_odds():
    key = load_config().get("odds_key")
    if not key:
        return jsonify(error="Add your Odds API key first."), 400
    try:
        pull_odds(key)
    except Exception as e:
        return jsonify(error=f"Could not pull lines: {e}"), 502
    return jsonify(get_board(force=True))


@app.route("/api/pull-sgo", methods=["POST"])
def api_pull_sgo():
    key = load_sgo_key()
    if not key:
        return jsonify(error="No SportsGameOdds key found at data/sgo_key.txt."), 400
    try:
        sgo = pull_sgo(key, games=get_games())
    except Exception as e:
        return jsonify(error=f"Could not pull SportsGameOdds lines: {e}"), 502
    board = get_board(force=True)
    board["sgo_events_pulled"] = sgo["events"]
    return jsonify(board)


if __name__ == "__main__":
    print("PropBoard NFL running at http://127.0.0.1:5051")
    app.run(host="127.0.0.1", port=5051, debug=False)
