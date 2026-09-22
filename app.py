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


def _build_q1_tables():
    """Reduce big play-by-play files to small first-quarter tables (cached, big file deleted)."""
    for yr, age in ((SEASON - 1, 24 * 30), (SEASON, 3)):
        small, small_def = DATA / f"q1_{yr}.parquet", DATA / f"q1def_{yr}.parquet"
        fresh = all(f.exists() and time.time() - f.stat().st_mtime < age * 3600 for f in (small, small_def))
        if fresh:
            continue
        big = DATA / f"pbp_{yr}.parquet"
        if not download(f"{NFLVERSE}/pbp/play_by_play_{yr}.parquet", big, 0):
            continue
        p = pd.read_parquet(big, columns=[
            "game_id", "qtr", "play_type", "epa", "defteam", "receiver_player_id", "receiving_yards",
            "rusher_player_id", "rushing_yards"])
        p = p[p.qtr == 1]
        rec = (p.dropna(subset=["receiver_player_id"])
               .groupby(["game_id", "receiver_player_id"]).receiving_yards.sum()
               .rename("q1_rec").rename_axis(["game_id", "player_id"]))
        rush = (p.dropna(subset=["rusher_player_id"])
                .groupby(["game_id", "rusher_player_id"]).rushing_yards.sum()
                .rename("q1_rush").rename_axis(["game_id", "player_id"]))
        pd.concat([rec, rush], axis=1).reset_index().to_parquet(small)
        d = p[p.epa.notna() & p.defteam.notna() & p.play_type.isin(["pass", "run"])]
        (d.groupby(["defteam", "play_type"]).epa.agg(["sum", "count"]).reset_index()
         .to_parquet(small_def))
        big.unlink()


def _read_all(prefix):
    frames = [pd.read_parquet(f) for yr in (SEASON - 1, SEASON)
              if (f := DATA / f"{prefix}_{yr}.parquet").exists()]
    return pd.concat(frames, ignore_index=True) if frames else None


def load_q1():
    """First-quarter receiving/rushing yards per player per game, from play-by-play."""
    _build_q1_tables()
    q1 = _read_all("q1")
    return q1 if q1 is not None else pd.DataFrame(columns=["game_id", "player_id", "q1_rec", "q1_rush"])


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
    df = df[df.position.isin(["QB", "RB", "FB", "WR", "TE"])].copy()
    df["position"] = df.position.replace({"FB": "RB"})
    df["pass_yds"] = df.passing_yards
    df["pass_tds"] = df.passing_tds
    df["rush_yds"] = df.rushing_yards
    df["rec_yds"] = df.receiving_yards
    df["rec"] = df.receptions
    df["rush_rec_yds"] = df.rushing_yards + df.receiving_yards
    df["any_td"] = df.rushing_tds + df.receiving_tds
    df["touches"] = df.carries + df.targets
    q1 = load_q1()
    df = df.merge(q1, on=["game_id", "player_id"], how="left")
    known = df.game_id.isin(set(q1.game_id))          # games we have play-by-play for
    df["q1_rec_tgt"] = df.q1_rec.notna() & known      # had at least one Q1 target / carry
    df["q1_rush_att"] = df.q1_rush.notna() & known
    df.loc[known, ["q1_rec", "q1_rush"]] = df.loc[known, ["q1_rec", "q1_rush"]].fillna(0)
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
            sub = df[df.position == pos].sort_values(mkey, ascending=False)
            per_game = (sub.groupby(["opponent_team", "season", "week"])
                        .agg(total=(mkey, "sum"), top=(mkey, "max"),
                             name=("player_display_name", "first"), off=("team", "first"))
                        .reset_index().sort_values(["season", "week"]))
            last = per_game.groupby("opponent_team").tail(17)
            avg = last.groupby("opponent_team").total.mean()
            rk = avg.rank(ascending=False, method="min").astype(int)
            dvp[f"{pos}|{mkey}"] = {
                "league": round(float(avg.mean()), 2),
                "teams": {t: [int(rk[t]), round(float(avg[t]), 2)] for t in avg.index},
            }
            for team, r in rk.items():
                ranks[(team, pos, mkey)] = int(r)
            for team, grp in per_game.groupby("opponent_team"):
                recent[f"{team}|{pos}|{mkey}"] = [
                    [int(x.season), int(x.week), x.off, round(float(x.total), 1), x.name, round(float(x.top), 1)]
                    for x in grp.tail(5).itertuples()]
    return ranks, dvp, recent


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
    ranks, dvp, recent = defense_ranks(df)
    tiers = q1_tiers(df, games, inj_latest)
    sched = load_schedule()

    status = {}
    if len(inj):
        for _, r in inj.iterrows():
            status[(r.gsis_id, int(r.week))] = r.report_status

    cur = df[df.season == SEASON]
    latest = cur.sort_values("week").groupby("player_id").tail(1).set_index("player_id")

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
                    if mkey == "any_td":
                        est = 0.5
                    if mkey in FIXED_LINES:
                        est = FIXED_LINES[mkey]
                    inj_status = status.get((pid, g["week"]))
                    if g["week"] > inj_week:
                        inj_status = None
                    row = {
                        "id": f"{pid}|{mkey}|{g['id']}", "pid": pid, "player": p.player_display_name,
                        "pos": pos, "team": t, "opp": o, "home": home, "game": g["id"],
                        "img": p.headshot_url, "market": mkey, "label": label,
                        "est": est, "line": est, "src": "fixed" if mkey in FIXED_LINES else "est", "over": None, "under": None,
                        "books": [], "inj": inj_status,
                        "opp_rank": ranks.get((o, pos, mkey)),
                        "log": [game_entry(sched, r, mkey) for r in hl.itertuples()],
                        "vol": round(float(vol), 1),
                        "tier": tiers.get((pid, mkey, g["id"])),
                    }
                    books = odds["lines"].get(f"{norm_name(p.player_display_name)}|{mkey}")
                    if books:
                        books.sort(key=lambda b: BOOK_ORDER.index(b["book"])
                                   if b["book"] in BOOK_ORDER else 99)
                        row.update(line=books[0]["line"], src="book", over=books[0].get("over"),
                                   under=books[0].get("under"), books=books)
                    rows.append(row)

    return {
        "season": SEASON, "games": games, "props": rows, "dvp": dvp, "recent": recent,
        "has_key": bool(load_config().get("odds_key")),
        "odds_pulled": odds["pulled"], "odds_remaining": odds["remaining"],
        "inj_week": inj_week, "built": time.time(),
    }


def get_board(force=False):
    with lock:
        if force or not _board_cache["data"] or time.time() - _board_cache["t"] > 300:
            _board_cache["data"] = build_board()
            _board_cache["t"] = time.time()
        return _board_cache["data"]


# ------------------------------------------------------------------ routes ---
@app.route("/")
def index():
    return render_template("index.html", public=False)


@app.route("/api/board")
def api_board():
    return jsonify(get_board())


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


if __name__ == "__main__":
    print("PropBoard NFL running at http://127.0.0.1:5051")
    app.run(host="127.0.0.1", port=5051, debug=False)
