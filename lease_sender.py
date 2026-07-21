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
    # NOTE: "1_Wiring Fraud Advisory Notice - eXp Connecticut" is OUT of the
    # lease packet (Jay's call, 2026-07-19): it's a sales doc whose Seller
    # roles don't belong on a lease. If a template ever injects roles we don't
    # use, they're excluded at add time via the Role Options dialog (see
    # template_keep_roles) or removed via the Distribution-party trick
    # (_remove_row_via_distribution) — both taught by Jay.
    # NOTE: 'Disclosure of Interest in Property' is NOT a template anymore —
    # its fill-in boxes are canvas-drawn and reject synthetic input, so
    # lease_forms.fill_disclosure_of_interest pre-fills the PDF (address +
    # licensee initials per property tree) and it rides as an UPLOAD.
    # Lead disclosure: the Sign template was the SALES version — replaced by
    # the pre-filled RENTALS PDF upload (lease_forms.fill_lead_disclosure_rentals)
    # + the 'agent automated lead_rentals' overlay for landlord/tenant fields.
    "packet_templates": [
        "protectyourfamily_pamphlet_2026_3 Lead",
    ],
    # Role base-names to KEEP when adding a packet template (checkboxes in the
    # template's Role Options dialog). Anything else (Buyer, Seller, Licensee,
    # ...) is unchecked so it never creates a participant row or orphan fields.
    "template_keep_roles": ["Tenant", "Landlord", "Listing Agent"],
    # The listing agent signs wherever a template carries that role (e.g. the
    # lead-paint disclosure's agent certification) — always Jason Arcuri.
    # Jay is always the listing agent; WHICH inbox he signs from depends on
    # who the landlord is (his rule, 2026-07-21): Jay-as-landlord -> both
    # roles at realtorarcuri (shared email + phones); Matt-as-landlord ->
    # Matt at realtorarcuri, Jay-as-agent at premiopropertymanagement.
    "listing_agent": {"name": "Jason Arcuri", "phone": "2039107602",
                      "email_when_self": "realtorarcuri@gmail.com",
                      "email_when_other": "premiopropertymanagement@gmail.com"},
    # Checkboxes to tick on the Disclosure of Interest, by management tree
    # (Jay 2026-07-19): owned properties -> '2: Himself or herself' + item 3;
    # Premio-managed -> item 3 only. Tree comes from the job or the folder map;
    # unknown defaults to 'personal' (most leases are own properties, and the
    # approval card gates every send anyway).
    # Overlays applied to UPLOADED packet documents (matched by filename
    # substring). The disclosure's Tenant/Landlord acknowledgment fields come
    # from this overlay; its address/initials/licensee signature are already
    # stamped on the PDF by lease_forms.
    "doc_overlays": {
        "disclosure-of-interest": "agnet disclosure_of_interest",
        "lead-disclosure-rentals": "Agent Disclosure of Information on Lead-Based Paint and_or Lead-Based Paint Hazards (Rentals)",
    },
    "disclosure_checks": {
        "personal": ["item2_himself", "item3"],
        "premio":   ["item3"],
    },
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


def listing_agent_for(job: dict) -> dict:
    """The listing agent (always Jason) with the inbox chosen by WHO the
    landlord signer is: Jay himself -> realtorarcuri on both roles
    (same_person=True, phones fill); anyone else (Matt, a client) ->
    premiopropertymanagement for the agent."""
    la = dict(CONFIG.get("listing_agent") or {})
    lname = ((job.get("landlord_signer") or {}).get("name") or "").lower()
    self_signs = ("jason" in lname) or ("jay" in lname.split())
    la["email"] = la.get("email_when_self") if self_signs else la.get("email_when_other")
    la["same_person"] = self_signs
    return la


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
    emails = [s["email"].strip().lower() for s in signers]
    if ls and ls.get("email"):
        emails.append(ls["email"].strip().lower())
    la_dyn = listing_agent_for(job)
    la_email = la_dyn.get("email") or ""
    if la_email:
        emails.append(la_email.strip().lower())   # the listing agent signs too
    dupes = sorted({e for e in emails if emails.count(e) > 1})
    la = la_email.strip().lower()
    # Jay may be agent AND landlord on the same signing (SmartMLS accepts the
    # shared email when both participants carry a phone; reconcile fills it).
    if la and ls and (ls.get("email") or "").strip().lower() == la:
        dupes = [d for d in dupes if d != la]
    if dupes:
        raise ValueError(
            f"Signers share an email address {dupes!r}. SmartMLS Sign requires a "
            f"phone number per participant when emails repeat — give each signer "
            f"a distinct email in the intake (only Jay-as-agent + Jay-as-landlord "
            f"may share).")
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


def _doc_listed(page, name, timeout_ms):
    """Wait until `name` appears as a row in the Documents PANEL (not a modal).
    This is the only trustworthy postcondition that a document actually
    attached — matching the filename anywhere on screen also matches the
    upload modal and lets silent failures through."""
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        listed = page.evaluate(
            "(n)=>[...document.querySelectorAll('.group_document')]"
            ".some(e=>(e.innerText||'').includes(n))", name)
        if listed:
            return True
        page.wait_for_timeout(500)
    return False


