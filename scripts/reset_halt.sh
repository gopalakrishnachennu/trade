#!/usr/bin/env bash
# Manual clear of the fail-closed halt latch. Requires --yes.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ "${1:-}" != "--yes" ]]; then
  echo "Usage: scripts/reset_halt.sh --yes" >&2
  echo "This is the only supported way to clear data/runtime/HALT.json after you have read the reason." >&2
  exit 2
fi
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
PYTHONPATH="$ROOT/src" "$PYTHON" - <<'PY'
from pathlib import Path
import sys
sys.path.insert(0, str(Path("src") if Path("src").exists() else Path("/workspace/polymarket-bot/src")))
from polymarket_bot.runtime.interlock import HaltLatch
from polymarket_bot.runtime.bus import Bus
root = Path("/workspace/polymarket-bot/data/runtime")
latch = HaltLatch(root / "HALT.json")
before = latch.read()
latch.reset(yes=True)
Bus(root).event("manual_reset", "operator", f"cleared halt: {before.reason} / {before.component} / {before.details}")
print("halt cleared" if before.halted else "halt was already clear")
if before.halted:
    print(f"previous: {before.reason} from {before.component}: {before.details}")
PY
