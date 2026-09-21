#!/bin/bash
# Double-click this file to launch PropBoard NFL.
# It starts the site and opens it in your web browser.

cd "$(dirname "$0")"

echo "Starting PropBoard NFL..."
echo "The first load takes about 10 seconds while it downloads fresh stats."
echo "Leave this window open while you use it."
echo "Close this window (or press Ctrl+C) when you're done."
echo ""

# Open the browser a moment after the server starts.
( sleep 3 && open "http://127.0.0.1:5051" ) &

./venv/bin/python app.py
