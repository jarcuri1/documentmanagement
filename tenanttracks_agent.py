"""
TenantTracksAgent — applicant screening via app.tenanttracks.com
================================================================
Two jobs (see TENANTTRACKS_UI_MAP.md for the mapped UI):

  1. PULL: scrape "Open Access Applications" (?page=applications) and upsert
     every application created in the last N days (default 30) into the
     applicant registry `shared\\lease_applicants.json`. A row with an
     "Open Report" button = the screening completed -> status "screened";
     otherwise "invited". New screened applicants push a notification.

  2. SCREEN: consume queued screening requests from
     `shared\\screening_queue\\*.json` and drive the Run-Background-Check
     flow: Applicant Pays -> existing property -> Option 1 (email request).
     Queue job shape:
       {"tt_property": "<exact TenantTracks property name>",
        "applicants": [{"email": "...", "phone": "2035550123"}, ...]}
     Jay's rules: applicant ALWAYS pays; fake phone when unknown.

Registry shape:
  {"aliases":   {"<TT Property Name>": "<lease_folders key>"},
   "applicants": {"<application id>": {tt_property, city, created,
                  email, name, status, property_key, first_seen, screened_at}},
   "updated_at": iso}

RUN (fleet PC; after `python set_login.py --service tenanttracks` once):
  python tenanttracks_agent.py --pull            # scrape + update registry
  python tenanttracks_agent.py --queue           # consume screening queue
  python tenanttracks_agent.py --once            # both, once (fleet tick)
  python tenanttracks_agent.py --dry-run --pull  # scrape + report, no writes
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

_SHARED = Path(os.environ.get("LEASE_SHARED_ROOT", r"C:\AIAgents\shared"))

CONFIG = {
    "app_url": "https://app.tenanttracks.com",
    "registry": Path(os.environ.get("TT_REGISTRY", str(_SHARED / "lease_applicants.json"))),
    "queue_dir": Path(os.environ.get("TT_QUEUE_DIR", str(_SHARED / "screening_queue"))),
    "push_outbox": Path(os.environ.get("LEASE_PUSH_OUTBOX", str(_SHARED / "push_outbox"))),
    "pull_days": int(os.environ.get("TT_PULL_DAYS", "30")),
    "profile_dir": Path(os.environ.get(
        "TT_PROFILE_DIR", str(Path(__file__).with_name("chrome-profile-tenanttracks")))),
    "step_timeout_ms": int(os.environ.get("TT_STEP_TIMEOUT_MS", "30000")),
    "keyring_service": "LeaseAgent-TenantTracks",
}

_push_seq = 0


def push(title, body, data=None):
    global _push_seq
    _push_seq += 1
    CONFIG["push_outbox"].mkdir(parents=True, exist_ok=True)
    payload = {"title": title, "body": body, "data": {**(data or {}), "kind": "screening"}}
    name = f"tt-{os.getpid()}-{int(time.time() * 1000)}-{_push_seq}.json"
    tmp = CONFIG["push_outbox"] / (name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(CONFIG["push_outbox"] / name)


def get_credentials():
    import keyring
    svc = CONFIG["keyring_service"]
    username = keyring.get_password(svc, "__username__")
    if not username:
        return None, None
    return username, keyring.get_password(svc, username)


def load_registry():
    try:
        return json.loads(CONFIG["registry"].read_text(encoding="utf-8"))
    except Exception:
        return {"aliases": {}, "applicants": {}, "updated_at": None}


def save_registry(reg):
    reg["updated_at"] = datetime.now().isoformat(timespec="seconds")
    tmp = CONFIG["registry"].with_suffix(".json.tmp")
    tmp.write_text(json.dumps(reg, indent=2), encoding="utf-8")
    tmp.replace(CONFIG["registry"])


# ----------------------------------------------------------------------
# Browser plumbing (same posture as lease_sender: headed, own profile)
# ----------------------------------------------------------------------
def open_browser(p):
    ctx = p.chromium.launch_persistent_context(
        str(CONFIG["profile_dir"]), headless=False,
        args=["--disable-blink-features=AutomationControlled"])
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return ctx, page


def login_if_needed(page):
    """Land on the dashboard; if bounced to /user/login, sign in from the
    stored credentials (Remember me ticked). Same abort contract as the
    lease sender: assert with a human-actionable message."""
    t = CONFIG["step_timeout_ms"]
    page.goto(f"{CONFIG['app_url']}/report_smart", timeout=t)
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1500)
    if "/user/login" not in page.url:
        return
    username, password = get_credentials()
    assert username and password, (
        "TenantTracks login required and no stored credentials. Run "
        "`python set_login.py --service tenanttracks` at the fleet PC.")
    page.fill("input[type='text']", username, timeout=t)
    page.fill("input[type='password']", password, timeout=t)
    try:
        page.check("input[type='checkbox']", timeout=3000)  # Remember me
    except Exception:
        pass
    page.click("button:has-text('Log in')", timeout=t)
    page.wait_for_url(re.compile(r"report_smart"), timeout=t)


# ----------------------------------------------------------------------
# PULL — scrape ?page=applications into the registry
# ----------------------------------------------------------------------
def scrape_applications(page):
    t = CONFIG["step_timeout_ms"]
    page.goto(f"{CONFIG['app_url']}/report_smart?page=applications", timeout=t)
    page.wait_for_selector("table", timeout=t)
    return page.evaluate("""() => {
      const rows = [...document.querySelectorAll('table tr')].slice(1);
      return rows.map(r => {
        const c = [...r.querySelectorAll('td')].map(td => td.innerText.trim());
        if (c.length < 6) return null;
        return {tt_property: c[0], city: c[1], app_id: c[2], created: c[3],
                email: c[4], name: c[5] || null,
                has_report: r.innerText.includes('Open Report')};
      }).filter(Boolean);
    }""")


def pull(page, dry_run=False):
    rows = scrape_applications(page)
    cutoff = datetime.now() - timedelta(days=CONFIG["pull_days"])
    reg = load_registry()
    apps, aliases = reg["applicants"], reg.get("aliases", {})
    newly_screened, added = [], 0
    for r in rows:
        try:
            created = datetime.strptime(r["created"].replace("\n", " ").strip(),
                                        "%m/%d/%Y %H:%M:%S")
        except ValueError:
            continue
        if created < cutoff:
            continue
        status = "screened" if r["has_report"] else "invited"
        cur = apps.get(r["app_id"])
        if dry_run:
            print(f"[DRY-RUN] {r['app_id']} {r['tt_property']} ({r['city']}) "
                  f"{r['email']} name={r['name']!r} -> {status}"
                  f"{' (new)' if not cur else ''}")
            continue
        if cur is None:
            cur = {"first_seen": datetime.now().isoformat(timespec="seconds")}
            apps[r["app_id"]] = cur
            added += 1
        was = cur.get("status")
        cur.update({
            "tt_property": r["tt_property"], "city": r["city"],
            "created": created.isoformat(timespec="seconds"),
            "email": r["email"], "name": r["name"] or cur.get("name"),
            "status": status,
            "property_key": aliases.get(r["tt_property"], cur.get("property_key")),
        })
        if status == "screened" and was != "screened":
            cur["screened_at"] = datetime.now().isoformat(timespec="seconds")
            newly_screened.append(cur)
    if dry_run:
        return 0
    save_registry(reg)
    for a in newly_screened:
        push("Screening complete",
             f"{a.get('name') or a['email']} — {a['tt_property']} ({a['city']}). "
             f"Report is ready on TenantTracks.",
             {"app_id": next(k for k, v in reg['applicants'].items() if v is a)})
    print(f"pull: {len(rows)} rows, {added} new, {len(newly_screened)} newly screened")
    return len(newly_screened)


# ----------------------------------------------------------------------
# SCREEN — drive the Option-1 request for queued jobs
# ----------------------------------------------------------------------
def run_screening(page, job):
    t = CONFIG["step_timeout_ms"]
    applicants = job.get("applicants") or []
    assert applicants and all(a.get("email") for a in applicants), "job needs applicant emails"
    for a in applicants:
        a.setdefault("phone", "2035550100")   # Jay's rule: fake number when unknown

    page.goto(f"{CONFIG['app_url']}/report_smart?page=new", timeout=t)
    # 1. payer — ALWAYS applicant pays
    page.click("text=Applicant Pays", timeout=t)
    page.click("text=Confirm", timeout=t)
    # 2. property — existing only (add-new is a manual/TODO path)
    prop = job["tt_property"]
    sel = page.locator("select").first
    sel.select_option(label=prop)
    page.click("text=Choose property", timeout=t)
    # 3. Option 1 form
    page.click("text=Option 1: Send Background check request", timeout=t)
    for i, a in enumerate(applicants):
        if i > 0:
            page.click("text=Add Additional Applicant", timeout=t)
        emails = page.locator("input[placeholder='Applicant Email']")
        retypes = page.locator("input[placeholder='Retype Applicant Email']")
        phones = page.locator("input[placeholder='Applicant Phone']")
        emails.nth(i).fill(a["email"])
        retypes.nth(i).fill(a["email"])
        phones.nth(i).fill(a["phone"])
    page.check("input[type='checkbox']", timeout=t)   # required confirm box
    page.click("text=Submit Application", timeout=t)
    page.wait_for_selector("text=The application has been saved", timeout=t)


def consume_queue(page, dry_run=False):
    qdir = CONFIG["queue_dir"]
    qdir.mkdir(parents=True, exist_ok=True)
    handled = 0
    for f in sorted(qdir.glob("*.json")):
        try:
            job = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"bad queue file {f.name}: {e}", file=sys.stderr)
            f.rename(f.with_suffix(".json.bad"))
            continue
        who = ", ".join(a.get("email", "?") for a in job.get("applicants", []))
        if dry_run:
            print(f"[DRY-RUN] would screen {who} @ {job.get('tt_property')}")
            continue
        try:
            run_screening(page, job)
            f.unlink()
            handled += 1
            push("Screening request sent",
                 f"{who} — {job.get('tt_property')}. Applicant pays; you'll be "
                 f"notified when they respond.", {"tt_property": job.get("tt_property")})
        except Exception as e:
            f.rename(f.with_suffix(".json.failed"))
            push("Screening request FAILED",
                 f"{who} @ {job.get('tt_property')}: {e}. Job set aside as .failed.",
                 {"tt_property": job.get("tt_property")})
    return handled


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pull", action="store_true")
    ap.add_argument("--queue", action="store_true")
    ap.add_argument("--once", action="store_true", help="pull + queue")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    do_pull = args.pull or args.once
    do_queue = args.queue or args.once
    if not (do_pull or do_queue):
        ap.error("pick --pull, --queue, or --once")

    # Nothing queued and pull not wanted? Don't even open a browser.
    if do_queue and not do_pull and not any(CONFIG["queue_dir"].glob("*.json")):
        print("queue empty — nothing to do")
        return

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx, page = open_browser(p)
        try:
            login_if_needed(page)
            if do_pull:
                pull(page, dry_run=args.dry_run)
            if do_queue:
                consume_queue(page, dry_run=args.dry_run)
        finally:
            ctx.close()


if __name__ == "__main__":
    main()
