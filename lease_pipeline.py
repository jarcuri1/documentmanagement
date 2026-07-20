"""
lease_pipeline.py — the whole lease pipeline as ONE supervisor agent
====================================================================
Runs the three run-once stages in order, every supervisor tick:
  1. lease_fill --drain          new intakes -> filled packet + approval card
  2. lease_watcher --once        approved decisions -> claim -> SmartMLS send
  3. lease_signed_watcher        completed signings -> pull PDF -> file it

Replaces the separate lease_fill / lease_watcher / lease_signed fleet entries
so the Agents tab shows a single "Lease Pipeline" tile. Each stage's output is
prefixed so the combined log stays readable; the worst exit code wins.
"""

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent

STAGES = [
    ("fill",  ["lease_fill.py", "--drain"]),
    ("send",  ["lease_watcher.py", "--once"]),
    ("file",  ["lease_signed_watcher.py"]),
]

worst = 0
for name, args in STAGES:
    print(f"--- [{name}] {' '.join(args)}", flush=True)
    r = subprocess.run([sys.executable, str(HERE / args[0]), *args[1:]], cwd=str(HERE))
    print(f"--- [{name}] exit {r.returncode}", flush=True)
    if r.returncode != 0:
        worst = max(worst, r.returncode)
sys.exit(worst)
