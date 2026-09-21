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