def _add_uploaded_document(page, pdf_path):
    """Add another uploaded PDF (e.g. the filled Rental Terms Summary) from the
    editor via '+ Add Document(s)'. The upload modal shows the file with a
    check mark once received; it then needs its confirm/close — and the doc
    MUST then appear in the Documents panel (hard postcondition)."""
    t = CONFIG["step_timeout_ms"]; S = SELECTORS
    name = _doc_display_name(pdf_path)
    _dismiss_stray_dialog(page)
    page.click(S["add_documents_btn"], timeout=t)
    page.wait_for_timeout(800)
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
    # The upload modal attaches the file and closes ITSELF (~6s, verified
    # live). Touch nothing — clicking or Escaping mid-upload discards it.
    # The doc appearing in the Documents panel is the completion signal.
    assert _doc_listed(page, name, t), (
        f"uploaded document {name!r} never appeared in the Documents panel — "
        f"the upload may have been interrupted")


def _click_doc_gear(page, doc_stem):
    """Click the settings gear of the Documents-panel row whose text contains
    `doc_stem`. Falls back to 'the only gear' when a single document exists."""
    t = CONFIG["step_timeout_ms"]; S = SELECTORS
    gears = page.locator(f"button:has({S['doc_gear_svg_path']})")
    if gears.count() == 1:
        gears.first.click(timeout=t)
        return
    clicked = page.evaluate(
        """([stem, pathPrefix])=>{
          const gears=[...document.querySelectorAll('button')].filter(b=>{
            const p=b.querySelector('path');
            return p && (p.getAttribute('d')||'').startsWith(pathPrefix);});
          for(const g of gears){
            let n=g;
            for(let i=0;i<6;i++){
              n=n.parentElement; if(!n) break;
              const t=(n.innerText||'').trim();
              if(t.length<120){
                if(t.toLowerCase().includes(stem.toLowerCase())){ g.click(); return true; }
              } else break;
            }
          }
          return false;
        }""", [doc_stem[:25], "M12 15.75"])
    assert clicked, f"no document gear found for row containing {doc_stem!r}"


def _apply_overlay_to_doc(page, doc_stem, overlay_name, exclude_roles=()):
    """Apply a signature-field overlay onto an uploaded document (the lease,
    the disclosure, ...) via its row gear -> Apply Signing Overlay.

    Two 'Select' clicks: the first picks the overlay from the list, which opens a
    field-mapping dialog (Role Options + Field Options, all checked by default);
    the second confirms it. The overlay's roles become participant roles."""
    t = CONFIG["step_timeout_ms"]; S = SELECTORS
    _click_doc_gear(page, doc_stem)
    page.wait_for_timeout(800)
    page.click(S["apply_overlay_item"], timeout=t)
    page.wait_for_timeout(1500)
    # When the signing already has fields (e.g. the lease overlay), Sign asks
    # 'Clear Fields — Do you want to clear existing fields?'. NO — keep them;
    # this overlay only adds the new document's fields.
    try:
        if page.get_by_text("Do you want to clear existing fields", exact=False).count():
            page.get_by_role("button", name="No", exact=True).first.click(timeout=5_000)
            page.wait_for_timeout(1500)
    except Exception:
        pass
    _click_template_row(page, overlay_name, t)   # tolerant: search + substring
    page.wait_for_timeout(500)
    page.locator(S["picker_select_btn"]).last.click(timeout=t)   # -> field-mapping dialog
    # Mapping dialog: keep every overlay role, EXCEPT drop the unused second
    # tenant slot on a single-tenant lease (its fields would orphan and block
    # Send), then confirm.
    page.wait_for_selector("text=Role Options", timeout=t)
    page.wait_for_timeout(500)
    if exclude_roles:
        dropped = _uncheck_mapping_roles(page, keep_bases=None, exclude_exact=exclude_roles)
        if dropped:
            print(f"    excluded overlay roles: {dropped}")
    page.locator(S["picker_select_btn"]).last.click(timeout=t)
    page.wait_for_selector("text=Role Options", state="detached", timeout=t)
    page.wait_for_timeout(1500)


def _apply_overlay_to_lease(page, overlay_name, exclude_roles=()):
    # Back-compat alias: the lease is the only document when its overlay is
    # applied, so the single-gear fallback in _click_doc_gear handles it.
    _apply_overlay_to_doc(page, "", overlay_name, exclude_roles)


def _uncheck_mapping_roles(page, keep_bases=None, exclude_exact=()):
    """In an open Role Options mapping dialog, uncheck role rows so they never
    create participant rows or orphan fields (Jay's method). A role stays
    checked when its base name is in keep_bases (None = keep all) and it isn't
    in exclude_exact. Returns the roles unchecked."""
    return page.evaluate(
        """([keep, exclude])=>{
          const out=[];
          const boxes=[...document.querySelectorAll("input[type=checkbox]")];
          for(const box of boxes){
            let row=box;
            for(let i=0;i<5;i++){
              row=row.parentElement; if(!row) break;
              const t=(row.innerText||'').trim();
              if(t && t.length<40){
                const role=t.split('\\n')[0].trim();
                const base=role.replace(/\\s*\\(\\d+\\)$/,'').trim();
                const isRole=/^[A-Z][A-Za-z' ]+(\\s*\\(\\d+\\))?$/.test(role)
                             && !/^(All Fields|Signature and Initial Fields Only|Other Fields|Form Only|Text Box|Checkbox|Radio|Dropdown|Date|Signature|Initials|Full Name|Email|Attachment|Stamp|Page)/i.test(role);
                const drop=isRole && box.checked &&
                           ((keep!==null && !keep.includes(base)) || exclude.includes(role));
                if(drop){ box.click(); out.push(role); }
                break;
              }
              if(t && t.length>=40) break;
            }
          }
          return out;
        }""", [keep_bases if keep_bases is not None else None, list(exclude_exact)])


