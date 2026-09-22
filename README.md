# PropBoard NFL

A props.cash-style NFL player prop finder that runs on your Mac.

**Start it:** double-click `start.command`. Your browser opens to http://127.0.0.1:5051.
Leave the black Terminal window open while you use it; close it to stop the site.

## NFL + NCAAF
A **NFL / NCAAF** switch in the header swaps the whole site to the other sport — same table,
player pages, game pages, search and parlay slip, each sport with its own separate slip and
custom lines. NCAAF covers FBS only, sourced from ESPN's scoreboard (schedule/lines) and its
per-game box scores (player stats — see `cfb.py`'s docstring for why, not nflverse's play-level
export, which turned out too unreliable to hand-aggregate). No Q1 markets, sharpness tiers or
sportsbook odds for NCAAF yet — just the core props.

## What it does (NFL)
- Every upcoming game (this week + next) with spread and total, from ESPN.
- Player props: Rec Yds, Receptions, Rush Yds, Rush+Rec Yds, Pass Yds, Pass TDs, Anytime TD.
- **Q1 Rec 5+ / Q1 Rush 5+**: did the player get 5+ yards in the first quarter? (fixed line 4.5, from
  play-by-play data). Same hit-rate windows, matchup rank and filters as every other prop.
- **Sharpness tiers** on the Q1 tabs (💎 ELITE ≥ .80, 🔥 STRONG ≥ .72, ⭐ LEAN ≥ .63), ported from `~/nfl-q1-bot`.
  Only each team's top WR (Q1 Rec) and top RB (Q1 Rush) are scored. Click a player for the full breakdown.
  The opponent-defense term is flipped vs the original engine (see `FIX_EPA_RANK_DIRECTION` in `app.py`).
- **Game page** (click any game): every player's alt lines he cleared in each of his last 3/5/10 games,
  plus a **parlay slip** (add/swap/delete legs, optional odds -> payout, saved in your browser).
- **Search bar** in the header (or press `/`): type a player, team or prop name, use arrow keys + Enter.
- Hit rate over the Last 5 / Last 10 / This season / vs this opponent, for Over or Under.
- Opponent rank (how much the defense allows to that position).
- Click a player for a full player page (modeled on props.cash): everyone in that game in a left-hand list,
  prop tabs, a big bar chart, window tabs (2026 / 2025 / H2H / L5 / L10 / L20 / L30), chart filters
  (similar-DvP, Home/Away, Fav/Dog, game total), Matchup Insights and an Odds panel.
- Matchup rank + letter grade (A+ to F): how much the opponent's defense allows to that position per game
  (last 17 games, this + last season). 1st = allows the most = best for Overs.
- Type your own line in any Line box to test it (saved in your browser).
- Injury tags from the nflverse report (only shown once that week's report is out).

## Lines
By default lines are **estimates** (the player's recent average, rounded to x.5), marked `EST`.
For real FanDuel/DraftKings lines, click **Sportsbook lines**, paste a free
[The Odds API](https://the-odds-api.com) key, and click **Pull lines now**.
A pull costs ~110 of the free 500 monthly credits, so it never runs automatically.

## Files
- `app.py` – the server (downloads stats, builds the board)
- `templates/index.html` – the page
- `data/` – cached downloads (safe to delete)

## Photos and logos
Player photos and team logos load straight from ESPN / NFL.com (never copied onto this site). If one fails to load
you see initials or a team-code badge instead. To turn **all** photos and logos off, set `SHOW_IMAGES = false`
in `templates/index.html` (search for `SHOW_IMAGES`). Adding `?images=off` to the address does it for one visit.
A footer notes the site is independent/not affiliated, credits nflverse/ESPN/The Odds API, and carries a 21+ / 1-800-GAMBLER notice.

## Public copy (GitHub Pages)
`python build_static.py` writes a static copy into `docs/` (data in `board.json`, page in "public" mode with no refresh
or API-key buttons). `.github/workflows/publish.yml` runs it every 3 hours on GitHub and publishes the result, so the
public site needs no server and visitors never use your Odds API credits. `data/` (which holds any saved API key) and
`docs/` are git-ignored: the key is never committed.
