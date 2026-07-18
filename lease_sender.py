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
    # SmartMLS Sign — Signings dashboard. The create button (a stable test-id)
    # is our proof the app is loaded and the session is alive.
    "logged_in_marker":     "[data-testid='signings-create-btn']",
    "signin_with_mls_btn":  "button:has-text('Sign in with Smart MLS')",  # propkit /auth landing

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

    # --- New signing: startup screen (/signings/edit?isStartUp=true) ---
    "new_signing_btn":      "[data-testid='signings-create-btn']",
    "signing_name_input":   "[data-testid='signing-form-signing-details-dialog-edit-name-input']",
    # Reveal the uploader, then set files on the hidden input. Continue is only
    # enabled once at least one document is uploaded.
    "upload_reveal_btn":    "[data-testid='signing-form-signing-upload-file-btn']",
    "upload_input":         "[data-testid='editor-document-uploader-input']",
    "continue_btn":         "[data-testid='signing-form-save-and-continue-btn']",

    # --- Editor: documents ---
    "add_documents_btn":    "button:has-text('+ Add Document(s)')",
    "select_template_btn":  "button:has-text('Select Template')",
    # A template/overlay is chosen by clicking its name (get_by_text, exact) then
    # this Select button applies it.
    "picker_select_btn":    "button:has-text('Select')",
    # Per-document settings gear (its SVG path is the stable handle; the sibling
    # trash icon is path d^='M4.5 5.57' — never click that). Scoped to a doc row
    # at runtime so we act on the right document.
    "doc_gear_svg_path":    "path[d^='M12 15.75']",
    "apply_overlay_item":   "text=Apply Signing Overlay",

    # --- Editor: participants (Signing Flow) ---
    # Applying the overlay pre-creates ONE participant row; edit it for the first
    # signer, then '+ Add Participant' for the rest. Each opens the "Edit
    # Participant" modal. Role is a dropdown of generic role types (Landlord,
    # Tenant, ...) — two Tenant participants become the overlay's Tenant (1) /
    # Tenant (2) by order.
    "add_participant_btn":  "[data-testid='add-role']",
    "edit_participant_btn": "[data-testid='button-edit-participant']",
    "participant_section":  "[data-testid='element-participant']",
    "participant_role":     "[data-testid='role-selector-participant']",   # click to open the list
    "participant_first":    "[data-testid='name-participant'] input",
    "participant_last":     "[data-testid='lastname-participant'] input",
    "participant_email":    "[data-testid='email-participant'] input",
    "participant_type":     "[data-testid='type-participant']",
    "participant_save":     "[data-testid='dialog-prompt-ok-btn']",     # 'Save'
    "participant_cancel":   "[data-testid='dialog-prompt-cancel-btn']",

    # --- Review + send ---
    # Emails render inside the participant sections; we read them back from there.
    "review_email_text":    "[data-testid='element-participant']",
    "send_btn":             "button:has-text('Send Signing')",
    # Post-send the signing flips to a state showing Resend/Withdraw; we also
    # accept an explicit success toast. Confirmed/adjusted on the first live run.
    "sent_confirmation":    "text=/has been sent|successfully sent|Resend signing|Withdraw/i",
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


def _wait_any(page, selectors, timeout_ms):
    """Poll for any of several selectors; return the first that appears, or None
    on timeout. Used because the login flow can land on several surfaces (app
    dashboard vs. Keycloak form) and we don't know which up front."""
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        for sel in selectors:
            try:
                if page.locator(sel).count():
                    return sel
            except Exception:
                pass
        page.wait_for_timeout(500)
    return None