def _click_template_row(page, name, timeout):
    """Click a template row in the picker by name. The picker has two views
    toggled by 'My Favorites' and a Search box; templates can live in either
    view, so: search by name, look in the current view, then toggle and retry."""
    def _row():
        # Only the card TITLE (div.text-4.font-semibold) is the click target.
        # get_by_text can resolve to a text-3 subtitle elsewhere in the dialog,
        # which sits under another layer and times out the click.
        row = page.locator("div.text-4.font-semibold").filter(has_text=name[:40])
        return row.first if row.count() else None

    search = page.locator("input[placeholder='Search...']")
    for attempt in range(2):
        if search.count():
            search.first.fill(name[:40], timeout=timeout)
            page.wait_for_timeout(1200)
        row = _row()
        if row:
            row.scroll_into_view_if_needed()
            row.click(timeout=timeout)
            return
        # not in this view — toggle Favorites/All and look again
        toggle = page.get_by_text("My Favorites", exact=True)
        if attempt == 0 and toggle.count():
            toggle.first.click(timeout=timeout)
            page.wait_for_timeout(1500)
    raise AssertionError(f"template not found in picker (both views): {name!r}")


def _dismiss_stray_dialog(page, attempts=10):
    """Close any leftover modal (template preview, prompt, ...) so the next
    editor click isn't intercepted. Escapes repeatedly until the dialog layer
    is actually gone — the post-template-add preview can take a few."""
    for _ in range(attempts):
        if not page.locator("div[class*='z-dialog']").count():
            return
        page.keyboard.press("Escape")
        page.wait_for_timeout(700)


def _add_template_by_name(page, name):
    """Add a premade packet template (its own document) by exact name."""
    t = CONFIG["step_timeout_ms"]; S = SELECTORS
    _dismiss_stray_dialog(page)
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
    _click_template_row(page, name, t)
    page.wait_for_timeout(500)
    page.locator(S["picker_select_btn"]).last.click(timeout=t)
    # After the picker's Select, EITHER the Role Options / field-mapping
    # dialog opens (roles to import -> uncheck the junk, confirm with its own
    # Select) OR the template attaches directly (no new roles to map). Accept
    # both; retry the picker Select once if neither happened.
    deadline = time.time() + t / 1000.0
    retried = False
    while True:
        if page.get_by_text("Role Options", exact=False).count():
            page.wait_for_timeout(800)
            excluded = _uncheck_mapping_roles(page, keep_bases=CONFIG.get("template_keep_roles"))
            if excluded:
                print(f"    excluded template roles: {excluded}")
            page.wait_for_timeout(600)
            page.locator(S["picker_select_btn"]).last.click(timeout=t)
            break
        if _doc_listed(page, name[:25], 1):
            break
        if time.time() > deadline:
            raise StepFailure(f"template {name!r}: neither the mapping dialog nor "
                              f"the attached document appeared after Select")
        if not retried and time.time() > deadline - t / 2000.0                 and page.locator("input[placeholder='Search...']").count():
            page.locator(S["picker_select_btn"]).last.click(timeout=t)
            retried = True
        page.wait_for_timeout(700)
    page.wait_for_timeout(1500)
    assert _doc_listed(page, name[:25], t), (
        f"template {name!r} never appeared in the Documents panel — its "
        f"mapping dialog was likely dismissed instead of confirmed")


def _decline_contact_merge(page):
    """After a participant save, Sign may ask 'Do you want to merge the
    following contacts?'. Always decline — the automation must never silently
    mutate Jay's saved Contacts. (Escape is the verified decline path.)"""
    try:
        if page.get_by_text("Do you want to merge", exact=False).count():
            no = page.get_by_role("button", name="No", exact=True)
            if no.count():
                no.first.click(timeout=4_000)
            else:
                page.keyboard.press("Escape")
            page.wait_for_timeout(1000)
    except Exception:
        pass


