"""Build the public, static copy of PropBoard into ./docs.

    python build_static.py

Writes docs/board.json (all the data) and docs/index.html (the page in "public" mode: no refresh or
API-key buttons). Host the docs folder anywhere. If the data looks broken it exits with an error and
writes nothing, so a bad run can never replace a good site.
"""
import datetime
import json
import os
import sys
from pathlib import Path

import app

out = Path(__file__).parent / "docs"

# Optional expiry: if TAKEDOWN_AT (e.g. 2026-09-23T03:00:00Z) has passed, publish an "offline" page instead
# of the site. Remove the TAKEDOWN_AT line in .github/workflows/publish.yml to bring the site back.
takedown = os.environ.get("TAKEDOWN_AT", "").strip()
if takedown and datetime.datetime.now(datetime.timezone.utc) >= datetime.datetime.fromisoformat(takedown.replace("Z", "+00:00")):
    out.mkdir(exist_ok=True)
    (out / "board.json").unlink(missing_ok=True)
    (out / "index.html").write_text("""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>PropBoard NFL - offline</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0c0f0e;color:#e8efeb;
font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;text-align:center;padding:24px}
b{color:#4ade80}p{color:#8b9a91;max-width:420px}</style></head>
<body><div><h1>Prop<b>Board</b> NFL</h1><p>This site is offline for now. Thanks for stopping by.</p></div></body></html>""")
    (out / ".nojekyll").write_text("")
    print(f"TAKEDOWN_AT {takedown} has passed: published the offline page.")
    sys.exit(0)

sgo_key = os.environ.get("SGO_API_KEY", "").strip()
if sgo_key:
    try:
        sgo = app.pull_sgo(sgo_key, games=app.get_games())
        print(f"Pulled real SportsGameOdds lines for the public build: {sgo['events']} events.")
    except Exception as e:
        print(f"SportsGameOdds pull failed, publishing with EST-only lines instead: {e}")

board = app.get_board(force=True)
if not board["games"] or len(board["props"]) < 50:
    sys.exit(f"Refusing to publish: only {len(board['games'])} games / {len(board['props'])} props found.")

out.mkdir(exist_ok=True)
(out / "board.json").write_text(json.dumps(board, allow_nan=False, separators=(",", ":")))
with app.app.test_request_context():
    (out / "index.html").write_text(app.render_template("index.html", public=True))
(out / ".nojekyll").write_text("")
print(f"Built docs/: {len(board['games'])} games, {len(board['props'])} props, "
      f"{(out / 'board.json').stat().st_size // 1024} KB of data.")

# NCAAF: soft-fail — one bad/slow ESPN fetch shouldn't take the whole site down. If it doesn't meet
# the same sanity bar, leave whatever board_ncaaf.json is already published (or none) rather than error.
try:
    import cfb
    cfb_board = cfb.build_board()
    if not cfb_board["games"] or len(cfb_board["props"]) < 200:
        print(f"NCAAF: only {len(cfb_board['games'])} games / {len(cfb_board['props'])} props — not publishing this run.")
    else:
        (out / "board_ncaaf.json").write_text(json.dumps(cfb_board, allow_nan=False, separators=(",", ":")))
        print(f"Built docs/board_ncaaf.json: {len(cfb_board['games'])} games, {len(cfb_board['props'])} props, "
              f"{(out / 'board_ncaaf.json').stat().st_size // 1024} KB of data.")
except Exception as e:
    print(f"NCAAF build failed, leaving the site's NFL side unaffected: {e}")

# NBA: same soft-fail approach as NCAAF. The 2026-27 season hasn't started (checked live before
# building this at all — one preseason game on the schedule as of writing), so this will likely
# publish nothing yet, which is correct: no upcoming games means nothing real to show props for.
try:
    import nba
    nba_board = nba.build_board()
    if not nba_board["games"] or len(nba_board["props"]) < 20:
        print(f"NBA: only {len(nba_board['games'])} games / {len(nba_board['props'])} props "
              "— season likely hasn't started yet, not publishing this run.")
    else:
        (out / "board_nba.json").write_text(json.dumps(nba_board, allow_nan=False, separators=(",", ":")))
        print(f"Built docs/board_nba.json: {len(nba_board['games'])} games, {len(nba_board['props'])} props, "
              f"{(out / 'board_nba.json').stat().st_size // 1024} KB of data.")
except Exception as e:
    print(f"NBA build failed, leaving the rest of the site unaffected: {e}")

# NHL: same soft-fail approach. Unlike NBA, NHL preseason has already started (checked live before
# building this), but regular-season games are still weeks out, so this will likely publish little
# or nothing yet until real regular-season games exist to build a schedule from.
try:
    import nhl
    nhl_board = nhl.build_board()
    if not nhl_board["games"] or len(nhl_board["props"]) < 20:
        print(f"NHL: only {len(nhl_board['games'])} games / {len(nhl_board['props'])} props "
              "— season likely hasn't started yet, not publishing this run.")
    else:
        (out / "board_nhl.json").write_text(json.dumps(nhl_board, allow_nan=False, separators=(",", ":")))
        print(f"Built docs/board_nhl.json: {len(nhl_board['games'])} games, {len(nhl_board['props'])} props, "
              f"{(out / 'board_nhl.json').stat().st_size // 1024} KB of data.")
except Exception as e:
    print(f"NHL build failed, leaving the rest of the site unaffected: {e}")