def login_if_needed(page):
    """Ensure the app is loaded and logged in, whatever surface we land on:

      A) Fresh session  -> /signings shows the dashboard directly.
      B) App token stale, SSO cookie good -> bounced to /auth; clicking
         'Sign in with Smart MLS' silently returns to the dashboard.
      C) SSO cookie expired -> that click lands on the Keycloak form; we fill
         the stored credentials, tick 'Remember me' (device trust so MFA stays
         suppressed), and submit.

    Aborts (via assert, handled by step()) with a human-actionable message only
    when a person is genuinely required (no stored creds, or MFA demanded)."""
    t = CONFIG["step_timeout_ms"]
    S = SELECTORS
    dash, user = S["logged_in_marker"], S["sso_username"]

    # Case A: already in?
    if _wait_any(page, [dash], 5_000):
        return

    # Get onto a login surface: click the propkit /auth button once it renders.
    if _wait_any(page, [S["signin_with_mls_btn"]], 10_000):
        try:
            page.click(S["signin_with_mls_btn"], timeout=t)
        except Exception:
            pass

    # Now either the dashboard comes back (Case B, silent SSO) or Keycloak
    # asks for credentials (Case C).
    landed = _wait_any(page, [dash, user], t)
    assert landed, ("Could not reach the SmartMLS Sign dashboard or a login "
                    "form after clicking 'Sign in with Smart MLS'. The app may "
                    "be down or the flow changed.")
    if landed == dash:
        return  # Case B — silent SSO refresh worked

    # Case C — full Keycloak login from stored credentials.
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
    # Success = dashboard appears. MFA prompt instead => device trust lapsed.
    after = _wait_any(page, [dash, S["sso_mfa_marker"]], t)
    assert after and after == dash, (
        "SmartMLS demanded MFA during unattended login (or the dashboard never "
        "loaded) — device trust has lapsed. Run `python lease_sender.py --setup` "
        "at the PC and check 'Remember me' to restore hands-free login.")


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
# UI helpers — the real SmartMLS Sign flow (see SIGN_UI_MAP.md)
# ----------------------------------------------------------------------
def _doc_display_name(pdf_path) -> str:
    """How an uploaded PDF's name renders in the Documents panel (no extension)."""
    return Path(pdf_path).stem


def _split_name(full: str):
    """('First Middle', 'Last') from a full name; last token is the surname."""
    parts = full.split()
    if len(parts) <= 1:
        return (parts[0] if parts else ""), ""
    return " ".join(parts[:-1]), parts[-1]


def _upload_document(page, pdf_path):
    """Reveal the uploader (if needed) and attach a PDF via the hidden input."""
    t = CONFIG["step_timeout_ms"]; S = SELECTORS
    if not page.locator(S["upload_input"]).count():
        page.click(S["upload_reveal_btn"], timeout=t)
        page.wait_for_timeout(800)
    page.set_input_files(S["upload_input"], pdf_path, timeout=t)
    page.wait_for_selector(f"text={_doc_display_name(pdf_path)}", timeout=t)


def _add_uploaded_document(page, pdf_path):
    """Add another uploaded PDF (e.g. the filled Rental Terms Summary) from the
    editor via '+ Add Document(s)'."""
    t = CONFIG["step_timeout_ms"]; S = SELECTORS
    page.click(S["add_documents_btn"], timeout=t)
    page.wait_for_timeout(800)
    # Prefer an explicit upload option if the menu offers one; otherwise the
    # hidden uploader input may already be present.
    for lbl in ("Upload Document(s)", "Upload Document", "Upload"):
        opt = page.get_by_text(lbl, exact=False)
        if opt.count():
            try:
                opt.first.click(timeout=3000)
                break
            except Exception:
                pass
    page.wait_for_timeout(600)
    page.set_input_files(S["upload_input"], pdf_path, timeout=t)
    page.wait_for_selector(f"text={_doc_display_name(pdf_path)}", timeout=t)


def _apply_overlay_to_lease(page, overlay_name):
    """Apply the signature-field overlay onto the uploaded FILLED lease. Called
    while the lease is the ONLY document, so there is exactly one document gear.

    Two 'Select' clicks: the first picks the overlay from the list, which opens a
    field-mapping dialog (Role Options: Tenant (1)/(2)/Landlord + All Fields, all
    checked by default); the second confirms it. The overlay's roles become the
    signing's participant roles."""
    t = CONFIG["step_timeout_ms"]; S = SELECTORS
    gear = page.locator(f"button:has({S['doc_gear_svg_path']})")
    assert gear.count() == 1, (
        f"expected exactly one document gear before applying the overlay, "
        f"found {gear.count()} — apply the overlay before adding other documents")
    gear.first.click(timeout=t)
    page.wait_for_timeout(800)
    page.click(S["apply_overlay_item"], timeout=t)
    page.wait_for_timeout(1500)
    page.get_by_text(overlay_name, exact=True).first.click(timeout=t)
    page.wait_for_timeout(500)
    page.locator(S["picker_select_btn"]).last.click(timeout=t)   # -> field-mapping dialog
    # Confirm the mapping dialog (defaults: all roles + all fields).
    page.wait_for_selector("text=Role Options", timeout=t)
    page.locator(S["picker_select_btn"]).last.click(timeout=t)
    page.wait_for_selector("text=Role Options", state="detached", timeout=t)
    page.wait_for_timeout(1500)


