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


def fail_shot(page, label):
    """Screenshot on failure so a TenantTracks UI change is diagnosable from
    the audit folder instead of a bare traceback (same idea as the lease
    sender's Audit folders). Never raises."""
    try:
        d = Path(r"D:\Dropbox\Dropbox\Leases\Audit	enanttracks")
        d.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", label)[:60]
        f = d / f"{datetime.now():%Y%m%d-%H%M%S}-FAIL_{safe}.png"
        page.screenshot(path=str(f), full_page=True)
        print(f"    screenshot: {f}", file=sys.stderr)
        return str(f)
    except Exception:
        return ""


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
        str(CONFIG["profile_dir"]), channel="chrome", headless=False,
        args=["--disable-blink-features=AutomationControlled"])
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return ctx, page


# Logged-in marker: the nav shows "Run Background Check" on every app page.
_APP_MARKER = "a:has-text('Run Background Check')"
_LOGIN_MARKER = "input[type='password']"


def login_if_needed(page):
    """Land on the dashboard; the SPA can client-side-bounce to /user/login
    well after load, so wait until either the login form or the app nav is
    actually on screen. Sign in from stored credentials when needed."""
    t = CONFIG["step_timeout_ms"]
    # /user/login is the reliable entry: logged-out shows the form, logged-in
    # redirects into the app. (/report_smart while logged out bounces to the
    # MARKETING site tenanttracks.com, showing neither.)
    page.goto(f"{CONFIG['app_url']}/user/login", timeout=t)
    state = None
    deadline = time.time() + t / 1000.0
    while time.time() < deadline:
        if page.locator(_LOGIN_MARKER).count():
            state = "login"; break
        if page.locator(_APP_MARKER).count():
            state = "app"; break
        page.wait_for_timeout(500)
    if state == "app":
        return
    assert state == "login", (
        f"TenantTracks showed neither the app nor a login form ({page.url}) — "
        "site down or flow changed.")
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
    page.wait_for_selector(_APP_MARKER, timeout=t)


def goto_app_page(page, url, marker):
    """goto + wait for the page's own marker; if the session lapsed and we
    got bounced to the login page instead, log in and retry once."""
    t = CONFIG["step_timeout_ms"]
    page.goto(url, timeout=t)
    try:
        page.wait_for_selector(marker, timeout=10_000)
        return
    except Exception:
        pass
    if page.locator(_LOGIN_MARKER).count() or "/user/login" in page.url:
        login_if_needed(page)
        page.goto(url, timeout=t)
    page.wait_for_selector(marker, timeout=t)


# ----------------------------------------------------------------------
# PULL — scrape ?page=applications into the registry
# ----------------------------------------------------------------------
def scrape_applications(page):
    goto_app_page(page, f"{CONFIG['app_url']}/report_smart?page=applications", "table")
    # Cells carry their column label inline ("Application Created: 07/23...")
    # — strip the known labels, nothing else (emails/timestamps contain ':').
    return page.evaluate("""() => {
      const strip = s => s.replace(
        /^(Property Name|Property City|Application ID|Application Created|Applicant Email|Applicant Name)\\s*:\\s*/i, '');
      const rows = [...document.querySelectorAll('table tr')].slice(1);
      return rows.map(r => {
        const c = [...r.querySelectorAll('td')].map(td => strip(td.innerText.trim()));
        if (c.length < 6) return null;
        return {tt_property: c[0], city: c[1], app_id: c[2], created: c[3],
                email: c[4], name: c[5] || null,
                has_report: r.innerText.includes('Open Report')};
      }).filter(Boolean);
    }""")


# ----------------------------------------------------------------------
# Report synopsis — when a screening completes, read the TransUnion report
# and condense it into Jay's rubric via Claude. Reports EXPIRE fast on
# TenantTracks, so this runs the moment an applicant flips to 'screened'.
# ----------------------------------------------------------------------
def _anthropic_key():
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return key
    try:  # standalone runs outside the fleet env: parse shared\.env
        for line in (_SHARED / ".env").read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


