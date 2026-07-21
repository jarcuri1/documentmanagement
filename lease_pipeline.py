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

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent

STAGES = [
    ("fill",  ["lease_fill.py", "--drain"]),
    ("send",  ["lease_watcher.py", "--once"]),
    ("file",  ["lease_signed_watcher.py"]),
]


def reap_orphaned_senders():
    """A sender orphaned by a crashed/killed watcher blocks the lane forever:
    its Chrome holds the persistent profile, so no new sender can launch, and
    the shell session can't kill Services-session processes — only we (running
    under the supervisor's task) can. Reap sender pythons and profile Chromes
    older than the watchdog cap; nothing younger is touched, and Jay's own
    Chrome uses a different user-data-dir so it never matches."""
    max_age_s = int(os.environ.get("LEASE_SENDER_TIMEOUT_S", "1500")) + 120
    ps = (
        "$cut=(Get-Date).AddSeconds(-%d);"
        "Get-CimInstance Win32_Process | Where-Object {"
        " $_.CommandLine -and $_.CreationDate -lt $cut -and"
        " ($_.CommandLine -like '*lease_sender.py*' -or"
        "  $_.CommandLine -like '*LeaseAgent\\chrome-profile*') } |"
        " ForEach-Object { Write-Output ('reaped stale pid ' + $_.ProcessId + ' ' + $_.Name);"
        " Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        % max_age_s)
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=60)
        if r.stdout.strip():
            print(f"--- [reap] {r.stdout.strip()}", flush=True)
    except Exception as e:
        print(f"--- [reap] skipped ({e})", flush=True)


reap_orphaned_senders()

worst = 0
for name, args in STAGES:
    print(f"--- [{name}] {' '.join(args)}", flush=True)
    r = subprocess.run([sys.executable, str(HERE / args[0]), *args[1:]], cwd=str(HERE))
    print(f"--- [{name}] exit {r.returncode}", flush=True)
    if r.returncode != 0:
        worst = max(worst, r.returncode)
sys.exit(worst)
