"""NCAAF (FBS) data pipeline for PropBoard.

Unlike the NFL side (which uses nflverse's clean pre-aggregated weekly-stats file), the closest
free CFB equivalent (cfbfastR-data's "player_stats" file) turned out to be a raw, one-stat-per-row
play export with an unreliable player<->play join for touchdown/yardage lines (verified by hand;
scoring-play rows don't reliably share a key with their yardage row). Rather than ship stats I
can't verify are correct, this pulls the same numbers ESPN.com's own box scores show: one request
per game via the public college-football "summary" endpoint. Heavier than the NFL pipeline (one
request per game instead of one big file), so completed games are cached to disk forever — a
finished game's box score never changes — and only new/upcoming games are re-fetched.

History spans this season-to-date PLUS all of last season (see PRIOR_SEASON_WEEKS below) — early in
a new season there aren't enough games yet to find real streaks or a stable defense read from this
season alone (nflverse-backed NFL and the NBA/NHL modules don't have that problem: they carry 30+
games or a full prior season by default). The one-time cost is a full season's worth of individual
box-score fetches (~700-800 games for FBS) the first time this runs; every run after that is cheap,
since finished games are cached to disk forever exactly like the current-season ones already were.
"""
import io
import json
import time
from pathlib import Path

import pandas as pd
import requests

BASE = Path(__file__).parent
DATA = BASE / "data" / "cfb"
GAMES_DIR = DATA / "games"
GAMES_DIR.mkdir(parents=True, exist_ok=True)

ESPN_HOSTS = ["site.api.espn.com", "site.web.api.espn.com"]   # 2nd host: site.api.* 403s from GH Actions
ESPN_PATH = "apis/site/v2/sports/football/college-football"
HTTP = {"User-Agent": "Mozilla/5.0 PropBoard"}
FBS = 80             # ESPN's "group" id for FBS (skips FCS-only games)
HIST_WEEKS = 6        # how many of the most recent completed weeks to build history from
PRIOR_SEASON_WEEKS = 16   # regular season only (through conf championships) -- bowl-season rosters/
                          # motivation are too different from the regular-season sample we want, same
                          # reasoning nba.py already uses to exclude playoffs from its own history

# Team ATS/O-U streaks (see cfb_team_streaks below) need real game-level results + closing lines,
# which this module's own ESPN box-score pipeline doesn't carry (that's player stats only). cfbfastR-
# data publishes both as plain files committed straight into the repo (not GitHub Releases), each a
# few MB -- small enough to just re-fetch whole on every build rather than caching to disk like the
# box scores above. Verified by hand before writing this: real spread/total/moneyline lines from
# multiple books back to 2006, actively maintained (updated within the last week as of writing).
# team_id in this dataset is ESPN's own numeric team id (confirmed against ESPN's logo CDN, which
# every row's own logo URL is keyed by) -- the same id ESPN's scoreboard exposes per competitor, so
# it's used as the join key back to upcoming_games() rather than matching by team name across the two
# datasets, which would be far more fragile ("Hawai'i" vs "Hawaii", mascot suffixes, etc).
CFBFASTR_RAW = "https://raw.githubusercontent.com/sportsdataverse/cfbfastR-data/main"
CFB_BOOK_ORDER = ["ESPN Bet", "DraftKings", "Draft Kings", "Bovada"]   # coverage-checked by hand; ESPN Bet has the most rows

