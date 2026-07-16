"""
LeaseAgent — unattended Authentisign sender
============================================
Runs on the fleet PC. Takes an APPROVED lease job, drives a real Chrome
window into SmartMLS -> Authentisign, creates the signing from your saved
lease template, adds the tenant, verifies everything on-screen matches the
approved job, clicks Send, and files the audit trail to Dropbox.

This is piece 2 of the pipeline (see HANDOFF_LEASE_AGENT.md). It is invoked
by lease_watcher.py, which has already CLAIMED the job by moving its JSON
into Dropbox\\Leases\\Sending before this script launches. That claim-first
move is what makes double-sends impossible; this script must therefore
never put a job back into a pickable state (Pending) on its own.

DESIGN RULES (do not soften these):
  1. Script never guesses. Any unexpected page/element -> screenshot,
     abort, notify. No retries past MAX_RETRIES, no creative clicking.
  2. Send is only clicked after the on-screen tenant email matches the
     approved email (normalized, case-insensitive) AND no unexpected email
     appears in the signer list.
  3. Every step screenshots to the job's Dropbox folder (audit trail).
  4. File placement is one-way. On a pre-send abort the job goes to Failed.
     On any failure AFTER Send was clicked the job is LEFT in Sending and
     flagged for manual review -- a crash must never cause a silent resend
     and must never be mistaken for "never sent".

ONE-TIME SETUP (do this while at the PC):
  1. pip install playwright && playwright install chrome
  2. Log the automation profile in once:
       python lease_sender.py --setup
     A Chrome window opens using the persistent profile. Log into
     smartmls.com (complete any MFA), open Authentisign once, then close
     the window. Cookies persist in BROWSER_PROFILE_DIR.
  3. Build your Authentisign template ("CE Residential Lease" or similar)
     with all signature/initial/date blocks pre-placed. Put its exact
     name in CONFIG["template_name"].
  4. Capture real selectors: Authentisign 2.0's DOM will not match my
     placeholders exactly. Run:
       playwright codegen --user-data-dir="<BROWSER_PROFILE_DIR>" https://www.smartmls.com
     Walk through one signing manually; codegen prints the selectors.
     Update the SELECTORS dict below -- it's the ONLY place they live.
  5. Put your own signer email (if you countersign) in
     CONFIG["signer_whitelist"] so the recipient check doesn't abort on it.

PER-LEASE FLOW (wired into the fleet):
  - Fill agent drops:   Dropbox\\Leases\\Pending\\<job>.pdf + <job>.json
  - Supervisor pushes an approval card; you tap Approve in the Samantha app
  - lease_watcher.py consumes the `send` decision, CLAIMS the job
    (Pending -> Sending), then runs:
       python lease_sender.py --job "C:\\...\\Sending\\123-main-smith.json"

EXIT CODES (read by lease_watcher):
  0  sent and filed to Sent
  1  aborted BEFORE Send -> job filed to Failed
  2  Send was clicked but confirmation not observed -> job LEFT in Sending
  3  sent successfully but filing to Sent failed -> job LEFT in Sending

JOB FILE FORMAT (<job>.json):
{
  "property": "123 Main St Apt 2, Waterbury CT",
  "tenant_name": "John Smith",
  "tenant_email": "jsmith@example.com",
  "pdf_path": "C:\\Users\\Jay\\Dropbox\\Leases\\Pending\\123-main-smith.pdf",
  "signing_name": "Lease - 123 Main St Apt 2 - Smith"
}
"""

import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ----------------------------------------------------------------------
# CONFIG — edit paths to match the fleet layout
# ----------------------------------------------------------------------
CONFIG = {
    "smartmls_url": "https://www.smartmls.com",
    "template_name": "CE Residential Lease",          # exact Authentisign template name
    "browser_profile_dir": r"C:\AIAgents\LeaseAgent\chrome-profile",
    "dropbox_root": r"D:\Dropbox\Dropbox\Leases",   # adjust if Dropbox lives elsewhere
    "sent_dir": r"D:\Dropbox\Dropbox\Leases\Sent",
    "failed_dir": r"D:\Dropbox\Dropbox\Leases\Failed",
    "audit_dir": r"D:\Dropbox\Dropbox\Leases\Audit",
    "push_outbox_dir": r"C:\AIAgents\shared\push_outbox",  # supervisor sweeps this -> phone push
    # Emails that may legitimately appear in the signer list besides the
    # tenant (e.g. Jay's own email if he countersigns). Compared normalized.
    "signer_whitelist": [],   # e.g. ["jay@premioproperty.com"]
    "step_timeout_ms": 30_000,
    "headed": True,   # keep True: some MLS auth flows behave better headed
}

