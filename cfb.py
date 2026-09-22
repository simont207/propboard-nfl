"""NCAAF (FBS) data pipeline for PropBoard.

Unlike the NFL side (which uses nflverse's clean pre-aggregated weekly-stats file), the closest
free CFB equivalent (cfbfastR-data's "player_stats" file) turned out to be a raw, one-stat-per-row
play export with an unreliable player<->play join for touchdown/yardage lines (verified by hand;
scoring-play rows don't reliably share a key with their yardage row). Rather than ship stats I
can't verify are correct, this pulls the same numbers ESPN.com's own box scores show: one request
per game via the public college-football "summary" endpoint. Heavier than the NFL pipeline (one
request per game instead of one big file), so completed games are cached to disk forever — a
finished game's box score never changes — and only new/upcoming games are re-fetched.
"""
import json
import time
from pathlib import Path

import pandas as pd
import requests

BASE = Path(__file__).parent
DATA = BASE / "data" / "cfb"
GAMES_DIR = DATA / "games"
GAMES_DIR.mkdir(parents=True, exist_ok=True)

ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/college-football"
HTTP = {"User-Agent": "Mozilla/5.0 PropBoard"}
FBS = 80             # ESPN's "group" id for FBS (skips FCS-only games)
HIST_WEEKS = 6        # how many of the most recent completed weeks to build history from

MARKETS = {
    "pass_yds": ("Pass Yds", "QB", 12),
    "pass_tds": ("Pass TDs", "QB", 12),
    "rush_yds": ("Rush Yds", "RB", 6),
    "rec_yds": ("Rec Yds", "WR", 3),
    "rec": ("Receptions", "WR", 3),
}


# ------------------------------------------------------------------- ESPN ---
def _get(path, **params):
    r = requests.get(f"{ESPN}/{path}", params=params, headers=HTTP, timeout=25)
    r.raise_for_status()
    return r.json()


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
                    "pass_att": 0, "pass_yds": 0.0, "pass_tds": 0.0,
                    "carries": 0, "rush_yds": 0.0, "rush_tds": 0.0,
                    "rec": 0, "rec_yds": 0.0, "rec_tds": 0.0, "cats": set(),
                })
                row["cats"].add(name)
                s = ath.get("stats") or []
                try:
                    if name == "passing" and len(s) >= 5:
                        att = s[0].split("/")
                        row["pass_att"] = int(att[1]) if len(att) == 2 else 0
                        row["pass_yds"] = float(s[1] or 0)
                        row["pass_tds"] = float(s[3] or 0)
                    elif name == "rushing" and len(s) >= 4:
                        row["carries"] = float(s[0] or 0)
                        row["rush_yds"] = float(s[1] or 0)
                        row["rush_tds"] = float(s[3] or 0)
                    elif name == "receiving" and len(s) >= 4:
                        row["rec"] = float(s[0] or 0)
                        row["rec_yds"] = float(s[1] or 0)
                        row["rec_tds"] = float(s[3] or 0)
                except (ValueError, IndexError):
                    continue
    out = list(players.values())
    for r in out:
        # position isn't in ESPN's box score; infer from whichever stat line dominates
        r["pos"] = "QB" if "passing" in r["cats"] and r["pass_att"] >= max(r["carries"], r["rec"]) \
            else "RB" if r["carries"] >= r["rec"] else "WR"
        del r["cats"]
    return out


def load_history(season, current_week):
    """Every FBS box score from the last HIST_WEEKS completed weeks (cached forever per game)."""
    rows = []
    start = max(1, current_week - HIST_WEEKS)
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
                rows.extend(parse_boxscore(s))
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
            })
    games.sort(key=lambda g: g["date"])
    return games, week, season


def defense_ranks(df, fbs_teams):
    """Rank FBS defenses only — leaving FCS one-off opponents in would blow the rank scale
    past the real ~130-team FBS pool (an FCS team that shows up in one game skews its own
    'average allowed' wildly, and there's no benefit to ranking a defense nobody has props on)."""
    ranks, dvp = {}, {}
    n = len(fbs_teams)
    for mkey, (_, pos, _) in MARKETS.items():
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


def build_board():
    print("NCAAF: fetching schedule...")
    games, week, season = upcoming_games()
    print(f"NCAAF: week {week}/{season}, {len(games)} upcoming games. Fetching history...")
    df = load_history(season, week)
    if df.empty:
        print("NCAAF: no history rows, skipping.")
        return {"season": season, "games": games, "props": [], "dvp": {}, "recent": {}, "built": time.time()}
    df["rush_rec_yds"] = df.rush_yds + df.rec_yds
    df = df.sort_values(["week", "date"]).reset_index(drop=True)
    # FBS team set: real FBS teams play (almost) every week, so a team seen in most of the fetched
    # weeks is FBS; a one-off FCS/cupcake opponent only ever shows up once or twice. A raw scoreboard
    # event still lists an FCS opponent by name (ESPN's groups=80 filter only guarantees one side is
    # FBS), so "every team seen in any game" would wrongly pull those into the ranking pool.
    counts = df.groupby("team").game_id.nunique()
    fbs_teams = set(counts[counts >= min(3, df.week.nunique())].index)
    ranks, dvp = defense_ranks(df, fbs_teams)

    latest = df.sort_values("week").groupby("athlete_id").tail(1).set_index("athlete_id")
    rows = []
    for g in games:
        for team, opp in ((g["home"], g["away"]), (g["away"], g["home"])):
            cand = latest[latest.team == team]
            for aid, p in cand.iterrows():
                hist = df[(df.athlete_id == aid) & (df.team == team)]
                pos = p.pos
                for mkey, (label, mpos, vmin) in MARKETS.items():
                    if pos != mpos:
                        continue
                    vol_col = {"pass_yds": "pass_att", "pass_tds": "pass_att", "rush_yds": "carries",
                               "rec_yds": "rec", "rec": "rec"}[mkey]
                    vol = hist[vol_col].tail(6).mean()
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
                        "log": [[season, int(r.week), r.opp, float(getattr(r, mkey)), r.date[:10],
                                 1 if r.team == g["home"] else (0 if r.team == g["away"] else None),
                                 None, None] for r in hl.itertuples()],
                        "vol": round(float(vol), 1),
                    })

    recent = {}
    for mkey, (_, pos, _) in MARKETS.items():
        sub = df[df.pos == pos]
        for def_team, grp in sub.groupby("opp"):
            byweek = grp.groupby(["game_id", "week"]).agg(total=(mkey, "sum")).reset_index().sort_values("week")
            entries = []
            for _, row in byweek.tail(5).iterrows():
                game_rows = grp[grp.game_id == row.game_id]
                off_team = game_rows.team.iloc[0] if len(game_rows) else ""
                top = game_rows.loc[game_rows[mkey].idxmax()] if len(game_rows) else None
                entries.append([season, int(row.week), off_team, round(float(row.total), 1),
                                 top["name"] if top is not None else "",
                                 round(float(top[mkey]), 1) if top is not None else 0])
            recent[f"{def_team}|{pos}|{mkey}"] = entries

    board = {"season": season, "games": games, "props": rows, "dvp": dvp, "recent": recent,
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