MARKETS = {                          # label, positions (tuple), min recent volume
    "pass_yds": ("Pass Yds", ("QB",), 12),
    "pass_tds": ("Pass TDs", ("QB",), 12),
    "pass_att": ("Pass Attempts", ("QB",), 12),
    "pass_comp": ("Pass Completions", ("QB",), 12),
    "pass_int": ("Interceptions", ("QB",), 12),
    "rush_yds": ("Rush Yds", ("RB",), 6),
    "rush_att": ("Rush Attempts", ("RB",), 6),
    "long_rush": ("Longest Rush", ("RB",), 6),
    "rec_yds": ("Rec Yds", ("WR",), 3),
    "rec": ("Receptions", ("WR",), 3),
    "long_rec": ("Longest Reception", ("WR",), 3),
    "rush_rec_yds": ("Rush+Rec Yds", ("RB", "WR"), 4),
    "any_td": ("Anytime TD", ("RB", "WR"), 4),
}
VOL_COL = {"pass_yds": "pass_att", "pass_tds": "pass_att", "pass_att": "pass_att", "pass_comp": "pass_att",
           "pass_int": "pass_att", "rush_yds": "carries", "rush_att": "carries", "long_rush": "carries",
           "rec_yds": "rec", "rec": "rec", "long_rec": "rec", "rush_rec_yds": "touches", "any_td": "touches"}