# ----------------------------------------------------------------------
# SELECTORS — placeholders. Replace with real ones from `playwright codegen`.
# This dict is the single source of truth; nothing else hardcodes selectors.
# ----------------------------------------------------------------------
SELECTORS = {
    # SmartMLS dashboard
    "authentisign_tile":    "text=Authentisign",           # tile on Member Dashboard
    "logged_in_marker":     "text=Member Dashboard",       # proves session is alive

    # Create Signing screen
    "new_signing_btn":      "[data-testid='add-signing']",  # the + / Add icon
    "signing_name_input":   "input[name='signingName']",
    "create_btn":           "button:has-text('Create')",

    # Add document
    "add_doc_btn":          "button:has-text('Add Document')",
    "upload_input":         "input[type='file']",           # direct file upload path
    "doc_uploaded_marker":  ".document-thumbnail",

    # Apply template
    "templates_btn":        "button:has-text('Templates')",
    "template_row":         "text={template_name}",         # filled at runtime
    "apply_template_btn":   "button:has-text('Apply')",

    # Participants
    "signers_btn":          "button:has-text('Signers')",
    "add_participant_btn":  "button:has-text('Add Participants')",
    "add_new_contact":      "text=Add New",
    "participant_name":     "input[name='fullName']",
    "participant_email":    "input[name='email']",
    "participant_role":     "select[name='role']",          # choose tenant/lessee role
    "participant_save":     "button:has-text('Save')",

    # Review + send
    "review_email_text":    ".participant-list",            # container we read email back from
    "send_btn":             "button:has-text('Send')",
    "sent_confirmation":    "text=invitation",              # 'signing invites will be sent'
}

MAX_RETRIES = 1  # per step; beyond this we abort, never improvise

# Two patterns, on purpose:
#  _EMAIL_TOKEN  — loose, used ONLY anchored (^...$) to validate a whole
#                  trimmed email field.
#  _EMAIL_SCRAPE — bounded, used to pull emails OUT of free-form on-screen
#                  text. It must stop at punctuation: the review pane renders
#                  things like "jsmith@example.com;" or "jsmith@x.com (Tenant)",
#                  and a greedy [^@\s]+ would capture the trailing ';' / '(' and
#                  make a legitimate recipient read as a mismatch -> false abort.
_EMAIL_TOKEN = r"[^@\s]+@[^@\s]+\.[^@\s]+"
_EMAIL_SCRAPE = r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
_push_seq = 0


