#!/bin/bash
# Launch the 2-agent live demo: seed the AST database, then open two Terminal
# windows, each a Claude Code agent whose only world is the shared SQLite
# codebase (no files, no git). Both agents commit straight to main through the
# speculative merge queue; there is no merge agent — conflicts bounce back to
# whoever caused them.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"

python3 "$DIR/init_db.py" --fresh

TASK1="Add quantity support: Item should accept a quantity argument (default 1), and total_price should charge price * quantity per item."
TASK2="Add discount support: add a function apply_discount(subtotal, code) where code 'SAVE10' gives 10% off and any other/None code gives no discount, and total_price should accept an optional discount_code argument and apply it to the subtotal before tax."

open_window() {
  osascript -e "tell application \"Terminal\" to do script \"cd '$DIR' && ./launch-agent.sh $1\""
}

open_window "feature-1 $TASK1"
open_window "feature-2 $TASK2"

echo "Launched 2 agent TUIs in Terminal windows:"
echo "  feature-1  — $TASK1"
echo "  feature-2  — $TASK2"
echo
echo "Both tasks touch total_price, so expect one agent to hit a conflict or"
echo "verification failure and adapt. Watch from here with:"
echo "  python3 astctl.py status | render | outline | log | queue"
echo
echo "If a pending worktree ever gets orphaned: ./launch-agent.sh resolver"
