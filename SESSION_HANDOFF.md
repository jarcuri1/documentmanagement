# SESSION HANDOFF — finish the lease automation on this PC + do a live test

You are a Claude Code session running **locally on Jay's Windows fleet PC** with a
real shell (C:\ and D:\). You're taking over a lease-automation build that was
written in a separate cloud session. **All code is on GitHub.** Your goal RIGHT
NOW: get it running on this PC and do a **live test run that drives SmartMLS Sign
with fake data while Jay watches.** Start immediately with Step 1 below; pause
only for the GUI steps marked **[JAY]**.

## START NOW — Step 1 (clone + install)
Run these (Command Prompt). Report any error to Jay and fix before moving on.
```
cd /d C:\AIAgents
git clone -b claude/lease-sender-automation-04ts0r https://github.com/jarcuri1/documentmanagement.git LeaseAgent
cd LeaseAgent
pip install playwright python-docx pymupdf google-api-python-client dropbox
playwright install chrome
```
- If `pip` isn't found, use `py -m pip install ...`.
- If `git` isn't found, the installer is in C:\Users\realt\Downloads (Git-*.exe).
- Commit any code changes to branch `claude/lease-sender-automation-04ts0r` and push.

## Read these in the repo for full context
- `DEPLOY_LEASE_AGENTS.md` — deploy + fleet wiring.
- The docstring at the top of every `lease_*.py` — each explains its piece.
- `HANDOFF_TO_SAMANTHA_AND_SUPERVISOR.md` — the fleet integration contracts.

## What this system does (one breath)
`lease_intake.py` (interview) → `lease_fill.py` fills the Word lease + `lease_forms.py`
auto-fills the CT Rental Terms Summary → approval card to Jay's phone → on approve
`lease_watcher.py` claims the job → `lease_sender.py` drives **SmartMLS Sign**
(upload lease + apply overlay + add 4 premade templates + add signers + send) →
when signed, `lease_signed_watcher.py` pulls the executed PDF from Gmail and
`lease_filer.py` files it into the right Dropbox property folder.

## Environment facts (this PC)
- Windows user `realt`. Dropbox root: **D:\Dropbox\Dropbox**. Fleet: **C:\AIAgents**.
- LibreOffice INSTALLED at `C:\Program Files\LibreOffice\program\soffice.exe`
  (converting works; the code auto-finds it).
- SmartMLS Sign app: `https://signings.smartmls.propkit.io/signings` (via SmartMLS SSO).
- Code defaults already point at these paths — no env vars needed on this PC.

## State: DONE vs TODO
DONE: LibreOffice installed + converting.
TODO (your job, in order):
1. **[YOU]** Step 1 above (clone + pip install).
2. **[JAY, browser]** `python lease_sender.py --setup` → a Chrome window opens on
   the automation's own profile; Jay clicks "Sign in with Smart MLS", completes
   SmartMLS login + MFA, lands on the Signings dashboard, closes the window.
   (This is separate from Jay's normal browser login.)
3. **[JAY, in Sign]** Finish the signature **overlay templates** in Sign, named
   EXACTLY: `Agent automated single_family_lease` and
   `Agent automated multi_family_lease`. Build them on the reference PDFs you
   generate in the next line so alignment holds:
   ```
   "C:\Program Files\LibreOffice\program\soffice.exe" --headless -env:UserInstallation=file:///C:/temp/lo --convert-to pdf --outdir . templates\single_family_lease.docx
   ```
   (repeat for `templates\multi_family_lease.docx`). The lease fill guarantees
   layout stability (`verify_layout_locked`), so overlays built on these PDFs stay
   aligned on every filled lease.
4. **[YOU + JAY] THE GATE — capture selectors.** Current `SELECTORS` in
   `lease_sender.py` are PLACEHOLDERS; the sender fails on the first click until
   this is done. Run:
   ```
   playwright codegen --user-data-dir="C:\AIAgents\LeaseAgent\chrome-profile" https://signings.smartmls.propkit.io/signings
   ```
   Have Jay walk ONE signing by hand: New Signing → name it → upload a PDF →
   apply a template → add a signer (search existing contact + add a new one) →
   Send. Capture each printed Playwright selector and edit the `SELECTORS` dict in
   `lease_sender.py` — that dict is the ONLY place selectors live. Keys to fill:
   `new_signing_btn, signing_name_input, create_btn, upload_input,
   doc_uploaded_marker, templates_btn, template_row, apply_template_btn,
   add_signer_btn, contact_search, contact_result, signer_name, signer_email,
   signer_role, signer_save, remove_second_tenant, review_email_text, send_btn,
   sent_confirmation, logged_in_marker`.
5. **[YOU]** `python lease_folders_bootstrap.py` → open the draft
   `C:\AIAgents\shared\lease_folders.json`, review with Jay, delete the `_review`
   block.
6. **[YOU + JAY] TEST RUN** (see below).

## The test run (Step 6)
Use the fake intake below. First-time tip: to test the core flow before the 4
supporting templates are confirmed, temporarily set `CONFIG["packet_templates"] = []`
in `lease_sender.py`, get a clean run, then restore them.
```
python lease_intake.py        # answer with the fake data below
```
That writes an intake to `D:\Dropbox\Dropbox\Leases\Intake`. Then:
```
python lease_fill.py --drain  # fills lease + RTS, writes job + card to Pending
python lease_sender.py --job "D:\Dropbox\Dropbox\Leases\Pending\<slug>.json"
```
The sender opens a visible Chrome and drives Sign. Watch it. If a step fails, it
screenshots to `D:\Dropbox\Dropbox\Leases\Audit\...` and aborts — read the shot,
fix the matching selector, retry.

### Fake test data (single-family)
- lease_type: single-family
- Owner LLC (Landlord line): `MWC Real Estate LLC`
- Landlord signer: `Matthew Como`, email `jarcuri1@hotmail.com` (reuse Jay's test inbox)
- Property: `123 Test St, 1st Floor, Waterbury CT`; premises `123 Test St, 1st Floor, Waterbury, CT 06704`
- Term: 2026-09-01 to 2027-08-31; rent 1500; deposit 1500
- Utilities: City / Sewer / Gas
- **Tenant 1:** name `Test Tenant One`, email `jarcuri1@hotmail.com`
- **Tenant 2:** name `Test Tenant Two`, email `jarcuri1@gmail.com`
  (Address/SSN are tenant-filled in Sign; leave blank.)
- ONLY send to Jay's own inboxes above — never a real tenant during testing.

## Gotchas
- `soffice` exits 0 even when it fails; the code verifies the PDF appears (handled).
- The code launches soffice with an isolated `-env:UserInstallation` profile so
  conversion works even if the LibreOffice Quickstarter is running.
- `lease_sender` opens a HEADED Chrome — needs the interactive desktop (Jay's here).
- Do NOT expect the sender to work before Step 4 (selectors).
- Still-open integration items (NOT blockers for this test): Google Sheet →
  auto-LLC lookup, the app-thread `kind:"lease"` card, and adding the 3 agents to
  the supervisor. Do those after the test succeeds.

## Do this now
Run Step 1. Report the clone + pip output. Then walk Jay through Step 2 and Step 4.
```