def notify(level: str, message: str, job_name: str = ""):
    """Log locally always; for meaningful events (success/error) also drop a
    push onto the Supervisor's push_outbox rail (swept every 15s -> Jay's
    phone). Per the fleet contract, info-level steps do NOT push — one push
    per meaningful event, sent or failed.
    """
    global _push_seq
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {level.upper()}: {message}")
    if level not in ("success", "error"):
        return
    _push_seq += 1
    outbox = Path(CONFIG["push_outbox_dir"])
    outbox.mkdir(parents=True, exist_ok=True)
    title = "Lease sent" if level == "success" else "Lease agent error"
    payload = {"title": title, "body": message, "data": {"kind": "lease", "job": job_name}}
    name = f"lease-{os.getpid()}-{int(time.time() * 1000)}-{_push_seq}.json"
    tmp = outbox / (name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(outbox / name)


class Auditor:
    """Numbered screenshots per job into the Dropbox audit folder."""

    def __init__(self, job_name: str, page):
        self.page = page
        self.n = 0
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir = Path(CONFIG["audit_dir"]) / f"{job_name}-{stamp}"
        self.dir.mkdir(parents=True, exist_ok=True)

    def snap(self, label: str):
        self.n += 1
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", label)[:60]
        self.page.screenshot(path=str(self.dir / f"{self.n:02d}-{safe}.png"),
                             full_page=True)


class StepFailure(Exception):
    pass


def step(auditor: Auditor, label: str, fn):
    """Run one step with screenshot-on-success and screenshot+abort on failure."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            result = fn()
            auditor.snap(f"ok {label}")
            return result
        except (PWTimeout, AssertionError) as e:
            if attempt < MAX_RETRIES:
                time.sleep(2)
                continue
            auditor.snap(f"FAIL {label}")
            raise StepFailure(f"Step failed: {label} — {e}") from e


def signers_of(job: dict) -> list:
    """The lease's signers. Supports the multi-signer contract and the legacy
    single-tenant shape so older job files still work."""
    if job.get("signers"):
        return job["signers"]
    if job.get("tenant_name") and job.get("tenant_email"):
        return [{"name": job["tenant_name"], "email": job["tenant_email"]}]
    return []


def load_job(path: Path) -> dict:
    job = json.loads(path.read_text(encoding="utf-8"))
    required = ["property", "pdf_path", "signing_name"]
    missing = [k for k in required if not job.get(k)]
    if missing:
        raise ValueError(f"Job file missing fields: {missing}")
    signers = signers_of(job)
    if not signers:
        raise ValueError("Job file has no signers (need signers[] or tenant_name/tenant_email)")
    for i, s in enumerate(signers, 1):
        if not s.get("name") or not s.get("email"):
            raise ValueError(f"Signer {i} missing name/email: {s!r}")
        if not re.match(rf"^{_EMAIL_TOKEN}$", s["email"]):
            raise ValueError(f"Signer {i} email looks malformed: {s['email']}")
    if not Path(job["pdf_path"]).exists():
        raise ValueError(f"Lease PDF not found: {job['pdf_path']}")
    return job


# ----------------------------------------------------------------------
# The signing routine
# ----------------------------------------------------------------------
def run_signing(page, job: dict, auditor: Auditor, state: dict):
    t = CONFIG["step_timeout_ms"]
    S = SELECTORS

    # 1. SmartMLS — confirm we're logged in (never type credentials here)
    def goto_dashboard():
        page.goto(CONFIG["smartmls_url"], timeout=t)
        page.wait_for_selector(S["logged_in_marker"], timeout=t)
    step(auditor, "smartmls dashboard (session alive)", goto_dashboard)

    # 2. Open Authentisign
    def open_authentisign():
        page.click(S["authentisign_tile"], timeout=t)
        page.wait_for_load_state("networkidle", timeout=t)
    step(auditor, "open authentisign", open_authentisign)

    # 3. New signing with the job's name
    def create_signing():
        page.click(S["new_signing_btn"], timeout=t)
        page.fill(S["signing_name_input"], job["signing_name"], timeout=t)
        page.click(S["create_btn"], timeout=t)
    step(auditor, "create signing", create_signing)

    # 4. Upload the filled lease PDF
    def add_document():
        page.click(S["add_doc_btn"], timeout=t)
        page.set_input_files(S["upload_input"], job["pdf_path"], timeout=t)
        page.wait_for_selector(S["doc_uploaded_marker"], timeout=t)
    step(auditor, "upload lease pdf", add_document)

    # 5. Apply the saved lease template (pre-placed signature blocks)
    def apply_template():
        page.click(S["templates_btn"], timeout=t)
        row = S["template_row"].format(template_name=CONFIG["template_name"])
        page.click(row, timeout=t)
        page.click(S["apply_template_btn"], timeout=t)
    step(auditor, "apply lease template", apply_template)

    # 6. Add each signer as a participant (one or two tenants)
    def add_signers():
        page.click(S["signers_btn"], timeout=t)
        for s in signers_of(job):
            page.click(S["add_participant_btn"], timeout=t)
            page.click(S["add_new_contact"], timeout=t)
            page.fill(S["participant_name"], s["name"], timeout=t)
            page.fill(S["participant_email"], s["email"], timeout=t)
            # role select is optional depending on template roles; ignore if absent
            if page.locator(S["participant_role"]).count():
                page.select_option(S["participant_role"], label="Tenant")
            page.click(S["participant_save"], timeout=t)
    step(auditor, "add tenant participants", add_signers)

    # 7. HARD CHECK — every approved signer email must appear on-screen exactly
    #    (normalized, case-insensitive) and no OTHER email may appear.
    def verify_recipients():
        container = page.locator(S["review_email_text"])
        container.wait_for(timeout=t)
        text = container.inner_text()
        approved = {s["email"].strip().lower() for s in signers_of(job)}
        # Exact token match, not a substring test: 'jsmith@x.com' must not be
        # accepted because it is a substring of 'xjsmith@x.com'.
        tokens = {tok.lower() for tok in re.findall(_EMAIL_SCRAPE, text)}
        missing = approved - tokens
        assert not missing, (
            f"Approved signer email(s) {sorted(missing)!r} not found on the "
            f"review screen. Emails on screen: {sorted(tokens)!r}"
        )
        whitelist = {e.strip().lower() for e in CONFIG["signer_whitelist"]}
        unexpected = tokens - approved - whitelist
        assert not unexpected, f"Unexpected emails on review screen: {sorted(unexpected)!r}"
    step(auditor, "verify recipients match approval", verify_recipients)

    # 8. SEND — only reachable if every check above passed.
    #    We flip state['sent_clicked'] the instant the click lands so the
    #    caller can tell a pre-send abort from a post-send anomaly. The
    #    guard also makes a retry re-wait for confirmation without ever
    #    clicking Send a second time.
    def send():
        if not state["sent_clicked"]:
            page.click(S["send_btn"], timeout=t)
            state["sent_clicked"] = True
        page.wait_for_selector(S["sent_confirmation"], timeout=t)
    step(auditor, "send signing invites", send)


def process_job(job_path: Path):
    job = load_job(job_path)
    name = job_path.stem
    recipients = ", ".join(s["email"] for s in signers_of(job))
    notify("info", f"Starting signing for {job['property']} -> {recipients}", name)

    state = {"sent_clicked": False}
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            CONFIG["browser_profile_dir"],
            channel="chrome",
            headless=not CONFIG["headed"],
            viewport={"width": 1440, "height": 900},
        )
        page = ctx.new_page()
        auditor = Auditor(name, page)
        try:
            run_signing(page, job, auditor, state)
        except Exception as e:
            ctx.close()
            if state["sent_clicked"]:
                # Send was clicked but we never saw confirmation. We do NOT
                # know if the invite went out. Leave the job in Sending and
                # flag it -- NEVER move it back to a pickable state, NEVER
                # auto-resend. Exit 2 so the watcher surfaces it.
                notify("error",
                       f"AMBIGUOUS: Send was clicked for {job['property']} "
                       f"({recipients}) but confirmation was not observed ({e}). Job "
                       f"LEFT in Sending — verify in Authentisign before any resend. "
                       f"Audit: {auditor.dir}", name)
                sys.exit(2)
            # Clean pre-send abort: nothing was sent, safe to file to Failed.
            notify("error", f"ABORTED before send — {e}. Screenshots in {auditor.dir}", name)
            Path(CONFIG["failed_dir"]).mkdir(parents=True, exist_ok=True)
            shutil.move(str(job_path), Path(CONFIG["failed_dir"]) / job_path.name)
            sys.exit(1)
        ctx.close()

    # File the paperwork: move PDF + job to Sent. If this fails the lease is
    # ALREADY sent, so we must not pretend it failed -- leave the job in
    # Sending, flag it, and let Jay file it by hand. Exit 3.
    try:
        sent = Path(CONFIG["sent_dir"])
        sent.mkdir(parents=True, exist_ok=True)
        shutil.move(job["pdf_path"], sent / Path(job["pdf_path"]).name)
        shutil.move(str(job_path), sent / job_path.name)
    except Exception as e:
        notify("error",
               f"Lease for {job['property']} WAS SENT to {recipients} but filing to "
               f"Sent failed ({e}). Job left in Sending — file it manually, do NOT "
               f"resend. Audit: {auditor.dir}", name)
        sys.exit(3)

    signer_list = ", ".join(f"{s['name']} <{s['email']}>" for s in signers_of(job))
    notify("success",
           f"Lease for {job['property']} sent to {signer_list}. "
           f"Audit: {auditor.dir}", name)


def setup_profile():
    """Open a headed window on the persistent profile so Jay can log in once."""
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            CONFIG["browser_profile_dir"], channel="chrome", headless=False)
        page = ctx.new_page()
        page.goto(CONFIG["smartmls_url"])
        print("Log into SmartMLS (complete MFA), open Authentisign once, "
              "then close the browser window.")
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        ctx.close()


if __name__ == "__main__":
    if "--setup" in sys.argv:
        setup_profile()
    elif "--job" in sys.argv:
        process_job(Path(sys.argv[sys.argv.index("--job") + 1]))
    else:
        print(__doc__)