def _fill_participant_dialog(page, sr, pick_role=True):
    """Fill the open 'Edit Participant' modal: role (combobox — verified model:
    picking base 'Tenant' creates the numbered instance 'Tenant (1)', a second
    pick creates 'Tenant (2)', which is what binds the overlay's role fields),
    first/last name, email, type=Signer, then Save."""
    t = CONFIG["step_timeout_ms"]; S = SELECTORS
    first, last = _split_name(sr["name"])
    page.wait_for_selector(S["participant_section"], timeout=t)
    if pick_role and sr.get("role"):
        page.locator(S["participant_role"]).click(timeout=t)
        page.wait_for_timeout(800)
        base = sr["role"]
        opts = page.locator("[role=option], li")
        picked = False
        for pattern in (base, f"{base} (+Add new)"):
            cand = opts.filter(has_text=pattern)
            if cand.count():
                cand.first.click(timeout=5_000)
                picked = True
                break
        assert picked, f"role option {base!r} not found in the participant role list"
        page.wait_for_timeout(600)
    # The name fields are contact-autocomplete comboboxes: typing opens a
    # suggestions dropdown, and any later click can accidentally select a
    # contact into a spare row. Tab out of each field (Jay's manual pattern)
    # to commit the text and close the dropdown before the next action.
    page.fill(S["participant_first"], first, timeout=t)
    page.locator(S["participant_first"]).press("Tab")
    page.wait_for_timeout(300)
    page.fill(S["participant_last"], last, timeout=t)
    page.locator(S["participant_last"]).press("Tab")
    page.wait_for_timeout(300)
    if sr.get("email"):
        page.fill(S["participant_email"], sr["email"], timeout=t)
        page.locator(S["participant_email"]).press("Tab")
        page.wait_for_timeout(300)
    if sr.get("phone"):
        # Phone is required by Sign when two participants share an email
        # (Jay as agent + landlord). Field selector is best-effort; a miss
        # surfaces at Send as the phone-required toast (fail-visible).
        for psel in ("[data-testid='element-participant'] input[type='tel']",
                     "[data-testid='phone-participant'] input"):
            ph = page.locator(psel)
            if ph.count():
                ph.first.fill(sr["phone"], timeout=5_000)
                ph.first.press("Tab")
                page.wait_for_timeout(300)
                break
    # Ensure the participant is a Signer (not Reviewer/Distribution).
    try:
        page.locator(S["participant_type"]).get_by_text("Signer", exact=True).first.click(timeout=3_000)
    except Exception:
        pass
    page.click(S["participant_save"], timeout=t)
    page.wait_for_timeout(1500)
    _decline_contact_merge(page)
    _dismiss_stray_dialog(page)
    page.wait_for_timeout(500)


def _participant_edit_index(page, row_label, exact=False):
    """Index of the edit button whose Signing Flow row matches `row_label`.
    exact=True matches the row's FIRST LINE exactly (needed because every row
    also carries a 'Signer' subtitle, so substring matching is ambiguous).
    -1 if absent."""
    return page.evaluate(
        """([target, exact])=>{
          const SEL="[data-testid='button-edit-participant']";
          const btns=[...document.querySelectorAll(SEL)];
          const rowOf=(b)=>{  // climb while parent still holds ONLY this button,
                              // stopping at the stage container
            let n=b;
            while(n.parentElement
                  && n.parentElement.querySelectorAll(SEL).length===1
                  && !n.parentElement.querySelector("[data-testid='add-role']")
                  && !(n.parentElement.matches && n.parentElement.matches("[data-testid='stage-item']"))){
              n=n.parentElement;
            }
            return n;
          };
          return btns.findIndex(b=>{
            const t=(rowOf(b).innerText||'').trim();
            const first=t.split('\\n')[0].trim();
            return exact ? first===target : t.includes(target);
          });
        }""", [row_label, exact])


def _participant_row_labels(page):
    """First line of each participant row in the Signing Flow, in edit-button
    order: 'Test Tenant One (Tenant (1))' for an assigned row, or a bare role
    like 'Tenant (1)' / 'Landlord (2)' / 'Signer' for an unassigned one."""
    return page.evaluate(
        """()=>{
          const SEL="[data-testid='button-edit-participant']";
          const btns=[...document.querySelectorAll(SEL)];
          const rowOf=(b)=>{
            let n=b;
            while(n.parentElement
                  && n.parentElement.querySelectorAll(SEL).length===1
                  && !n.parentElement.querySelector("[data-testid='add-role']")
                  && !(n.parentElement.matches && n.parentElement.matches("[data-testid='stage-item']"))){
              n=n.parentElement;
            }
            return n;
          };
          return btns.map(b=>((rowOf(b).innerText||'').trim().split('\\n')[0]||'').trim());
        }""")


def _remove_row_via_distribution(page, row_label):
    """Remove a leftover participant row using Jay's method: the stage rows
    have no delete control, but flipping the participant's Type to
    'Distribution' moves it to the Distribution Party section, where each row
    DOES have a trash icon. Edit -> Type Distribution -> Save -> trash."""
    t = CONFIG["step_timeout_ms"]; S = SELECTORS
    idx = _participant_edit_index(page, row_label, exact=True)
    assert idx >= 0, f"participant row {row_label!r} not found for removal"
    page.locator(S["edit_participant_btn"]).nth(idx).click(timeout=t)
    page.wait_for_selector(S["participant_section"], timeout=t)
    page.wait_for_timeout(800)
    page.locator(S["participant_type"]).get_by_text("Distribution", exact=True).first.click(timeout=t)
    page.wait_for_timeout(500)
    page.click(S["participant_save"], timeout=t)
    page.wait_for_timeout(1500)
    _decline_contact_merge(page)
    _dismiss_stray_dialog(page)
    # Now trash it from the Distribution Party section.
    deleted = page.evaluate(
        """(label)=>{
          const trashes=[...document.querySelectorAll("button:has(path)")]
            .filter(b=>{const p=b.querySelector('path');
                        return p && (p.getAttribute('d')||'').startsWith('M4.5 5.57');});
          const inDist=[];
          for(const b of trashes){
            let n=b, rowText='';
            for(let i=0;i<7;i++){
              n=n.parentElement; if(!n) break;
              const t=(n.innerText||'').trim();
              if(t.length<80){ rowText=t; break; }
              if(t.length>=80) break;
            }
            if(rowText.includes('Distribution')) inDist.push({b, rowText});
          }
          const hit=inDist.find(x=>x.rowText.includes(label));
          if(hit){ hit.b.click(); return true; }
          // the converted blank row may render with an empty label — if the
          // Distribution section holds exactly one row, that's ours.
          if(inDist.length===1){ inDist[0].b.click(); return true; }
          return false;
        }""", row_label)
    assert deleted, (f"could not find the Distribution-party trash for "
                     f"{row_label!r} after converting it")
    page.wait_for_timeout(1000)
    try:
        ok = page.locator("[data-testid='dialog-prompt-ok-btn']")
        if ok.count():
            ok.first.click(timeout=4_000)
    except Exception:
        pass
    page.wait_for_timeout(1200)
    _dismiss_stray_dialog(page)