_SYNOPSIS_PROMPT = """You are summarizing a tenant-screening report (TransUnion \
credit + criminal + eviction) for a landlord. Produce a PLAIN TEXT synopsis, \
max ~20 short lines, facts only — no advice, no recommendation, no commentary. \
The landlord makes his own decision.

Format, in this order:
1. If there is an auto/car repossession anywhere: FIRST line must be
   "SEVERE FLAG: CAR REPOSSESSION" plus the details on the next line.
2. "Score: <credit score>" (or "Score: not shown").
3. "Collections: <N> accounts" then one line each:
   "- <creditor>: $<amount> (<medical | student loan | other>)".
4. Missed-payment picture on NON-collection accounts, as a pattern statement,
   e.g. "No missed payments outside the collections in the past 24 months"
   or "5 missed payments on other accounts in the past 2 years (latest: <date>)".
   List medical and student-loan missed payments SEPARATELY and label them
   "(medical — typically disregarded)" / "(student loan — typically disregarded)"
   — the landlord does not count those against applicants.
5. "Criminal: <records with charge + year, or 'none reported'>".
6. "Evictions: <records with year + outcome, or 'none reported'>".

If the report text is garbled or missing a section, say "<section>: could not \
read" rather than guessing. Never invent numbers."""


def synopsize_report(report_text, applicant_label):
    """One Claude call -> plain-text synopsis, or None on any failure.
    Uses claude-opus-5 per the fleet's approved tier for tenant-facing work
    (accuracy-sensitive, ~pennies per report at this volume)."""
    key = _anthropic_key()
    if not key:
        print("WARN: no ANTHROPIC_API_KEY — skipping report synopsis", file=sys.stderr)
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        resp = client.messages.create(
            model="claude-opus-5",
            max_tokens=2000,
            system=_SYNOPSIS_PROMPT,
            messages=[{
                "role": "user",
                "content": f"Applicant: {applicant_label}\n\nReport text:\n{report_text[:150000]}",
            }],
        )
        if resp.stop_reason == "refusal":
            print("WARN: synopsis refused", file=sys.stderr)
            return None
        return next((b.text for b in resp.content if b.type == "text"), None)
    except Exception as e:
        print(f"WARN: synopsis failed ({e})", file=sys.stderr)
        return None


def fetch_report_text(page, app_id):
    """Open a completed application's report page; None when expired/unreadable."""
    goto_app_page(
        page,
        f"{CONFIG['app_url']}/report_smart?page=applicationSa&application_id={app_id}",
        "body")
    page.wait_for_timeout(4000)
    text = page.evaluate("() => document.body.innerText")
    if "expired and no longer available" in text or "Errors getting application" in text:
        return None
    if len(text) < 2000:  # report content plainly didn't load
        hidden = page.evaluate("() => document.body.textContent")
        if len(hidden) > len(text) * 2:
            return hidden
        return None
    return text


def _auto_alias(tt_property, aliases, folder_keys):
    """Map a TenantTracks property name to a lease_folders key when the
    slugified name prefixes exactly one key (e.g. '128 Walnut St' ->
    '128-walnut-st-naugatuck'). Ambiguous or no match: leave unmapped —
    Jay can add it to the registry's aliases by hand."""
    if tt_property in aliases:
        return
    slug = re.sub(r"[^a-z0-9]+", "-", tt_property.lower()).strip("-")
    if not slug:
        return
    hits = [k for k in folder_keys if k.startswith(slug)]
    if len(hits) == 1:
        aliases[tt_property] = hits[0]


def scrape_properties(page):
    """All TenantTracks properties (?page=properties: Name | Address | City) —
    feeds the app's property picker so screening isn't limited to properties
    that already have applicants."""
    goto_app_page(page, f"{CONFIG['app_url']}/report_smart?page=properties", "table")
    return page.evaluate("""() => {
      const strip = s => s.replace(/^(Name|Address|City)\\s*:\\s*/i, '');
      const rows = [...document.querySelectorAll('table tr')].slice(1);
      return rows.map(r => {
        const c = [...r.querySelectorAll('td')].map(td => strip(td.innerText.trim()));
        return c.length >= 3 ? {name: c[0], address: c[1], city: c[2]} : null;
      }).filter(x => x && x.name);
    }""")


