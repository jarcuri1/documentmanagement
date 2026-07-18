"""
LeaseAgent — unattended SmartMLS Sign sender
============================================
Runs on the fleet PC. Takes an APPROVED lease job, drives a real Chrome
window into SmartMLS Sign (SmartMLS's built-in e-signature, hosted at
signings.smartmls.propkit.io and reached through SmartMLS SSO), creates a
new signing from the filled lease PDF, adds the tenant(s) as signer(s),
verifies everything on-screen matches the approved job, clicks Send, and
files the audit trail to Dropbox.

This is piece 2 of the pipeline (see HANDOFF_LEASE_AGENT.md). It is invoked
by lease_watcher.py, which has already CLAIMED the job by moving its JSON
into Dropbox\\Leases\\Sending before this script launches. That claim-first
move is what makes double-sends impossible; this script must therefore
never put a job back into a pickable state (Pending) on its own.

The signing is NAMED from job["signing_name"] ("Lease - <property> -
<surname>"). SmartMLS Sign echoes that name into the "eSigning Completed |
<name>" email when signing finishes, which is how the filing agent
(lease_filer) later correlates the returned PDF back to this job -- so the
name must not be left as a template default.

DESIGN RULES (do not soften these):
  1. Script never guesses. Any unexpected page/element -> screenshot,
     abort, notify. No retries past MAX_RETRIES, no creative clicking.
  2. Send is only clicked after every approved signer email matches on
     screen (normalized, case-insensitive) AND no unexpected email appears.
  3. Every step screenshots to the job's Dropbox folder (audit trail).
  4. File placement is one-way. On a pre-send abort the job goes to Failed.
     On any failure AFTER Send was clicked the job is LEFT in Sending and
     flagged for manual review -- a crash must never cause a silent resend
     and must never be mistaken for "never sent".

ONE-TIME SETUP (do this while at the PC):
  1. pip install playwright && playwright install chrome
  2. Log the automation profile in once:
       python lease_sender.py --setup
     A Chrome window opens on the persistent profile at the SmartMLS Sign
     app. Click "Sign in with Smart MLS", complete the SmartMLS login (+ any
     MFA), land on the Signings dashboard, then close the window. Cookies
     persist in BROWSER_PROFILE_DIR.
  3. In SmartMLS Sign -> Templates (Forms), build the lease signature overlays
     (signature / initial / date + tenant fillable blocks) named EXACTLY as in
     CONFIG["lease_overlay"] — one per lease type. Build each supporting-doc
     template (lead disclosure, pamphlet, overview) and list their names in
     CONFIG["packet_templates"]. A signing is assembled by adding these
     templates one at a time.
  4. Capture real selectors: the app's DOM will not match my placeholders.
     Run:
       playwright codegen --user-data-dir="<BROWSER_PROFILE_DIR>" https://signings.smartmls.propkit.io/signings
     Walk through one signing manually; codegen prints the selectors.
     Update the SELECTORS dict below -- it's the ONLY place they live.
  5. Put your own signer email (if you countersign) in
     CONFIG["signer_whitelist"] so the recipient check doesn't abort on it.

PER-LEASE FLOW (wired into the fleet):
  - Fill agent drops:   Dropbox\\Leases\\Pending\\<job>.pdf + <job>.json
  - Supervisor pushes an approval card; you tap Approve in the Samantha app
  - lease_watcher.py consumes the `send` decision, CLAIMS the job
    (Pending -> Sending), then runs:
       python lease_sender.py --job "D:\\...\\Sending\\<slug>.json"

EXIT CODES (read by lease_watcher):
  0  sent and filed to Sent
  1  aborted BEFORE Send -> job filed to Failed
  2  Send was clicked but confirmation not observed -> job LEFT in Sending
  3  sent successfully but filing to Sent failed -> job LEFT in Sending

JOB FILE FORMAT (<job>.json):
{
  "property": "123 Main St Apt 2, Waterbury CT",
  "signing_name": "Lease - 123 Main St Apt 2, Waterbury CT - Smith",
  "signers": [{"name": "John Smith", "email": "jsmith@example.com"}],
  "pdf_path": "D:\\Dropbox\\Dropbox\\Leases\\Sending\\123-main-st-smith.pdf"
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
    "sign_url": "https://signings.smartmls.propkit.io/signings",  # SmartMLS Sign app
    # Exact SmartMLS Sign template (overlay) name per lease type — must match
    # the template names in Templates (Forms) character-for-character. Name your
    # templates exactly these:
    "lease_overlay": {
        "single_family": "Agent automated single_family_lease",
        "multi_family":  "Agent automated multi_family_lease",
    },
    # The other packet documents already exist as SmartMLS Sign templates with
    # their signature/initial spots. A signing is built by adding templates ONE
    # AT A TIME, so list their exact names here, in the order they should be
    # added. Expand by adding a name (and building that template once in Sign).
    # All premade SmartMLS Sign templates, added in this order. NOTE: confirm
    # these strings match the template names in Sign > Templates EXACTLY
    # (character-for-character) — the names below are from Jay's doc list.
    "packet_templates": [
        "1_Wiring Fraud Advisory Notice - eXp Connecticut",
        "protectyourfamily_pamphlet_2026_3 Lead",
        "Disclosure of Information on Lead-Based Paint and/or Lead-Based Paint Hazards (Rentals)",
        "Disclosure of Interest in Property",
    ],
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
    # SmartMLS Sign — Signings dashboard
    "logged_in_marker":     "text=New Signing",            # proves the SSO session is alive
    "signin_with_mls_btn":  "button:has-text('Sign in with Smart MLS')",  # propkit landing page

    # SmartMLS SSO (Keycloak realm 'connectmls') login form. Captured from the
    # real page. Used ONLY for unattended re-login when the persisted session
    # has lapsed — creds come from Windows Credential Manager (see set_login.py),
    # never from a file. 'remember_me' is the device-trust toggle that keeps MFA
    # from re-prompting; we always check it.
    "sso_username":         "#username",
    "sso_password":         "#password",
    "sso_remember_me":      "#rememberMe",
    "sso_submit":           "#kc-login",
    # If device trust has lapsed, Keycloak shows an OTP/MFA step instead of
    # redirecting. We detect it and abort loudly (a human must re-trust).
    "sso_mfa_marker":       "text=/one[- ]time code|verification code|authenticator|otp/i",

    # New signing
    "new_signing_btn":      "button:has-text('New Signing')",
    "signing_name_input":   "input[name='name']",
    "create_btn":           "button:has-text('Create')",

    # Add document (upload the filled lease PDF)
    "upload_input":         "input[type='file']",           # direct file upload path
    "doc_uploaded_marker":  ".document-uploaded",           # row/thumbnail confirming upload

    # Apply the saved signature-field template (Templates > Forms)
    "templates_btn":        "button:has-text('Apply Template')",
    "template_row":         "text={template_name}",         # filled at runtime
    "apply_template_btn":   "button:has-text('Apply')",

    # Signers (landlord person + one or two tenants). Reuse existing contacts.
    "add_signer_btn":       "button:has-text('Add Signer')",
    "contact_search":       "input[placeholder='Search contacts']",  # existing-contact search
    "contact_result":       "text={name}",                  # a matching existing contact row
    "signer_name":          "input[name='signerName']",
    "signer_email":         "input[name='signerEmail']",
    "signer_role":          "select[name='role']",          # optional; ignored if absent
    "signer_save":          "button:has-text('Save')",
    # Delete the unused 2nd-tenant role + its fields on a single-tenant signing
    # (the overlay is built for two tenants; an unassigned role blocks Send).
    "remove_second_tenant": "[data-role='Tenant 2'] button:has-text('Remove')",

    # Review + send
    "review_email_text":    ".signers-list",                # container we read emails back from
    "send_btn":             "button:has-text('Send')",
    "sent_confirmation":    "text=has been sent",           # confirmation toast/text
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


def get_credentials():
    """Read SmartMLS creds from Windows Credential Manager (stored by
    set_login.py). Returns (username, password), or (None, None) if the vault
    is empty or keyring isn't available. The password is never logged or
    written to disk — it lives only in the OS credential store and in memory
    for the moment of login."""
    try:
        import keyring
    except ImportError:
        return None, None
    try:
        username = keyring.get_password("LeaseAgent-SmartMLS", "__username__")
        if not username:
            return None, None
        password = keyring.get_password("LeaseAgent-SmartMLS", username)
        return (username, password) if password else (None, None)
    except Exception:
        return None, None


def login_if_needed(page):
    """Ensure the SSO session is live. If it has lapsed, log back in from the
    stored credentials, checking 'Remember me' so device trust persists and MFA
    stays suppressed. Raises (via assert / PWTimeout, handled by step()) with a
    clear, human-actionable message when a person is genuinely required."""
    t = CONFIG["step_timeout_ms"]
    S = SELECTORS
    # Already on the dashboard? The persisted session is still good.
    try:
        page.wait_for_selector(S["logged_in_marker"], timeout=5_000)
        return
    except PWTimeout:
        pass
    # Not logged in. Get to the Keycloak form (propkit landing has a button).
    if page.locator(S["signin_with_mls_btn"]).count():
        page.click(S["signin_with_mls_btn"], timeout=t)
    page.wait_for_selector(S["sso_username"], timeout=t)  # PWTimeout -> step() aborts
    username, password = get_credentials()
    assert username and password, (
        "SmartMLS session expired and no stored credentials were found. Run "
        "`python set_login.py` at the fleet PC to save them (or "
        "`python lease_sender.py --setup` to log in by hand once).")
    page.fill(S["sso_username"], username, timeout=t)
    page.fill(S["sso_password"], password, timeout=t)
    if page.locator(S["sso_remember_me"]).count():
        try:
            page.check(S["sso_remember_me"], timeout=5_000)
        except Exception:
            pass  # non-fatal; login still proceeds
    page.click(S["sso_submit"], timeout=t)
    # Success = dashboard reappears. If MFA is demanded instead, device trust
    # has lapsed and only a human can restore it — abort loudly, don't hang.
    try:
        page.wait_for_selector(S["logged_in_marker"], timeout=t)
    except PWTimeout:
        assert not page.locator(S["sso_mfa_marker"]).count(), (
            "SmartMLS demanded MFA during unattended login — device trust has "
            "lapsed. Run `python lease_sender.py --setup` at the PC and check "
            "'Remember me' to restore hands-free login.")
        raise  # genuine timeout: let step() screenshot + abort


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
    """The lease's TENANT signers. Supports the multi-signer contract and the
    legacy single-tenant shape so older job files still work."""
    if job.get("signers"):
        return job["signers"]
    if job.get("tenant_name") and job.get("tenant_email"):
        return [{"name": job["tenant_name"], "email": job["tenant_email"]}]
    return []


def all_signers(job: dict) -> list:
    """Everyone who signs, in order: the landlord person first (the individual
    signing on the owner's behalf — NOT the owner LLC), then the tenant(s).
    Each is {name, email, role}."""
    out = []
    ls = job.get("landlord_signer")
    if ls and ls.get("name"):
        out.append({"name": ls["name"], "email": ls.get("email", ""), "role": "Landlord"})
    for s in signers_of(job):
        out.append({"name": s["name"], "email": s["email"], "role": "Tenant"})
    return out


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
    ls = job.get("landlord_signer")
    if ls and ls.get("name"):
        # A landlord signer must carry an email so the recipient check can
        # confirm it on the review screen (otherwise it reads as unexpected).
        if not ls.get("email") or not re.match(rf"^{_EMAIL_TOKEN}$", ls["email"]):
            raise ValueError(f"Landlord signer needs a valid email: {ls!r}")
    if not Path(job["pdf_path"]).exists():
        raise ValueError(f"Lease PDF not found: {job['pdf_path']}")
    for doc in (job.get("documents") or []):
        if not Path(doc).exists():
            raise ValueError(f"Signing packet document not found: {doc}")
    return job


# ----------------------------------------------------------------------
# The signing routine
# ----------------------------------------------------------------------
def run_signing(page, job: dict, auditor: Auditor, state: dict):
    t = CONFIG["step_timeout_ms"]
    S = SELECTORS

    # 1. Open SmartMLS Sign; confirm the SSO session is alive (never type creds)
    def goto_app():
        page.goto(CONFIG["sign_url"], timeout=t)
        login_if_needed(page)   # unattended re-login if the session has lapsed
        page.wait_for_selector(S["logged_in_marker"], timeout=t)
    step(auditor, "smartmls sign dashboard (session alive)", goto_app)

    # 2. New signing, NAMED from the job (the name drives completion-email
    #    correlation, so it must be set — never left as a template default)
    def create_signing():
        page.click(S["new_signing_btn"], timeout=t)
        page.fill(S["signing_name_input"], job["signing_name"], timeout=t)
        page.click(S["create_btn"], timeout=t)
    step(auditor, "create signing", create_signing)

    # 3. Upload the filled lease PDF (plus any static-PDF docs in documents).
    #    Supporting docs that already exist as Smart Sign templates are ADDED
    #    in step 5, not uploaded.
    documents = job.get("documents") or [job["pdf_path"]]

    def add_documents():
        for doc in documents:
            page.set_input_files(S["upload_input"], doc, timeout=t)
            page.wait_for_selector(S["doc_uploaded_marker"], timeout=t)
    step(auditor, f"upload {len(documents)} document(s)", add_documents)

    # 4 + 5. Assemble the packet by adding templates ONE AT A TIME (Smart Sign
    #        requires this): first the lease signature overlay, then each
    #        supporting-doc template by name, in order.
    def apply_template_by_name(tpl_name):
        page.click(S["templates_btn"], timeout=t)
        page.click(S["template_row"].format(template_name=tpl_name), timeout=t)
        page.click(S["apply_template_btn"], timeout=t)
        page.wait_for_selector(S["doc_uploaded_marker"], timeout=t)

    overlay = CONFIG["lease_overlay"].get(job.get("lease_type", ""))

    def apply_lease_overlay():
        assert overlay, f"no lease overlay configured for lease_type {job.get('lease_type')!r}"
        apply_template_by_name(overlay)
    step(auditor, "apply lease overlay", apply_lease_overlay)
    for tpl in CONFIG["packet_templates"]:
        step(auditor, f"add template: {tpl}",
             (lambda name=tpl: apply_template_by_name(name)))

    # 6. Add each signer — landlord person + tenant(s). Reuse an existing
    #    SmartMLS Sign contact when the name already exists (so a repeat
    #    landlord like Matt isn't re-entered); type a new contact in full only
    #    when there's no match.
    def add_one_signer(sr):
        page.click(S["add_signer_btn"], timeout=t)
        matched = False
        if page.locator(S["contact_search"]).count():
            page.fill(S["contact_search"], sr["name"], timeout=t)
            result = page.locator(S["contact_result"].format(name=sr["name"]))
            if result.count():
                result.first.click(timeout=t)   # select the existing contact
                matched = True
        if not matched:
            page.fill(S["signer_name"], sr["name"], timeout=t)
            if sr.get("email"):
                page.fill(S["signer_email"], sr["email"], timeout=t)
        if page.locator(S["signer_role"]).count():
            page.select_option(S["signer_role"], label=sr["role"])
        page.click(S["signer_save"], timeout=t)

    def add_signers():
        for sr in all_signers(job):
            add_one_signer(sr)
    step(auditor, "add signers (landlord + tenants)", add_signers)

    # 6b. Single tenant -> remove the overlay's unused 2nd-tenant role and its
    #     fields, or SmartMLS Sign refuses to send (unassigned fields).
    if len(signers_of(job)) < 2:
        def remove_second_tenant():
            page.click(S["remove_second_tenant"], timeout=t)
        step(auditor, "remove unused 2nd tenant slot", remove_second_tenant)

    # 7. HARD CHECK — every approved signer email must appear on-screen exactly
    #    (normalized, case-insensitive) and no OTHER email may appear.
    def verify_recipients():
        container = page.locator(S["review_email_text"])
        container.wait_for(timeout=t)
        text = container.inner_text()
        approved = {s["email"].strip().lower() for s in all_signers(job) if s.get("email")}
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
                       f"LEFT in Sending — verify in SmartMLS Sign before any resend. "
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
        page.goto(CONFIG["sign_url"])
        print("Click 'Sign in with Smart MLS', complete the SmartMLS login "
              "(+ any MFA), land on the Signings dashboard, then close the window.")
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