# Template-injected sale roles that carry no lease signer. Any Signing Flow row
# whose label matches gets deleted during reconciliation.
_LEFTOVER_ROLE_RE = re.compile(r"^(Seller|Buyer)(\s*\(\d+\))?$|^Landlord\s*\((?!1\))\d+\)$")


def _reconcile_participants(page, job):
    """Assign the lease's real signers to participant rows and delete the
    leftover roles the packet templates injected.

    Verified model (live, 2026-07-19): the overlay leaves ONE blank 'Signer'
    row; packet templates inject their own numbered role rows (the wiring
    advisory: Landlord (1)/(2), Seller (1)/(2)). Picking base role 'Tenant'
    creates instance 'Tenant (1)' and binds that role's overlay fields; a
    second pick creates 'Tenant (2)'. The landlord signer goes into the
    existing 'Landlord (1)' (or 'Landlord') row; every other injected sale
    role is deleted."""
    t = CONFIG["step_timeout_ms"]; S = SELECTORS
    tenants = signers_of(job)
    ls = job.get("landlord_signer") or {}
    _dismiss_stray_dialog(page)   # the last template add leaves its preview open

    def edit_row_exact(label):
        idx = _participant_edit_index(page, label, exact=True)
        if idx < 0:
            return False
        page.locator(S["edit_participant_btn"]).nth(idx).click(timeout=t)
        page.wait_for_timeout(1000)
        return True

    # 1. Tenants — tenant i belongs to role instance 'Tenant (i+1)' (the
    #    overlay's fields are bound to those instances). FILL an existing
    #    tenant row when a template already created one; a template's bare
    #    'Tenant' row IS slot 1 (Sign renumbers the family to 'Tenant (1)'
    #    the moment a second instance appears). Creating instances blindly
    #    shifts the numbering and misbinds every tenant field.
    for i, tenant in enumerate(tenants):
        sr = {"name": tenant["name"], "email": tenant["email"], "role": "Tenant"}
        targets = [f"Tenant ({i + 1})"] + (["Tenant"] if i == 0 else [])
        for lbl in targets:
            if edit_row_exact(lbl):
                _fill_participant_dialog(page, sr, pick_role=False)
                break
        else:
            # No pre-made row: reuse the blank overlay row or add a fresh
            # participant, picking the base role (next free instance).
            if not edit_row_exact("Signer"):
                page.click(S["add_participant_btn"], timeout=t)
                page.wait_for_timeout(1000)
            _fill_participant_dialog(page, sr)
        labels = _participant_row_labels(page)
        ok = (f"{tenant['name']} (Tenant ({i + 1}))" in labels
              or (i == 0 and f"{tenant['name']} (Tenant)" in labels))
        assert ok, (f"tenant {i + 1} did not land in its role slot; rows now: {labels!r}")

    # Final binding check: with all tenants placed, the family is numbered and
    # each tenant must own their exact instance (fail closed on any drift).
    labels = _participant_row_labels(page)
    for i, tenant in enumerate(tenants):
        want = f"{tenant['name']} (Tenant ({i + 1}))"
        solo_ok = len(tenants) == 1 and f"{tenant['name']} (Tenant)" in labels
        assert want in labels or solo_ok, (
            f"after placement, tenant {i + 1} is not bound to Tenant ({i + 1}); "
            f"rows: {labels!r}")

    # 2. Landlord signer into the existing Landlord row (or a new one).
    la_cfg = listing_agent_for(job)
    same_person = la_cfg.get("same_person", False)
    if ls.get("name"):
        sr = {"name": ls["name"], "email": ls.get("email", ""), "role": "Landlord"}
        if same_person and la_cfg.get("phone"):
            sr["phone"] = la_cfg["phone"]
        if edit_row_exact("Landlord (1)") or edit_row_exact("Landlord"):
            _fill_participant_dialog(page, sr, pick_role=False)
        else:
            page.click(S["add_participant_btn"], timeout=t)
            page.wait_for_timeout(1000)
            _fill_participant_dialog(page, sr)
        assert any(lb.startswith(f"{ls['name']} (Landlord") for lb in _participant_row_labels(page)), (
            f"landlord signer did not land in a Landlord role; rows now: "
            f"{_participant_row_labels(page)!r}")

    # 2b. Listing agent — fill any 'Listing Agent' row the templates created
    #     (Jay: 'make sure Jason Arcuri is in there as the listing agent no
    #     matter what').
    la = la_cfg
    if la.get("name"):
        filled_agent = False
        for lbl in ("Listing Agent (1)", "Listing Agent"):
            if edit_row_exact(lbl):
                la_sr = {"name": la["name"], "email": la.get("email", ""),
                         "role": "Listing Agent"}
                if same_person and la.get("phone"):
                    la_sr["phone"] = la["phone"]
                _fill_participant_dialog(page, la_sr, pick_role=False)
                filled_agent = True
                break
        if not filled_agent:
            # No template creates the row anymore (lead form is an upload):
            # Jay is on EVERY signing as Listing Agent — create the participant.
            page.click(S["add_participant_btn"], timeout=t)
            page.wait_for_timeout(1000)
            la_sr = {"name": la["name"], "email": la.get("email", ""),
                     "role": "Listing Agent"}
            if same_person and la.get("phone"):
                la_sr["phone"] = la["phone"]
            _fill_participant_dialog(page, la_sr)

    # 3. Remove every remaining UNASSIGNED row: known junk roles, the overlay's
    #    blank 'Signer' row, and any extra unassigned Tenant/Landlord instance
    #    (e.g. a template's Tenant (2) on a single-tenant lease).
    def unassigned(labels):
        return [lb for lb in labels
                if lb == "Signer"
                or _LEFTOVER_ROLE_RE.match(lb)
                or re.match(r"^(Tenant|Landlord|Listing Agent)(\s*\(\d+\))?$", lb)]
    for _ in range(10):   # hard cap; each pass removes one row
        left = unassigned(_participant_row_labels(page))
        if not left:
            break
        _remove_row_via_distribution(page, left[0])
    left = unassigned(_participant_row_labels(page))
    assert not left, f"unassigned participant rows could not be removed: {left!r}"