def pull(page, dry_run=False):
    rows = scrape_applications(page)
    cutoff = datetime.now() - timedelta(days=CONFIG["pull_days"])
    reg = load_registry()
    apps, aliases = reg["applicants"], reg.get("aliases", {})
    try:
        folder_keys = list(json.loads(
            (_SHARED / "lease_folders.json").read_text(encoding="utf-8")).keys())
    except Exception:
        folder_keys = []
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
        _auto_alias(r["tt_property"], aliases, folder_keys)
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
            newly_screened.append((r["app_id"], cur))
    if dry_run:
        return 0
    try:
        props = scrape_properties(page)
        if props:
            reg["tt_properties"] = props
            for pr in props:
                _auto_alias(pr["name"], aliases, folder_keys)
    except Exception as e:
        print(f"property scrape failed (registry keeps old list): {e}", file=sys.stderr)
    save_registry(reg)
    # Reports expire quickly — synopsize each fresh completion NOW, while the
    # browser is open and the report is still live.
    for app_id, a in newly_screened:
        label = f"{a.get('name') or a['email']} — {a['tt_property']} ({a['city']})"
        synopsis = None
        try:
            text = fetch_report_text(page, app_id)
            if text:
                synopsis = synopsize_report(text, label)
        except Exception as e:
            print(f"report fetch failed for {app_id}: {e}", file=sys.stderr)
        if synopsis:
            a["synopsis"] = synopsis
            push(f"Screening complete: {a.get('name') or a['email']}",
                 f"{label}\n\n{synopsis[:3000]}", {"app_id": app_id})
        else:
            push("Screening complete",
                 f"{label}. Report is ready on TenantTracks (no synopsis — "
                 f"open the site to view).", {"app_id": app_id})
    if newly_screened:
        save_registry(reg)
    print(f"pull: {len(rows)} rows, {added} new, {len(newly_screened)} newly screened")
    return len(newly_screened)


# ----------------------------------------------------------------------
# SCREEN — drive the Option-1 request for queued jobs
def _derive_property_details(typed):
    """Best-effort details for TT's 'Add New Property' form. Sources, in
    order: the master sheet via the supervisor's lease options (fuzzy match
    on the typed name), then whatever Jay typed ('street, town zip').
    Returns None when no city could be worked out (form requires it)."""
    import urllib.request
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    street = typed.split(",")[0].strip()
    details = {"name": street, "address": street, "city": "", "zip": "",
               "state": "CT", "deposit": "1000", "rent": "1000"}
    if "," in typed:
        rest = typed.split(",", 1)[1]
        m = re.search(r"(\d{5})", rest)
        if m:
            details["zip"] = m.group(1)
        sm = re.search(r"\b(CT|NH|MA|NY|RI)\b", rest, re.I)
        if sm:
            details["state"] = sm.group(1).upper()
        details["city"] = re.sub(r"\b(CT|NH|MA|NY|RI)\b|\d{5}", "", rest, flags=re.I).strip(" ,")
    try:
        base = os.environ.get("SUPERVISOR_URL", "http://100.66.99.5:8787")
        with urllib.request.urlopen(f"{base}/api/lease/options", timeout=30) as r:
            props = json.loads(r.read().decode()).get("properties", [])
    except Exception:
        props = []
    want = norm(street)
    best = next((p for p in props if want and want in norm(
        (p.get("sheet_address") or "") + " " + (p.get("label") or ""))), None)
    if best:
        sa = best.get("sheet_address") or ""
        parts = [x.strip() for x in sa.split(",") if x.strip()]
        if parts:
            details["address"] = parts[0]
            details["name"] = parts[0]   # short street name, TT convention
        if len(parts) > 1 and not details["city"]:
            details["city"] = re.sub(r"\bCT\b|\d{5}", "", parts[1], flags=re.I).strip(" ,")
        if not details["city"] and best.get("town"):
            details["city"] = best["town"]
        m = re.search(r"(\d{5})", sa)
        if m and not details["zip"]:
            details["zip"] = m.group(1)
        units = (best.get("sheet") or {}).get("units") or []
        if units:
            digits = lambda v: re.sub(r"[^0-9]", "", str(v or ""))
            if digits(units[0].get("rent")):
                details["rent"] = digits(units[0]["rent"])
            if digits(units[0].get("deposit")):
                details["deposit"] = digits(units[0]["deposit"])
        sm = re.search(r"\b(CT|NH|MA|NY|RI)\b", sa, re.I)
        if sm:
            details["state"] = sm.group(1).upper()
    # Sheet cells like '2026 N Main St Pittsburg, NH' embed the town in the
    # street part — split at the street-type suffix (the NH property failed
    # here 8/19: city came out 'NH', state stayed Connecticut, TT refused
    # the save and the run timed out). Runs LAST so the sheet match above
    # can't clobber the cleaned address.
    if not details["city"] or details["city"].upper() == details["state"]:
        ssm = re.match(r"(.*?\b(?:St|Rd|Ave|Ln|Dr|Tpke|Turnpike|Street|Road|Avenue|Lane|Drive)\.?)\s+(.+)$",
                       details["address"], re.I)
        if ssm:
            details["address"] = details["name"] = ssm.group(1).strip()
            details["city"] = ssm.group(2).strip()
    # Zips the sheet doesn't carry (out-of-state properties):
    _ZIP_HINTS = {"pittsburg nh": "03592"}
    if not details["zip"]:
        details["zip"] = _ZIP_HINTS.get(f"{details['city']} {details['state']}".lower().strip(), "")
    return details if details["city"] else None