def _add_template_by_name(page, name):
    """Add a premade packet template (its own document) by exact name."""
    t = CONFIG["step_timeout_ms"]; S = SELECTORS
    page.click(S["add_documents_btn"], timeout=t)
    page.wait_for_timeout(800)
    page.click(S["select_template_btn"], timeout=t)
    page.wait_for_timeout(1500)
    # Adding a 2nd+ template pops a "Multiple Template Warning" (Signing Flow
    # formatting is reset) BEFORE the picker opens. Acknowledge it — the sender
    # reconciles participants after all documents are in.
    try:
        if page.get_by_text("Multiple Template Warning", exact=False).count():
            page.get_by_role("button", name="Proceed", exact=True).first.click(timeout=5_000)
            page.wait_for_timeout(1500)
    except Exception:
        pass
    page.get_by_text(name, exact=True).first.click(timeout=t)
    page.wait_for_timeout(500)
    page.click(S["picker_select_btn"], timeout=t)
    page.wait_for_timeout(1500)
    # Some template adds confirm via a generic prompt (dialog-prompt-ok-btn).
    try:
        ok = page.locator("[data-testid='dialog-prompt-ok-btn']")
        if ok.count() and not page.locator(S["participant_section"]).count():
            ok.first.click(timeout=4_000)
            page.wait_for_timeout(1500)
    except Exception:
        pass
    page.wait_for_timeout(1000)


def _fill_participant_dialog(page, sr):
    """Fill the open 'Edit Participant' modal: role (dropdown), first/last name,
    email, type=Signer, then Save."""
    t = CONFIG["step_timeout_ms"]; S = SELECTORS
    first, last = _split_name(sr["name"])
    page.wait_for_selector(S["participant_section"], timeout=t)
    # Role is a dropdown of generic role types. Options render as e.g.
    # "Tenant" or "Tenant (+Add new)" (a suffix appears once a contact exists),
    # so match on the role as a prefix, not an exact string.
    if sr.get("role"):
        page.locator(S["participant_role"]).click(timeout=t)
        page.wait_for_timeout(500)
        role_re = re.compile(rf"^{re.escape(sr['role'])}(\b|\s|\(|$)")
        opt = page.get_by_role("option", name=role_re)
        if opt.count():
            opt.first.click(timeout=5_000)
        else:
            page.locator(f"text=/^{re.escape(sr['role'])}( \\(\\+Add new\\))?$/").last.click(timeout=5_000)
    page.fill(S["participant_first"], first, timeout=t)
    page.fill(S["participant_last"], last, timeout=t)
    if sr.get("email"):
        page.fill(S["participant_email"], sr["email"], timeout=t)
    # Ensure the participant is a Signer (not Reviewer/Distribution).
    try:
        page.locator(S["participant_type"]).get_by_text("Signer", exact=True).first.click(timeout=3_000)
    except Exception:
        pass
    page.click(S["participant_save"], timeout=t)
    page.wait_for_timeout(1200)
    # When the email matches an existing SmartMLS contact, a "Do you want to
    # merge the following contacts?" dialog appears. Keep them separate (No) so
    # the automation never silently mutates Jay's saved contacts.
    try:
        if page.get_by_text("Do you want to merge", exact=False).count():
            page.get_by_role("button", name="No", exact=True).first.click(timeout=5_000)
            page.wait_for_timeout(1000)
    except Exception:
        pass
    page.wait_for_timeout(800)