# Disclosure of Interest checkbox positions, in Playwright's 1440x900 input
# space (page CSS is 1920x1200 at dpr 0.75; mouse = CSS * 0.75). Measured from
# the validation-state audit screenshots (canvas-drawn, so coordinates it is).
_DISCLOSURE_BOX_COORDS = {
    "item2_main":    (235, 433),   # '2. ... Seller's/Landlord's Agent' box
    "item2_himself": (266, 461),   # under 2: 'Himself or herself'
    "item3":         (239, 518),   # '3. ... owns or has ... interest'
}


def _management_tree(job):
    # personal (owned) / premio (managed) for this job's property — from the
    # job itself or the folder map; unknown -> personal.
    tree = (job.get("management_tree") or "").strip().lower()
    if tree:
        return tree
    try:
        m = json.loads(Path(r"C:\AIAgents\shared\lease_folders.json").read_text(encoding="utf-8"))
        t = (m.get(job.get("property_key", "")) or {}).get("tree", "")
        if t:
            return t
    except Exception:
        pass
    return "personal"


def _check_disclosure_boxes(page, job, auditor=None):
    # Tick the Disclosure of Interest checkboxes per the property's tree
    # (single canvas click each — the template starts all-unchecked, so one
    # click per box is deterministic). Runs right after the address boxes are
    # filled, while the editor is still on that document's page.
    tree = _management_tree(job)
    boxes = (CONFIG.get("disclosure_checks") or {}).get(tree, ["item3"])
    print(f"    disclosure checks for tree {tree!r}: {boxes}")
    for name in boxes:
        x, y = _DISCLOSURE_BOX_COORDS[name]
        page.mouse.click(x, y)
        page.wait_for_timeout(700)
    if auditor:
        auditor.snap(f"disclosure boxes checked ({tree}: {len(boxes)})")


def _missing_param_count(page):
    """Parse the editor's 'N out of M fields have missing parameters' badge.
    0 when absent — no sender-side fill-ins are pending."""
    txt = page.evaluate(
        "()=>{const e=[...document.querySelectorAll('*')].find(x=>x.children.length<=2"
        "&&(x.innerText||'').includes('fields have missing parameters'));"
        "return e?e.innerText:''}")
    m = re.search(r"(\d+)\s*out of\s*(\d+)", txt or "")
    return int(m.group(1)) if m else 0


def _address_lines(job):
    """Two lines for a street-address block: '123 Test St, 1st Floor' /
    'Waterbury, CT 06704' (falls back to the whole string twice)."""
    prem = (job.get("premises_address") or job.get("property") or "").strip()
    parts = [p.strip() for p in prem.split(",") if p.strip()]
    if len(parts) >= 3:
        return [", ".join(parts[:-2]), ", ".join(parts[-2:])]
    return [prem, prem]


