"""Build the public, static copy of PropBoard into ./docs.

    python build_static.py

Writes docs/board.json (all the data) and docs/index.html (the page in "public" mode: no refresh or
API-key buttons). Host the docs folder anywhere. If the data looks broken it exits with an error and
writes nothing, so a bad run can never replace a good site.
"""
import json
import sys
from pathlib import Path

import app

out = Path(__file__).parent / "docs"

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
