# SESSION HANDOFF — Lease Automation (updated 2026-08-10)

Read this first, then `C:\Users\realt\.claude\projects\C--AIAgents\memory\MEMORY.md`
(auto-loaded) and repo `SIGN_UI_MAP.md` for SmartMLS Sign mechanics.
`TENANTTRACKS_UI_MAP.md` covers the applicant-screening flow.

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

## STATE: full pipeline PROVEN end-to-end, including tenant turnover
- 2026-07-21: Jay approved the packet; full send→sign→file loop verified.
- 2026-07-23: dedicated E2E test with **111 Test St** completed cleanly —
  app wizard → auto-send → signed → filed to the property folder → master-sheet
  row updated (tenant/rent/deposit/dates) with before-values recorded in the
  Sent job json (`sheet_update.previous`) → pushes received.
- Lease share link (was a LATER item) SHIPPED 7/23: filer creates a restricted
  Dropbox link (viewers: Jay + Matt) and writes it to sheet col AA (leaseUrl).
- Test rollback DONE: Jay removed the 111 Test St sheet row. The folder entry
  in `lease_folders.json` + the Dropbox folder were KEPT on purpose — the
  wizard's stale filter (supervisor.js ~line 1061: personal-tree folders with
  no sheet match are hidden) keeps it invisible. To rerun an E2E test, Jay
  just re-adds the sheet row (`111 Test St, Naugatuck, CT 06770` col A,
  unit `Main` col B) and restarts the supervisor (1h sheet cache).
- Later hardening (all committed+pushed): signed watcher searches
  premio+realtor inboxes and skips already-filed jobs (7/23); interactive
  filing card for unmatched signed leases (7/29); filer honors unit
  subfolders on managed properties (7/30); watcher retries once on transient
  DNS failures (committed 8/10 — was sitting uncommitted since ~7/30).
- TenantTracks applicant screening was built in this repo 7/23-7/25
  (tenanttracks_agent.py, on-demand via app / screening_queue) — see the
  tenant-screening memory + TENANTTRACKS_UI_MAP.md.

## TENANT TURNOVER -> APARTMENTS.COM (built 8/10, browser half pending)
Jay's ask: new lease signed -> cancel old tenant's future payments + end
residency + set new tenant up to pay online on Apartments.com; renewal with
same tenant -> only act if rent changed; plus an app "Remove tenant" that
also retires the lease PDF and clears the sheet row.

Built and LIVE:
- `lease_turnover.py` classifies each filed signing (turnover / move_in /
  renewal_rent_change / renewal_no_change) off sheet_update.previous.
- `lease_filer.queue_turnover_card` puts an `aptpay-` card on the approvals
  rail (custom TurnoverCard in the app). Approve -> job file in
  `shared\apartments_lease_queue`. NOTHING touches Apartments.com unapproved.
- `lease_signed_watcher.consume_removals` executes app Remove-Tenant jobs
  (`shared\tenant_removals`, from supervisor POST /api/tenant/remove):
  retire lease PDFs to Past Tenants, clear sheet row (edit-tenant `clear`
  flag, deployed; prior values snapshotted), queue the payment cancel.
- App (OTA runtime 1.0.10): TurnoverCard in ApprovalsScreen; RemoveTenant
  danger-zone section at the bottom of the New Lease tab.
- Supervisor: POST /api/tenant/remove + `apartments_payments` fleet entry
  (daily 12:30, runs `ApartmentsAgent\apartments_payments_agent.py`).
- A real card for 168 Lucille (Sheila -> Mattesons) is pending in the app.

## IMMEDIATE NEXT: implement apartments_payments_agent.py browser half
The payments/residents UI IS MAPPED (live-probed 8/10 with Jay's OK — see
`ApartmentsAgent\APARTMENTS_UI_MAP.md` "Payments & Residents" table:
End Payments confirm chain, Edit Rent form, Residents & Leases list/detail,
Set Up New Resident entry). Remaining before code: walk the set-up wizard
past page 1, the Edit Rent review step, and an ACTIVE resident's detail
view (probed one was ended — showed Re-enable). Then implement the four
queue actions with a separate chrome-profile-payments. The 8/10 turnover
card for 168 Lucille was DELETED as stale (Jay removed Sheila by hand, and
his app pre-OTA showed the wrong card UI — make sure he's on runtime 1.0.10
before the next card goes out).
Also check `D:\Dropbox\Dropbox\Leases\Failed` and `git status` on pickup.

## OPEN ITEMS (low stakes)
- 128 Walnut "test bitches" signing (7/21, jarcuri1@gmail.com) was never
  signed — presumed withdrawn by Jay in Sign. Its job sits in Sent with
  `filed: null`; harmless, watcher skips it. Old test form-PDFs for it still
  sit in `Leases\Pending` — deletable noise.
- Sign cleanup: any remaining test drafts/signings are Jay's to withdraw.
- `chrome-profile-tenanttracks/` holds the live TenantTracks session — now
  gitignored (like `chrome-profile/`), never commit it.

## LATER / NICE-TO-HAVE
- Move old tenant's lease inside the Premio app UI (folder move covers it).
- Section-8 rent semantics for managed units (col F tenant vs col G total).
- Advise Jay again: mortgage credentials live in the link-shared sheet.

## GOTCHAS (cost hours — do not relearn)
- Sign canvas fields REJECT synthetic input → pre-fill PDFs (lease_forms.py).
- Documents panel truncates names → never text-match doc rows; last gear
  (`_click_doc_gear`), and every overlay apply asserts total field count grows.
- Listing Agent must be a SIGNER (Distribution can't own overlay fields).
- page.evaluate has NO timeout → 3-layer watchdog: watcher caps sender at
  25 min + tree-kill, sender self-aborts at 23 min, lease_pipeline reaps
  orphaned sender/chrome-profile processes (only the supervisor's elevated
  session can kill them — sandbox taskkill gets Access denied).
- bash heredocs corrupt `\b` in JS regexes → use Write-tool patch scripts.
- Supervisor answers ONLY on 100.66.99.5:8787 (not localhost).
- soffice profiles must live in local temp (Dropbox sync corrupts them).
- Never bump app version before OTA; OTA applies on second launch.
- Auto-mode classifier blocks sender runs from the sandbox (Jay/fleet runs
  them) and blocks deploying ad-hoc kill scripts — use the committed reaper.
- Sheet mortgage-credential columns: NEVER parse or serve.
- Wizard hides personal-tree properties that have no master/equity-sheet row
  (stale filter) — "missing property" in the app usually means missing sheet
  row, not a folder-map problem.
- Client leases / old app builds have no `sheet` coords → sheet update
  silently skips (by design).