def _fill_missing_param_fields(page, job, auditor=None):
    """Fill sender-side fill-in fields flagged by the missing-parameters badge.
    The fields render ON the canvas (no DOM), so: click the badge's navigator
    to select/scroll to the next flagged field, double-click it (Jay's method
    — opens an inline text editor), type the value, commit, repeat until the
    badge clears. Values: the property address lines."""
    t = CONFIG["step_timeout_ms"]
    n = _missing_param_count(page)
    if n == 0:
        return
    values = _address_lines(job)
    vi = 0
    stagnant = 0
    for _ in range(n + 6):
        n_now = _missing_param_count(page)
        if n_now == 0:
            return
        # Find a flagged field to fill. Preferred: the error-styled overlay
        # boxes over the canvas (red border/background, 'Please type
        # something'); fallback: the badge's navigator control, then rescan.
        def scan():
            return page.evaluate(
                """()=>{
                  const cv=document.querySelector("[data-testid='signing-form_editor_canvas']");
                  if(!cv) return {cands:[], all:[]};
                  const cr=cv.getBoundingClientRect();
                  const boxes=[...document.querySelectorAll('div,section,span')].filter(e=>{
                    const r=e.getBoundingClientRect();
                    if(!(r.width>40 && r.width<620 && r.height>12 && r.height<70)) return false;
                    if(!(r.left>=cr.left-10 && r.right<=cr.right+10
                         && r.top>=cr.top-10 && r.bottom<=cr.bottom+10)) return false;
                    const st=getComputedStyle(e);
                    if(st.position!=='absolute' && st.position!=='fixed') return false;
                    return true;
                  });
                  const info=boxes.map(e=>{
                    const r=e.getBoundingClientRect();
                    const st=getComputedStyle(e);
                    const cls=(e.className||'').toString();
                    const reddish=/danger|error|invalid/i.test(cls)
                      || /rgb\(2[0-4][0-9], *[0-9]{1,2}, *[0-9]{1,2}/.test(st.borderColor)
                      || /rgb\(2[0-4][0-9], *[0-9]{1,2}, *[0-9]{1,2}/.test(st.backgroundColor)
                      || /rgba?\(2[0-4][0-9]/.test(st.outlineColor||'');
                    return {x:Math.round(r.left+r.width/2), y:Math.round(r.top+r.height/2),
                            w:Math.round(r.width), h:Math.round(r.height),
                            cls:cls.slice(0,70), reddish,
                            bc:st.borderColor, bg:st.backgroundColor};
                  });
                  return {cands:info.filter(i=>i.reddish), all:info};
                }""")
        found = scan()
        print(f"    missing fields: {n_now}; error-styled overlays: "
              f"{len(found['cands'])} (of {len(found['all'])} overlays)")
        # Jay's demo (pause-demo events.log) decoded the success signal: a
        # double-click that actually lands on a fill-in box pops a DOM
        # TEXTAREA (w-full h-full resize-none ...) and focuses it. So: sweep
        # candidate points across the box's area, double-clicking until that
        # textarea takes focus, then type. Box 1 while both boxes are
        # missing, box 2 when one remains. (The 'found' overlay scan stays as
        # a hint but the sweep is the workhorse — fields are canvas-drawn.)
        def editor_open():
            return page.evaluate(
                "()=>{const a=document.activeElement;"
                "return !!(a && a.tagName==='TEXTAREA')}")

        def editor_opens_within(ms):
            # The canvas editor mounts SLOWLY (>400ms). Poll — a hasty next
            # click closes the editor that was about to appear, which is what
            # made the earlier sweeps self-defeating.
            waited = 0
            while waited < ms:
                if editor_open():
                    return True
                page.wait_for_timeout(250)
                waited += 250
            return editor_open()
        # Jay's demo gesture, exactly: a SINGLE click first (selects the field,
        # canvas takes focus), a beat, THEN the double-click opens the editor.
        # Coordinates are in Playwright's 1440x900 input space (page CSS is
        # 1920x1200 at dpr 0.75 — factor 0.75): box centers (560,216)/(560,244).
        ys = (216, 222, 210) if n_now >= 2 else (244, 250, 238)
        xs = (560, 640, 480)
        hit = None
        geom = page.evaluate("()=>({iw:window.innerWidth,ih:window.innerHeight,"
                             "dpr:window.devicePixelRatio})")
        print(f"    viewport: {geom}")
        page.wait_for_timeout(1500)   # let the validation view settle first
        for y in ys:
            for x in xs:
                page.mouse.click(x, y)        # select the field
                page.wait_for_timeout(800)
                page.mouse.dblclick(x, y)     # open its editor
                if editor_opens_within(3000):
                    hit = (x, y)
                    break
            if hit:
                break
        if auditor:
            auditor.snap("field sweep " + (f"hit at {hit[0]},{hit[1]}" if hit else "NO HIT"))
        assert hit, (f"could not open the fill-in box editor anywhere in its "
                     f"area (box {'1' if n_now >= 2 else '2'})")
        print(f"    editor opened at {hit}")
        val = values[0] if n_now >= 2 else values[-1]
        page.keyboard.type(val, delay=30)
        vi += 1
        page.wait_for_timeout(500)
        # Commit/deselect: click the canvas gutter (inside canvas, off the page).
        gut = page.evaluate(
            "()=>{const c=document.querySelector(\"[data-testid='signing-form_editor_canvas']\");"
            "const r=c.getBoundingClientRect();return {x:Math.round(r.left+12),y:Math.round(r.top+r.height/2)};}")
        page.mouse.click(gut["x"], gut["y"])
        page.wait_for_timeout(1200)
        if auditor:
            auditor.snap(f"typed {val[:25]!r}")
        if _missing_param_count(page) >= n_now:
            stagnant += 1
            assert stagnant < 5, (
                f"missing-parameters count is not decreasing (still {n_now}) — "
                f"field filling is not landing")
    assert _missing_param_count(page) == 0, (
        f"could not clear all missing-parameter fields "
        f"({_missing_param_count(page)} left)")


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
        # Single-tenant lease: drop the overlay's unused Tenant (2) slot so its
        # fields never orphan.
        exclude = ("Tenant (2)",) if len(signers_of(job)) < 2 else ()
        _apply_overlay_to_lease(page, overlay, exclude_roles=exclude)
    step(auditor, f"apply overlay: {overlay}", apply_overlay)

    # 4. Add the remaining uploaded documents (e.g. filled Rental Terms
    #    Summary, pre-filled Disclosure of Interest), applying a signature
    #    overlay right after any upload that has one configured.
    def add_doc_with_overlay(d):
        _add_uploaded_document(page, d)
        stem = Path(d).stem.lower()
        for key, overlay_name in (CONFIG.get("doc_overlays") or {}).items():
            if key.lower() in stem:
                exclude = ("Tenant (2)",) if len(signers_of(job)) < 2 else ()
                _apply_overlay_to_doc(page, Path(d).stem, overlay_name,
                                      exclude_roles=exclude)
                break
    for doc in documents[1:]:
        step(auditor, f"add document: {Path(doc).name}",
             (lambda d=doc: add_doc_with_overlay(d)))

    # 5. Add each premade packet template (its own document), one at a time.
    for tpl in CONFIG["packet_templates"]:
        step(auditor, f"add packet template: {tpl}",
             (lambda name=tpl: _add_template_by_name(page, name)))

    # 6. Reconcile participants: tenants -> Tenant (1)/(2), landlord signer ->
    #    the Landlord row, and delete the sale roles the templates injected.
    step(auditor, "reconcile participants (assign signers, prune sale roles)",
         (lambda: _reconcile_participants(page, job)))

    # 6b. Fill the sender-side fill-in fields (e.g. the Disclosure of Interest's
    #     'Subject Property Address' text boxes). The editor flags them as
    #     'N out of M fields have missing parameters'; each is double-clicked
    #     on the canvas and typed (Jay's method).
    step(auditor, "fill sender fields (missing parameters)",
         (lambda: _fill_missing_param_fields(page, job, auditor)))

    # 7. HARD CHECK — every approved signer email must appear on-screen exactly
    #    (normalized, case-insensitive) and no OTHER email may appear. Emails
    #    render inside the participant sections.
    def verify_recipients():
        # The Signing Flow rows show name+role but not the email, so read each
        # participant's email back from its edit dialog (then Cancel — no change).
        approved = {s["email"].strip().lower() for s in all_signers(job) if s.get("email")}
        la_email = listing_agent_for(job).get("email") or ""
        if la_email:
            approved.add(la_email.strip().lower())
        found = set()
        _dismiss_stray_dialog(page)
        edits = page.locator(S["edit_participant_btn"])
        count = edits.count()
        try:
            for i in range(count):
                edits.nth(i).click(timeout=t)
                page.wait_for_selector(S["participant_section"], timeout=t)
                page.wait_for_timeout(800)
                # Prefer the email input's value; fall back to scraping the page.
                try:
                    val = page.locator(S["participant_email"]).input_value(timeout=5_000)
                    if val:
                        found.add(val.strip().lower())
                except Exception:
                    pass
                dtxt = page.locator("body").inner_text()
                for tok in re.findall(_EMAIL_SCRAPE, dtxt):
                    found.add(tok.lower())
                try:
                    page.click(S["participant_cancel"], timeout=5_000)
                except Exception:
                    page.keyboard.press("Escape")
                page.wait_for_timeout(600)
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
        # Work through the post-Send prompts (observed live, any order):
        #   - "One or more checkboxes have not been completed. Do you still
        #     wish to proceed?"  -> Proceed (they're signer-completed fields)
        #   - "Please fix the issues marked with exclamation mark" + the
        #     'N out of M fields have missing parameters' badge -> the send was
        #     BLOCKED by validation: fill the flagged sender fields (property
        #     address boxes) and click Send again.
        #   - "Save Contact Group? ... save these contacts as a signing
        #     group ..."         -> No (never mutate Jay's saved groups)
        # then wait for the sent confirmation.
        deadline = time.time() + 3 * t / 1000.0
        while time.time() < deadline:
            if page.locator(S["sent_confirmation"]).count():
                return
            try:
                if page.get_by_text("Do you still wish to proceed", exact=False).count():
                    page.get_by_role("button", name="Proceed", exact=True).first.click(timeout=5_000)
                    page.wait_for_timeout(1500)
                    continue
                if page.get_by_text("save these contacts as a signing group", exact=False).count():
                    # Decline = the prompt's Cancel button (verified in Jay's demo).
                    page.locator("[data-testid='dialog-prompt-cancel-btn']").first.click(timeout=5_000)
                    page.wait_for_timeout(1500)
                    continue
                if _missing_param_count(page) > 0:
                    # Validation blocked this send — nothing went out. Fill the
                    # flagged fields, tick the disclosure checkboxes (once),
                    # then click Send again.
                    _dismiss_stray_dialog(page)
                    _fill_missing_param_fields(page, job, auditor)
                    if not state.get("boxes_checked"):
                        _check_disclosure_boxes(page, job, auditor)
                        state["boxes_checked"] = True
                    auditor.snap("sender fields filled; resending")
                    page.click(S["send_btn"], timeout=t)
                    page.wait_for_timeout(1500)
                    continue
            except StepFailure:
                raise
            except AssertionError:
                raise   # step() screenshots and reports these
            except Exception:
                pass
            page.wait_for_timeout(700)
        page.wait_for_selector(S["sent_confirmation"], timeout=5_000)
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
