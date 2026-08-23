"""Monthly lease-stack health check (Jay, 2026-08-23: "a lightweight health
check once a month to verify no new issues have arisen and if some did, fix
it"). Exercises the real browser flows WITHOUT sending anything:

  1. SmartMLS Sign: assemble the full packet for the 111 Test St fixture and
     stop before Send (lease_sender.py --no-send). Leaves a draft named
     "HEALTHCHECK - ..." that should be deleted in Sign afterwards.
  2. TenantTracks: log in and scrape applications + properties (read-only).

Prints a JSON summary and exits 0 if both passed, 1 otherwise. Run from
C:\AIAgents\LeaseAgent:  python healthcheck\run_healthcheck.py
"""
import json, shutil, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PY = sys.executable
results = {}

# --- 1. lease sender dry run ------------------------------------------------
job = HERE / "healthcheck-job.json"
shutil.copy(HERE / "healthcheck-job.template.json", job)
t0 = time.time()
for attempt in range(2):   # one retry: a lingering Chrome can close the first page
    shutil.copy(HERE / "healthcheck-job.template.json", job)
    r = subprocess.run([PY, str(ROOT / "lease_sender.py"), "--job", str(job), "--no-send"],
                       cwd=ROOT, capture_output=True, text=True, timeout=900)
    out = (r.stdout or "") + (r.stderr or "")
    ok = r.returncode == 0 and "stopped BEFORE Send" in out
    if ok or "has been closed" not in out:
        break
    time.sleep(15)
results["lease_sender"] = {"ok": ok, "seconds": round(time.time() - t0),
                           "tail": out[-1500:]}
# an abort moves the job copy into Failed under the fixture's name — clean it
stray = Path(r"D:\Dropbox\Dropbox\Leases\Failed") / job.name
if stray.exists():
    stray.unlink()
if job.exists():
    job.unlink()

# --- 2. tenanttracks login + scrape ----------------------------------------
t0 = time.time()
r = subprocess.run([PY, str(ROOT / "tenanttracks_agent.py"), "--pull", "--dry-run"],
                   cwd=ROOT, capture_output=True, text=True, timeout=600)
out = (r.stdout or "") + (r.stderr or "")
results["tenanttracks"] = {"ok": r.returncode == 0, "seconds": round(time.time() - t0),
                           "tail": out[-1500:]}

print(json.dumps(results, indent=2))
sys.exit(0 if all(v["ok"] for v in results.values()) else 1)