_STATE_NAMES = {"CT": "Connecticut", "NH": "New Hampshire", "MA": "Massachusetts",
                "NY": "New York", "RI": "Rhode Island"}


def _create_property(page, details):
    """Fill and save TT's Add New Property form (marketplace visibility and
    the $49.99 MA-records add-on stay 'No'). State: the dropdown defaults
    to Connecticut — select the property's real state (NH property 8/19)."""
    t = CONFIG["step_timeout_ms"]
    page.fill("input[placeholder='Property Name']", details["name"])
    page.fill("input[placeholder='Property Address']", details["address"])
    page.fill("input[placeholder='Property City']", details["city"])
    state = details.get("state") or "CT"
    if state != "CT":
        # the add-new form's state dropdown sits next to the Postal Code box
        # (NOT the page's first select — that's Choose Existing Property)
        sel = page.locator("select:near(input[placeholder='Postal Code'])").first
        try:
            sel.select_option(label=_STATE_NAMES.get(state, state))
        except Exception:
            sel.select_option(value=state)
    if details["zip"]:
        page.fill("input[placeholder='Postal Code']", details["zip"])
    page.fill("input[placeholder='Security Deposit']", details["deposit"])
    page.fill("input[placeholder='Rent Amount']", details["rent"])
    page.locator('button:has-text("Save Property"), a:text-is("Save Property")') \
        .first.click(timeout=t)
    # Saving drops us on the same step-3 panel as "Choose property" does.
    page.wait_for_selector("text=Option 1: Send Background check request", timeout=t)


# ----------------------------------------------------------------------
def run_screening(page, job):
    """One FULL pass through the request flow PER APPLICANT. 'Add Additional
    Applicant' did not render a second input row the way the map assumed
    (first 2-applicant job, 8/19, timed out) — separate submissions are
    equivalent: TT invites and charges each applicant individually, and the
    property exists after pass one so pass two just picks it."""
    applicants = job.get("applicants") or []
    assert applicants and all(a.get("email") for a in applicants), "job needs applicant emails"
    for a in applicants:
        a.setdefault("phone", "2035550100")   # Jay's rule: fake number when unknown
    for a in applicants:
        _run_one_screening(page, job, a)