def add_participants(page, signers):
    """Assign each signer to a participant row. The overlay pre-creates one row
    (edit it for the first signer); '+ Add Participant' opens a fresh modal for
    each of the rest."""
    t = CONFIG["step_timeout_ms"]; S = SELECTORS
    for i, sr in enumerate(signers):
        if i == 0 and page.locator(S["edit_participant_btn"]).count():
            page.locator(S["edit_participant_btn"]).first.click(timeout=t)
        else:
            page.click(S["add_participant_btn"], timeout=t)
        page.wait_for_timeout(1000)
        _fill_participant_dialog(page, sr)


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

    # 2. New signing: name it (drives completion-email correlation, so it must
    #    be set), upload the FILLED lease, then Continue into the editor.
    documents = job.get("documents") or [job["pdf_path"]]
    lease_pdf = job["pdf_path"]

    def start_signing():
        page.click(S["new_signing_btn"], timeout=t)
        page.wait_for_selector(S["signing_name_input"], timeout=t)
        page.fill(S["signing_name_input"], job["signing_name"], timeout=t)
        _upload_document(page, lease_pdf)
        page.click(S["continue_btn"], timeout=t)
        page.wait_for_selector(S["add_documents_btn"], timeout=t)  # editor is up
    step(auditor, "start signing + upload lease", start_signing)

    # 3. Apply the signature overlay onto the filled lease. MUST happen before
    #    any other document is added (so there's exactly one document gear).
    overlay = CONFIG["lease_overlay"].get(job.get("lease_type", ""))

    def apply_overlay():
        assert overlay, f"no lease overlay configured for lease_type {job.get('lease_type')!r}"
        _apply_overlay_to_lease(page, overlay)
    step(auditor, f"apply overlay: {overlay}", apply_overlay)

    # 4. Add the remaining uploaded documents (e.g. filled Rental Terms Summary).
    for doc in documents[1:]:
        step(auditor, f"add document: {Path(doc).name}",
             (lambda d=doc: _add_uploaded_document(page, d)))

    # 5. Add each premade packet template (its own document), one at a time.
    for tpl in CONFIG["packet_templates"]:
        step(auditor, f"add packet template: {tpl}",
             (lambda name=tpl: _add_template_by_name(page, name)))

    # 6. Assign each signer to a participant — landlord person first, then
    #    tenant(s). Two tenants map to the overlay's Tenant (1)/(2) by order.
    step(auditor, "add participants (landlord + tenants)",
         (lambda: add_participants(page, all_signers(job))))

    # 7. HARD CHECK — every approved signer email must appear on-screen exactly
    #    (normalized, case-insensitive) and no OTHER email may appear. Emails
    #    render inside the participant sections.
    def verify_recipients():
        # The Signing Flow rows show name+role but not the email, so read each
        # participant's email back from its edit dialog (then Cancel — no change).
        approved = {s["email"].strip().lower() for s in all_signers(job) if s.get("email")}
        found = set()
        edits = page.locator(S["edit_participant_btn"])
        count = edits.count()
        try:
            for i in range(count):
                edits.nth(i).click(timeout=t)
                # In edit mode the email renders as display text in a contact
                # card (not an input), so scrape it from the open dialog.
                page.wait_for_selector(S["participant_role"], timeout=t)
                page.wait_for_timeout(600)
                dtxt = page.locator("body").inner_text()
                for tok in re.findall(_EMAIL_SCRAPE, dtxt):
                    found.add(tok.lower())
                page.click(S["participant_cancel"], timeout=t)
                page.wait_for_timeout(500)
        except Exception as e:
            # Couldn't read a participant back. On a real send this must block;
            # during a --no-send dry run, warn and let the draft be eyeballed.
            assert CONFIG.get("no_send"), f"could not verify recipients ({e})"
            notify("info", f"DRY RUN — could not read back all recipient emails ({e})",
                   job.get("signing_name", ""))
            return
        missing = approved - found
        assert not missing, (
            f"Approved signer email(s) {sorted(missing)!r} not found among the "
            f"{count} participants. Emails found: {sorted(found)!r}")
        whitelist = {e.strip().lower() for e in CONFIG["signer_whitelist"]}
        unexpected = found - approved - whitelist
        assert not unexpected, f"Unexpected participant email(s): {sorted(unexpected)!r}"
    step(auditor, "verify recipients match approval", verify_recipients)

    # 8. SEND — only reachable if every check above passed. --no-send stops here
    #    (dry run: assemble + verify, leave as a draft to eyeball) so the first
    #    live runs never actually send. We flip state['sent_clicked'] the instant
    #    the click lands so the caller can tell a pre-send abort from a post-send
    #    anomaly, and so a retry re-waits for confirmation without clicking twice.
    if CONFIG.get("no_send"):
        auditor.snap("STOP before send (--no-send)")
        notify("info",
               f"Assembled signing for {job['property']} and stopped BEFORE Send "
               f"(--no-send). Left as a draft in SmartMLS Sign. Audit: {auditor.dir}",
               job.get("signing_name", ""))
        return

    def send():
        if not state["sent_clicked"]:
            page.click(S["send_btn"], timeout=t)
            state["sent_clicked"] = True
            page.wait_for_timeout(1500)
            # As the send commits, a "Save Contact Group? — save these contacts
            # as a signing group for future signings" prompt appears. Decline it.
            try:
                if page.get_by_text("save these contacts as a signing group",
                                    exact=False).count():
                    page.get_by_role("button", name="No", exact=True).first.click(timeout=8_000)
                    page.wait_for_timeout(1500)
            except Exception:
                pass
        page.wait_for_selector(S["sent_confirmation"], timeout=t)
    step(auditor, "send signing", send)


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

    # Dry run: nothing was sent, so leave every file exactly where it is.
    if CONFIG.get("no_send"):
        return

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
    # --no-send   : assemble + verify then STOP before Send (leaves a draft).
    # --skip-packet: don't add the CONFIG['packet_templates'] (core-flow test).
    if "--no-send" in sys.argv:
        CONFIG["no_send"] = True
    if "--skip-packet" in sys.argv:
        CONFIG["packet_templates"] = []
    if "--setup" in sys.argv:
        setup_profile()
    elif "--job" in sys.argv:
        process_job(Path(sys.argv[sys.argv.index("--job") + 1]))
    else:
        print(__doc__)
