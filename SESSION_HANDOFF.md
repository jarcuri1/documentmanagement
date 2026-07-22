# SESSION HANDOFF — Lease Automation (updated 2026-07-22 morning)

Read this first, then `C:\Users\realt\.claude\projects\C--AIAgents\memory\MEMORY.md`
(auto-loaded) and repo `SIGN_UI_MAP.md` for SmartMLS Sign mechanics.

## Who / where
- Jay (realtor/property manager), fleet PC, user `realt`.
- LeaseAgent: `C:\AIAgents\LeaseAgent` — repo jarcuri1/documentmanagement,
  branch `claude/lease-sender-automation-04ts0r` (commit + push here).
- Supervisor: `C:\AIAgents\supervisor\supervisor.js`, serves ONLY on
  Tailscale IP `100.66.99.5:8787`. Restart: `schtasks /End /TN "Fleet Supervisor"`
  then `/Run` (works from the sandbox, no admin).
- Phone app: `C:\AIAgents\AIDashboard` (Expo 57, runtime 1.0.8, NOT a git repo).
  OTA: `EAS_NO_VCS=1 NODE_OPTIONS=--max-old-space-size=4096 npx eas update
  --channel preview --environment preview --platform android --non-interactive`.
  Updates apply on the SECOND launch (double force-close).
- Premio app: `C:\AIAgents\PremioApp` → github jarcuri1/premio-property-management
  (main branch; push = Netlify auto-deploy to stalwart-truffle-2dd64a.netlify.app).
- Leases data: `D:\Dropbox\Dropbox\Leases\{Intake,Pending,Sending,Sent,Failed,Audit,Signed}`.

## STATE: everything works end-to-end, Jay approved the packet
Full loop proven + approved 2026-07-21: app wizard → intake → packet fill
(zero-shift lease + 3 pre-filled forms w/ Jay's initials/signature) → AUTO-send
(no approval card for app intakes) → SmartMLS Sign assembly (overlays verified
by field-count increase, template, 3-4 participants incl. dynamic listing-agent
email rule) → sign → executed PDF auto-filed, old lease → `Past Tenants\`.

Overlay bug that plagued runs 1-3 is FIXED: `_click_doc_gear` clicks the LAST
gear (panel truncates names; text match hit the lease doc), and every overlay
apply asserts total field count increases. Listing Agent is a SIGNER (his
lead-form overlay fields exist now; Distribution can't own fields).

Hang-proofing (all committed): watcher caps sender at 25 min then tree-kills;
sender self-watchdog aborts at 23 min; lease_pipeline reaps orphaned
sender/chrome-profile processes older than cap (only the supervisor's elevated
session can kill them — sandbox taskkill gets Access denied).

## TENANT TURNOVER — built + deployed, NOT yet tested
On signed-lease return, `lease_filer.file_signed_lease` now also calls
`update_sheet_tenant`: POSTs the Premio app's `edit-tenant` with the job's
exact sheet coords, writing tenant name(s)/rent/deposit to the master Google
Sheet. It reads the row's PRIOR values first (undo trail in
`job.sheet_update.previous`) and pushes success/failure.

Data path: wizard payload `sheet_tab/sheet_property/sheet_unit`
(NewLeaseScreen submit) → supervisor `intake.sheet {tab, property, unit}` →
lease_fill job `sheet` + `rent/deposit/term_end_iso` → filer. Client leases /
old app builds have no `sheet` → update silently skips.
- sheet_property must be the sheet's VERBATIM column-A string, e.g.
  `128 Walnut St, Naugatuck, CT 06770` (comes from prop.sheet.address).
- `edit-tenant` was patched (deployed) to skip paymentMethod when omitted.
- New function `add-property` (deployed): appends property row + unit rows.

## IMMEDIATE NEXT: full E2E test with 111 Test St
Jay asked for a dedicated test property so NOTHING is skipped:
1. DONE: folder `D:\Dropbox\Dropbox\Personal Properties\111 Test St Naugatuck`
   + entry `111-test-st-naugatuck` in `C:\AIAgents\shared\lease_folders.json`.
2. SHEET: Jay said HE will add it to Combined Empire (property row
   `111 Test St, Naugatuck, CT 06770` in col A + unit row `Main` in col B).
   CAUTION: my earlier `add-property` API call returned SUCCESS (unit Main,
   tenant "Prior Tenant", $1,000/$1,000) but the CSV export never showed it —
   VERIFY whether the rows exist before assuming; avoid duplicates.
3. Restart supervisor after the sheet rows exist (1h sheet cache), confirm
   `GET /api/lease/options` lists the property with sheet prefills.
4. Jay submits a lease from the app wizard → auto-send → he signs all roles →
   signed watcher files it → CHECK: PDF in the Test St folder, old lease
   retirement, sheet row now shows the new tenant + before-values recorded in
   the Sent job json (`sheet_update`), pushes received.
5. Roll back the sheet row / delete test signing afterward.

## OPEN ISSUES right now
- Phone app was crashing on New Lease + Settings tabs and main screen had
  fetch errors AFTER the turnover OTA. Likely cause: Jay rebooted the PC —
  phone Tailscale showed offline (fetch errors), plus a possibly half-applied
  OTA. Fix path: reconnect phone Tailscale, double force-close app. If crashes
  persist: republish previous good OTA group
  `eas update:republish --group 2f4404fc-59c4-43bc-8bbf-ee4a66edf7aa`
  (message "Require first+last names"). Current group:
  2dfbfd54-8773-43c4-9b31-c982d2e33bfd.
- Dropbox client NOT running after Jay's reboot (he closed programs — PC was
  lagging). Pipeline unaffected (local paths) but no cloud sync. Jay may start
  it himself; offer, don't force.
- 128 Walnut test signing ("test bitches", jarcuri1@gmail.com) is OUT for
  signing. When signed it will file but SKIP the sheet update (job predates
  sheet coords) — Katelyn Goff's real row is safe.
- Sign cleanup: many test drafts/signings for Jay to withdraw (his task).

## LATER / NICE-TO-HAVE
- Lease link into sheet col AA (edit-tenant leaseUrl) — needs a Dropbox API
  token (LEASE_DROPBOX_TOKEN) to create shared links.
- Move old tenant's lease inside the Premio app UI (folder move covers it).
- Section-8 rent semantics for managed units (col F tenant vs col G total).
- Advise Jay again: mortgage credentials live in the link-shared sheet.

## GOTCHAS (cost hours — do not relearn)
- Sign canvas fields REJECT synthetic input → pre-fill PDFs (lease_forms.py).
- Documents panel truncates names → never text-match doc rows; last gear.
- page.evaluate has NO timeout → the 3-layer watchdog above.
- bash heredocs corrupt `\b` in JS regexes → use Write-tool patch scripts.
- Supervisor answers ONLY on 100.66.99.5:8787 (not localhost).
- soffice profiles must live in local temp (Dropbox sync corrupts them).
- Never bump app version before OTA; OTA applies on second launch.
- Auto-mode classifier blocks sender runs from the sandbox (Jay/fleet runs
  them) and blocks deploying ad-hoc kill scripts — use the committed reaper.
- Sheet mortgage-credential columns: NEVER parse or serve.