def _run_one_screening(page, job, applicant):
    t = CONFIG["step_timeout_ms"]
    goto_app_page(page, f"{CONFIG['app_url']}/report_smart?page=new",
                  'text="Applicant Pays"')
    # 1. payer — ALWAYS applicant pays. EXACT text matches only: the intro
    # paragraph also contains the words "Applicant Pays"/"Confirm", and a
    # bare text= selector clicks the paragraph instead of the button.
    page.click('text="Applicant Pays"', timeout=t)
    page.wait_for_timeout(500)
    page.click('text="Confirm"', timeout=t)
    page.wait_for_selector("select", timeout=t)
    # 2. property — fuzzy-match Jay's wording against the live dropdown
    #    (he types from memory; 'walnut b' should find '128 Walnut B').
    prop = job["tt_property"]
    sel = page.locator("select").first
    options = sel.evaluate("el => [...el.options].map(o => o.label || o.text)")
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    want = norm(prop)
    exact = [o for o in options if norm(o) == want]
    loose = [o for o in options if want in norm(o) or norm(o) in want]
    pick = exact[0] if exact else (loose[0] if len(loose) == 1 else None)
    if pick:
        sel.select_option(label=pick)
        page.click('text="Choose property"', timeout=t)
    else:
        # Unknown property — create it on TenantTracks (Jay's rule: deduce,
        # and if you can't, make the property rather than fail).
        assert len(loose) <= 1, (
            f"{prop!r} is ambiguous on TenantTracks: {loose[:5]} — use one of "
            "those exact names.")
        details = _derive_property_details(prop)
        assert details, (
            f"no TenantTracks property matches {prop!r} and I couldn't work "
            "out its address/town/zip from the sheet — retype it as "
            "'street, town zip' (e.g. '29 Evans St, Waterbury 06705').")
        _create_property(page, details)
    # 3. Option 1 form — single applicant per pass (see run_screening)
    page.click("text=Option 1: Send Background check request", timeout=t)
    page.locator("input[placeholder='Applicant Email']").first.fill(applicant["email"])
    page.locator("input[placeholder='Retype Applicant Email']").first.fill(applicant["email"])
    page.locator("input[placeholder='Applicant Phone']").first.fill(applicant["phone"])
    # The whole flow is ONE page (anchored sections), so the MA criminal
    # add-on checkbox from step 1 is also in the DOM — scope to the checkbox
    # next to the "I confirm I have read" text, NEVER the first on the page.
    cb = page.locator("input[type='checkbox']:near(:text('I confirm I have read'))").first
    cb.check(timeout=t)
    # Submit is an <a> that's only VISIBLE while the box is checked. NEVER
    # text=Submit Application here: the label "(required to submit
    # application)" substring-matches first, and clicking it UNCHECKS the box.
    page.locator('a:text-is("Submit Application")').first.click(timeout=t)
    page.wait_for_selector("text=The application has been saved", timeout=t)


def consume_queue(page, dry_run=False):
    qdir = CONFIG["queue_dir"]
    qdir.mkdir(parents=True, exist_ok=True)
    handled = 0
    screened_any = False
    pulled = False
    for f in sorted(qdir.glob("*.json")):
        try:
            job = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"bad queue file {f.name}: {e}", file=sys.stderr)
            f.rename(f.with_suffix(".json.bad"))
            continue
        # Jay's refresh button queues {"action": "pull"} — on-demand only,
        # never scheduled (his rule: no constant rescanning).
        if job.get("action") == "pull":
            if dry_run:
                print("[DRY-RUN] would pull applications")
                continue
            try:
                pull(page)
                pulled = True
            finally:
                f.unlink()
            handled += 1
            continue
        who = ", ".join(a.get("email", "?") for a in job.get("applicants", []))
        if dry_run:
            print(f"[DRY-RUN] would screen {who} @ {job.get('tt_property')}")
            continue
        try:
            run_screening(page, job)
            f.unlink()
            handled += 1
            screened_any = True
            push("Screening request sent",
                 f"{who} — {job.get('tt_property')}. Applicant pays; you'll be "
                 f"notified when they respond.", {"tt_property": job.get("tt_property")})
        except Exception as e:
            import traceback
            traceback.print_exc()
            shot = fail_shot(page, f"screen_{job.get('tt_property', '')}")
            f.rename(f.with_suffix(".json.failed"))
            push("Screening request FAILED",
                 f"{who} @ {job.get('tt_property')}: {e}. Job set aside as .failed."
                 f"{' Screenshot: ' + shot if shot else ''}",
                 {"tt_property": job.get("tt_property")})
    # Browser's already open after a send — grab the fresh rows so the new
    # invite shows in the app without Jay tapping refresh.
    if screened_any and not pulled:
        try:
            pull(page)
        except Exception as e:
            print(f"post-send pull failed: {e}", file=sys.stderr)
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
            stage = "login"
            login_if_needed(page)
            if do_pull:
                stage = "pull"
                pull(page, dry_run=args.dry_run)
            if do_queue:
                stage = "queue"
                consume_queue(page, dry_run=args.dry_run)
        except Exception:
            fail_shot(page, stage)
            raise
        finally:
            ctx.close()


if __name__ == "__main__":
    main()
