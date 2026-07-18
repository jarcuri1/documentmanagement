# Deploying the lease agents into the fleet

## 1. Put the code where the supervisor expects it
The supervisor launches agents from `C:\AIAgents\<dir>`. Clone (or copy) this
repo to:
```
C:\AIAgents\LeaseAgent\
```
(so `dir: 'LeaseAgent'` resolves). All the scripts default their Dropbox paths
to `D:\Dropbox\Dropbox\Leases`, the shared rail to `C:\AIAgents\shared`, the
browser profile to `C:\AIAgents\LeaseAgent\chrome-profile`, and the Gmail token
to `C:\AIAgents\EmailAgent\token_premio.json` — so no env overrides are needed
if the machine matches those. Override any with the `LEASE_*` env vars if not.

## 2. Install dependencies
```
pip install playwright python-docx pymupdf google-api-python-client dropbox
playwright install chrome
```

## 3. Create the packet folder + drop in the static docs
```
D:\Dropbox\Dropbox\Leases\Packet\
```
Put the documents that ride with EVERY signing here — e.g.
`01_Lead Disclosure.pdf`, `02_Protect Your Family.pdf`,
`03_Lease Terms Overview.pdf`. They're uploaded unchanged; only the lease
itself swaps (single- vs multi-family). Prefix `01_`, `02_` to control order.
Add or remove files any time — the fill agent includes whatever is in the
folder. (The Leases pipeline folders — Pending/Sending/Sent/Failed/Rejected/
Audit/Intake/Signed — are created automatically.)

## 4. Add the three agents to the supervisor
Paste these into `DEFAULT_FLEET.agents` in `C:\AIAgents\supervisor\supervisor.js`
(new agents are merged into fleet.json on the next start):

```js
    lease_fill: {
      label: 'Lease — Fill & Queue',
      enabled: true,
      dir: 'LeaseAgent',
      cmd: 'python',
      args: ['lease_fill.py', '--drain'],   // process new intakes, exit
      schedule: { type: 'interval', minutes: 1 },
      env: {}, envControls: [], modes: { 'normal': [] },
    },
    lease_watcher: {
      label: 'Lease — Approval → Send',
      enabled: true,
      dir: 'LeaseAgent',
      cmd: 'python',
      args: ['lease_watcher.py', '--once'],  // consume decisions, claim, send, exit
      schedule: { type: 'interval', minutes: 1 },
      env: {}, envControls: [], modes: { 'normal': [] },
    },
    lease_signed: {
      label: 'Lease — File Signed',
      enabled: true,
      dir: 'LeaseAgent',
      cmd: 'python',
      args: ['lease_signed_watcher.py'],     // pull completed leases from email, file them
      schedule: { type: 'interval', minutes: 5 },
      env: {}, envControls: [], modes: { 'normal': [] },
    },
```
Each runs in "process the queue once and exit" mode; the supervisor skips a
tick if the previous run is still going, so a long signing never double-fires.
`lease_watcher` opens a visible Chrome to drive SmartMLS Sign, so the
supervisor must run in an interactive desktop session (start-at-login), not
"run whether logged on or not".

Restart the supervisor. The three agents appear on the dashboard with
Run-Now / logs / last-run status like the others.

## 5. One-time setup still required (see each script's header)
- `python lease_sender.py --setup` — sign into SmartMLS Sign once (persists the
  session), build the signature-field template under **Templates (Forms)**, and
  `playwright codegen` the real selectors into `lease_sender.py`'s `SELECTORS`.
- `python lease_folders_bootstrap.py` — draft the folder map; review, delete
  `_review`.
- `set LEASE_DROPBOX_TOKEN=...` (+ `pip install dropbox`) for the tap-to-open
  `pdf_url` on approval cards (optional).
- App thread: add the `kind:"lease"` card path so cards render with Send/Reject.
- Confirm which Gmail account receives SmartMLS Sign completions (default:
  `token_premio.json`); set `LEASE_GMAIL_TOKEN` if it's the realtor inbox.

## Starting a lease (day one, until Samantha /chat lands)
```
python lease_intake.py
```
Answer the questions (type, address, term, rent, deposit, utility checkboxes,
tenants). It writes an intake into `Leases\Intake\`; `lease_fill` fills the
lease, bundles the packet, and sends the approval card to your phone.