# ------------------------------------------------------------------- ESPN ---
def _get(path, **params):
    """site.api.espn.com 403s from GitHub Actions runners (same block NFL hit); site.web.api.espn.com
    serves the identical response and isn't blocked there, so try that host second, not first —
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


def scoreboard(week=None, season=None):
    params = {"groups": FBS}
    if week:
        params.update(week=week, seasontype=2, dates=season)
    return _get("scoreboard", **params)


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


# --------------------------------------------------------------- box score ---
def parse_boxscore(summary):
    """One dict per (game, athlete), merging passing/rushing/receiving lines for dual-threat players."""
    bx = summary.get("boxscore") or {}
    header = summary.get("header", {})
    comp = (header.get("competitions") or [{}])[0]
    game_id = str(header.get("id"))
    date = comp.get("date")
    week = header.get("week")
    abbrs = {c["team"].get("abbreviation"): c["homeAway"] for c in comp.get("competitors", [])}
    players = {}
    for team_block in bx.get("players", []):
        team = team_block["team"].get("abbreviation")
        opp = next((a for a in abbrs if a != team), None)
        for cat in team_block.get("statistics", []):
            name = cat.get("name")
            if name not in ("passing", "rushing", "receiving"):
                continue
            for ath in cat.get("athletes", []):
                a = ath.get("athlete") or {}
                aid = a.get("id")
                if not aid:
                    continue
                row = players.setdefault((game_id, aid), {
                    "game_id": game_id, "date": date, "week": week, "team": team, "opp": opp,
                    "athlete_id": aid, "name": a.get("displayName"),
                    "headshot": (a.get("headshot") or {}).get("href"),
                    "pass_att": 0, "pass_comp": 0, "pass_yds": 0.0, "pass_tds": 0.0, "pass_int": 0.0,
                    "carries": 0, "rush_yds": 0.0, "rush_tds": 0.0, "long_rush": 0.0,
                    "rec": 0, "rec_yds": 0.0, "rec_tds": 0.0, "long_rec": 0.0, "cats": set(),
                })
                row["cats"].add(name)
                s = ath.get("stats") or []
                # labels confirmed against a real live box score before writing this (not assumed):
                # passing = [C/ATT, YDS, AVG, TD, INT, QBR]; rushing/receiving = [CAR/REC, YDS, AVG, TD, LONG]
                try:
                    if name == "passing" and len(s) >= 5:
                        att = s[0].split("/")
                        row["pass_comp"] = int(att[0]) if len(att) == 2 else 0
                        row["pass_att"] = int(att[1]) if len(att) == 2 else 0
                        row["pass_yds"] = float(s[1] or 0)
                        row["pass_tds"] = float(s[3] or 0)
                        row["pass_int"] = float(s[4] or 0)
                    elif name == "rushing" and len(s) >= 4:
                        row["carries"] = float(s[0] or 0)
                        row["rush_yds"] = float(s[1] or 0)
                        row["rush_tds"] = float(s[3] or 0)
                        row["long_rush"] = float(s[4] or 0) if len(s) >= 5 else 0.0
                    elif name == "receiving" and len(s) >= 4:
                        row["rec"] = float(s[0] or 0)
                        row["rec_yds"] = float(s[1] or 0)
                        row["rec_tds"] = float(s[3] or 0)
                        row["long_rec"] = float(s[4] or 0) if len(s) >= 5 else 0.0
                except (ValueError, IndexError):
                    continue
    out = list(players.values())
    for r in out:
        # position isn't in ESPN's box score; infer from whichever stat line dominates
        r["pos"] = "QB" if "passing" in r["cats"] and r["pass_att"] >= max(r["carries"], r["rec"]) \
            else "RB" if r["carries"] >= r["rec"] else "WR"
        del r["cats"]
    return out


def load_history(season, current_week, max_weeks=HIST_WEEKS):
    """Every FBS box score from the last `max_weeks` completed weeks of `season` (cached forever per
    game). Tags each row with `season` explicitly -- once build_board() concatenates this with a prior
    season's full history, nothing downstream can tell them apart by week number alone (week 3 of a new
    season and week 3 of last season are both just "3")."""
    rows = []
    start = max(1, current_week - max_weeks)
    for wk in range(start, current_week):
        try:
            sb = scoreboard(week=wk, season=season)
        except Exception as e:
            print(f"week {wk} scoreboard failed: {e}")
            continue
        ids = [e["id"] for e in sb.get("events", [])
               if e["competitions"][0]["status"]["type"]["state"] == "post"]
        print(f"  week {wk}: {len(ids)} completed games")
        for gid in ids:
            s = game_summary(gid)
            if s:
                for row in parse_boxscore(s):
                    row["season"] = season
                    rows.append(row)
            time.sleep(0.05)
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ board ---
def ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def upcoming_games():
    cur = scoreboard()
    week, season = cur["week"]["number"], cur["season"]["year"]
    games, seen = [], set()
    for wk, page in ((week, cur), (week + 1, None)):
        data = page or scoreboard(week=wk, season=season)
        for e in data.get("events", []):
            comp = e["competitions"][0]
            if comp["status"]["type"]["state"] == "post" or e["id"] in seen:
                continue
            seen.add(e["id"])
            home = next(c for c in comp["competitors"] if c["homeAway"] == "home")
            away = next(c for c in comp["competitors"] if c["homeAway"] == "away")
            odds = (comp.get("odds") or [{}])[0]
            games.append({
                "id": e["id"], "week": wk, "state": comp["status"]["type"]["state"], "date": e["date"],
                "home": home["team"].get("abbreviation"), "away": away["team"].get("abbreviation"),
                "home_name": home["team"].get("displayName"), "away_name": away["team"].get("displayName"),
                "home_logo": home["team"].get("logo"), "away_logo": away["team"].get("logo"),
                "spread": odds.get("details"), "total": odds.get("overUnder"),
                "home_spread": odds.get("spread"),
                # ESPN's own numeric team id -- only used to join against cfb_team_streaks' history
                # (see CFBFASTR_RAW above), not shown anywhere or used for the box-score pipeline.
                "home_id": home["team"].get("id"), "away_id": away["team"].get("id"),
            })
    games.sort(key=lambda g: g["date"])
    return games, week, season


def defense_ranks(df, fbs_teams):
    """Rank FBS defenses only — leaving FCS one-off opponents in would blow the rank scale
    past the real ~130-team FBS pool (an FCS team that shows up in one game skews its own
    'average allowed' wildly, and there's no benefit to ranking a defense nobody has props on)."""
    ranks, dvp = {}, {}
    n = len(fbs_teams)
    for mkey, (_, positions, _) in MARKETS.items():
        for pos in positions:
            sub = df[(df.pos == pos) & (df.opp.isin(fbs_teams))]
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


def load_cfb_betting_history(seasons):
    """Real per-team ATS/O-U results for `seasons`, from cfbfastR-data's schedules (final scores)
    joined with its betting lines (closing spread/total, one row per game picked via CFB_BOOK_ORDER).
    Returns a DataFrame with one row per team per game -- team_id, opp, season, week, fav (bool: was
    this team favored), ats ('W'/'L'/None), ou ('O'/'U'/None) -- shaped like app.py's own
    team_situational_streaks() history, but built from a completely different data source since CFB
    has no nflverse-equivalent file with results+lines already joined."""
    frames = []
    for yr in seasons:
        try:
            r = requests.get(f"{CFBFASTR_RAW}/schedules/csv/cfb_schedules_{yr}.csv", timeout=30)
            r.raise_for_status()
            frames.append(pd.read_csv(io.StringIO(r.text)))
        except Exception as e:
            print(f"cfbfastR schedules {yr} failed: {e}")
    if not frames:
        return pd.DataFrame()
    sched = pd.concat(frames, ignore_index=True)
    sched = sched[(sched.completed == True) & sched.home_points.notna() & sched.away_points.notna()].copy()
    for c in ("game_id", "home_id", "away_id"):
        sched[c] = sched[c].astype("int64")

    try:
        r = requests.get(f"{CFBFASTR_RAW}/betting/csv/cfb_line_odds.csv.gz", timeout=60)
        r.raise_for_status()
        bet = pd.read_csv(io.BytesIO(r.content), compression="gzip")
    except Exception as e:
        print(f"cfbfastR betting lines failed: {e}")
        return pd.DataFrame()
    bet = bet[bet.season.isin(seasons)].copy()
    if bet.empty:
        # Real gap, not a bug: the betting file has historically lagged a season behind the
        # schedules file (verified by hand -- the current season had zero rows in it when this was
        # written). Team streaks for that season just won't exist yet; nothing downstream crashes on
        # an empty history, same soft-degradation as everywhere else in this codebase.
        return pd.DataFrame()
    bet["game_id"] = bet.game_id.astype("int64")
    bet["book_rank"] = bet.book.map({b: i for i, b in enumerate(CFB_BOOK_ORDER)}).fillna(99)

    spread = bet[bet.market_type == "spread"].merge(
        sched[["game_id", "home_team"]], on="game_id", how="inner")
    spread = spread[spread.abbr == spread.home_team]      # the home side's own spread row -- safe to
    # name-match here (unlike the ESPN join above) since both sides come from this same cfbfastR game record
    spread = spread.sort_values("book_rank").groupby("game_id").first().reset_index()
    spread = spread[["game_id", "lines"]].rename(columns={"lines": "home_spread"})

    total = bet[(bet.market_type == "total") & (bet.abbr == "over")]
    total = total.sort_values("book_rank").groupby("game_id").first().reset_index()
    total = total[["game_id", "lines"]].rename(columns={"lines": "total_line"})

    g = sched.merge(spread, on="game_id", how="inner").merge(total, on="game_id", how="left")
    if g.empty:
        return pd.DataFrame()

    rows = []
    for r in g.itertuples():
        margin = r.home_points - r.away_points
        cover = margin + r.home_spread     # >0 home covered, <0 away covered, 0 push (negative home_spread = home favored, same sign convention ESPN's live odds use)
        total_pts = r.home_points + r.away_points
        ou = None if pd.isna(r.total_line) or total_pts == r.total_line else ("O" if total_pts > r.total_line else "U")
        rows.append({"team_id": r.home_id, "opp": r.away_team, "season": int(r.season), "week": int(r.week),
                      "fav": r.home_spread < 0, "ats": None if cover == 0 else ("W" if cover > 0 else "L"), "ou": ou})
        rows.append({"team_id": r.away_id, "opp": r.home_team, "season": int(r.season), "week": int(r.week),
                      "fav": r.home_spread > 0, "ats": None if cover == 0 else ("W" if cover < 0 else "L"), "ou": ou})
    return pd.DataFrame(rows).sort_values(["team_id", "season", "week"])


def cfb_team_streaks(games, hist):
    """Same algorithm and output shape as app.py's team_situational_streaks(), so the frontend's
    Insights feed needs zero changes to pick this up. Joins to `hist` by ESPN team id (see
    upcoming_games()), and only ever outputs the upcoming game's own ESPN team codes -- hist's
    cfbfastR-sourced team/opp names never leak into the result."""
    if hist.empty:
        return []

    def streak(vals):
        vals = [v for v in vals if v][-10:]
        n = len(vals)
        if n < 5:
            return None
        w, l = vals.count("W"), vals.count("L")
        if w and w / n >= 0.8:
            return w, n, "W"
        if l and l / n >= 0.8:
            return l, n, "L"
        return None

    out = []
    for gm in games:
        for side, team_espn, opp_espn, team_id in (
            ("home", gm["home"], gm["away"], gm.get("home_id")),
            ("away", gm["away"], gm["home"], gm.get("away_id")),
        ):
            hs = gm.get("home_spread")
            if hs is None or team_id is None:
                continue
            fav = (hs < 0) if side == "home" else (hs > 0)
            grp = hist[(hist.team_id == team_id) & (hist.fav == fav)]
            article = "an" if side == "away" else "a"
            role = f"{article} {side} {'favorite' if fav else 'underdog'}"

            ats = streak(grp.ats.tolist())
            if ats:
                hits, n, d = ats
                verb = "covered the spread" if d == "W" else "failed to cover the spread"
                straight = " straight" if hits == n else ""
                recent = grp[grp.ats.notna()].tail(n)
                out.append({
                    "kind": "ats", "team": team_espn, "opp": opp_espn, "game": gm["id"], "dir": d,
                    "text": f"{team_espn} {verb} in {hits} of their last {n}{straight} games as {role}.",
                    "hits": hits, "n": n,
                    "games": [[x.season, x.week, x.opp, x.ats] for x in recent.itertuples()],
                })
            ou_vals = ["W" if v == "O" else "L" if v == "U" else None for v in grp.ou.tolist()]
            ou = streak(ou_vals)
            if ou:
                hits, n, d = ou
                word = "over" if d == "W" else "under"
                straight = " straight" if hits == n else ""
                recent = grp[grp.ou.notna()].tail(n)
                out.append({
                    "kind": "ou", "team": team_espn, "opp": opp_espn, "game": gm["id"], "dir": word[0].upper(),
                    "text": f"The {word} has hit in {hits} of {team_espn}'s last {n}{straight} games as {role}.",
                    "hits": hits, "n": n,
                    "games": [[x.season, x.week, x.opp, x.ou] for x in recent.itertuples()],
                })
    return out


def build_board():
    print("NCAAF: fetching schedule...")
    games, week, season = upcoming_games()
    print("NCAAF: fetching team ATS/O-U history (cfbfastR schedules + betting lines)...")
    try:
        team_hist = load_cfb_betting_history([season, season - 1])
        team_streaks = cfb_team_streaks(games, team_hist)
    except Exception as e:
        print(f"NCAAF: team streaks failed, leaving them empty: {e}")
        team_streaks = []
    print(f"NCAAF: week {week}/{season}, {len(games)} upcoming games. Fetching this season's history...")
    df = load_history(season, week)
    # Last season's full regular season, on top of this season's rolling window -- see the module
    # docstring for why: this early in a new season there aren't enough games yet on their own to find
    # real streaks or a stable defense read. Cached forever per game (same as the current-season fetch
    # above), so this is a one-time cost the first time this runs, not a recurring one.
    print(f"NCAAF: fetching {season - 1}'s full regular season for history depth...")
    prior = load_history(season - 1, PRIOR_SEASON_WEEKS + 1, max_weeks=PRIOR_SEASON_WEEKS)
    df = pd.concat([prior, df], ignore_index=True)
    if df.empty:
        print("NCAAF: no history rows, skipping.")
        return {"season": season, "games": games, "props": [], "dvp": {}, "recent": {},
                "team_streaks": team_streaks, "built": time.time()}
    df["rush_rec_yds"] = df.rush_yds + df.rec_yds
    df["touches"] = df.carries + df.rec
    df["any_td"] = df.rush_tds + df.rec_tds
    df["rush_att"] = df.carries      # alias: rush_att is a market key, carries is the raw column name
    # season first, then week+date -- week numbers reset every season (week 3 of 2025 and week 3 of
    # 2026 are both just "3"), so sorting by week alone would interleave the two seasons out of order.
    df = df.sort_values(["season", "week", "date"]).reset_index(drop=True)
    # FBS team set: real FBS teams play (almost) every week, so a team seen in most of the fetched
    # weeks is FBS; a one-off FCS/cupcake opponent only ever shows up once or twice. A raw scoreboard
    # event still lists an FCS opponent by name (ESPN's groups=80 filter only guarantees one side is
    # FBS), so "every team seen in any game" would wrongly pull those into the ranking pool.
    counts = df.groupby("team").game_id.nunique()
    fbs_teams = set(counts[counts >= min(3, df.week.nunique())].index)
    ranks, dvp = defense_ranks(df, fbs_teams)

    latest = df.sort_values(["season", "week", "date"]).groupby("athlete_id").tail(1).set_index("athlete_id")
    rows = []
    for g in games:
        for team, opp in ((g["home"], g["away"]), (g["away"], g["home"])):
            cand = latest[latest.team == team]
            for aid, p in cand.iterrows():
                hist = df[(df.athlete_id == aid) & (df.team == team)]
                pos = p.pos
                for mkey, (label, positions, vmin) in MARKETS.items():
                    if pos not in positions:
                        continue
                    vol = hist[VOL_COL[mkey]].tail(6).mean()
                    if not vol >= vmin:
                        continue
                    hl = hist.tail(20)
                    if hl.empty:
                        continue
                    est = float(int(hl[mkey].tail(6).mean())) + 0.5
                    rk = ranks.get((opp, pos, mkey))
                    rows.append({
                        "id": f"{aid}|{mkey}|{g['id']}", "pid": aid, "player": p["name"],
                        "pos": pos, "team": team, "opp": opp, "home": 1 if team == g["home"] else 0,
                        "game": g["id"], "img": p.headshot if isinstance(p.headshot, str) else None,
                        "market": mkey, "label": label,
                        "est": est, "line": est, "src": "est", "over": None, "under": None,
                        "books": [], "inj": None, "opp_rank": rk,
                        "log": [[int(r.season), int(r.week), r.opp, float(getattr(r, mkey)), r.date[:10],
                                 1 if r.team == g["home"] else (0 if r.team == g["away"] else None),
                                 None, None] for r in hl.itertuples()],
                        "vol": round(float(vol), 1),
                    })

    recent = {}
    for mkey, (_, positions, _) in MARKETS.items():
        for pos in positions:
            sub = df[df.pos == pos]
            for def_team, grp in sub.groupby("opp"):
                byweek = (grp.groupby(["game_id", "season", "week"]).agg(total=(mkey, "sum"))
                          .reset_index().sort_values(["season", "week"]))
                entries = []
                for _, row in byweek.tail(5).iterrows():
                    game_rows = grp[grp.game_id == row.game_id]
                    off_team = game_rows.team.iloc[0] if len(game_rows) else ""
                    top = game_rows.loc[game_rows[mkey].idxmax()] if len(game_rows) else None
                    entries.append([int(row.season), int(row.week), off_team, round(float(row.total), 1),
                                     top["name"] if top is not None else "",
                                     round(float(top[mkey]), 1) if top is not None else 0])
                recent[f"{def_team}|{pos}|{mkey}"] = entries

    board = {"season": season, "games": games, "props": rows, "dvp": dvp, "recent": recent,
             "team_streaks": team_streaks,
             "inj_week": 0, "built": time.time(), "has_key": False, "odds_pulled": None, "odds_remaining": None}
    return _clean_nans(board)


def _clean_nans(obj):
    """A stray pandas NaN anywhere in here (e.g. a missing headshot/name) breaks strict JSON encoding
    downstream — final safety net rather than chasing each source field by hand."""
    if isinstance(obj, float) and obj != obj:          # NaN != NaN is the cheapest NaN check
        return None
    if isinstance(obj, dict):
        return {k: _clean_nans(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_nans(v) for v in obj]
    return obj


if __name__ == "__main__":
    b = build_board()
    print(f"games={len(b['games'])} props={len(b['props'])}")
