# TenantTracks UI Map (mapped live 2026-07-23 with Jay)

Portal: `https://app.tenanttracks.com` (marketing site www.tenanttracks.com
redirects logged-out users to `/user/login`).
Account: Jay's "Open Access" account (realtorarcuri@gmail.com; username field
accepts email). 36 properties, 197 checks run. "Reports Remaining: 0" is fine —
**Jay's rule: the APPLICANT always pays** ($39/report; MA criminal add-on
$49.99 — not used, CT properties).

## Login
- `/user/login`: `Username or Email`, `Password (case sensitivity)`,
  `Remember me` checkbox, `Log in` button.
- Same unattended-login pattern as SmartMLS: creds go in Windows Credential
  Manager via set_login.py-style setup; bot fills and submits at runtime.

## Run a screening (the "Run Background Check" flow)
URL: `/report_smart?page=new` (also the top-nav + sidebar entry point).

1. **Choose who pays** — buttons `Owner / Rental Agent Pays` / `Applicant Pays`
   (select) then green `Confirm`. ALWAYS pick `Applicant Pays`.
   (MA criminal-records checkbox on this screen — leave unchecked.)
2. **Choose property** (`#choose-property`) — left: `Choose Existing Property`
   dropdown + `Choose property` button; right: `Add New Property` form
   (Property Name, Address, City, State [default Connecticut], Postal Code,
   Security Deposit, Rent Amount, ...). Automation: match existing by name,
   else add-new prefilled from the master sheet.
3. **Request delivery** — two panels:
   - `Option 1: Send Background check request(s) to your applicant via their
     email` → expands form: `Applicant Email`, `Retype Applicant Email`,
     `Applicant Phone` (placeholders match names). TenantTracks then emails
     AND texts the applicant, who accepts + pays; Jay is notified when the
     report has run. (Submit control appears with/below the form.)
   - `Option 2: Create Link and post or text link on your social media sites
     or MLS listing` → standing per-property self-screen link (future: attach
     to listings automatically).

## Sidebar map (hrefs)
- Run Background Check — `/report_smart?page=new`
- Completed Reports / Generate Receipt — `/report_smart?page=applications`
- Manage Your Properties — `/report_smart?page=properties`
- Create Tenant Denial Letter — `/dashboard/generate_denial_oa`
- Enhanced Screening Tools — `/addOns/dashboard`
- Your Rental Applications — `/report_smart/rental_applications`
- Application and Reporting Forms — `/dashboard/propforms`
- Enter Tenant Performance (Internal) — `/dashboard/performancelist`
- Report Rent to Credit Bureau — `/reporting?type=rental`
- Edit Your Profile — `/dashboard/userprofile`
- View Your Previous Purchases — `/dashboard/orders`
- Upgrade Your account — `/upgrade/init`

## Dashboard (`/report_smart` or `?page=dashboard`)
- Tiles: Reports Remaining / Properties / Background Checks.
- `Recent Properties` table (Property, Address, View).
- `Recent Applications` table (Property, Email, Created At, **"View completed
  report" link**) — completed screenings surface here; a poller can watch this
  or `?page=applications` instead of (or besides) the notification email.

## Feature design (Jay's goal, 2026-07-23)
Applicant pipeline ahead of the lease pipeline:
1. Applicant intake per property/unit (app tab; possibly also pull
   TenantTracks' own Rental Applications).
2. Fleet bot runs the Option-1 request unattended (applicant pays).
3. Completion watcher grabs the report, files it, pushes Jay a summary.
4. Applicant registry (shared JSON) tracks who applied per unit + status.
5. Lease wizard offers the unit's screened applicants as signer choices —
   picking them prefills the lease intake (names/emails/phones already known).

### Option 1 finish (mapped via live test send 2026-07-23, jarcuri1@gmail.com)
- Below the applicant fields: `Add Additional Applicant` button (multi-signer
  couples = one request), then a REQUIRED checkbox
  `I confirm I have read information above` + `Report price: $39`, then a
  `Submit Application` LINK (not button).
- Warning text: wrong applicant email is fatal (start over); Jay is NOT
  charged until the applicant submits info to Trans Union; Jay's billing
  phone gets 2 texts (applicant responded / report ready).
- After submit: `#send-application` — Status table (Email | Phone | Status
  spinner -> green OK) + "The application has been saved" + `Go to Dashboard`.
- Bureau is Trans Union. Applicant must answer the email/text and fill their
  own info; completion notice comes by email (the Email Agent will flag it —
  Jay pulls up the site himself; NO pdf download/filing wanted).

## Jay's rules (2026-07-23)
- Applicant ALWAYS pays. Never Owner/Rental Agent Pays.
- Phone is required by the form but usually unknown -> enter a fake number
  (e.g. 203555xxxx); the text simply never lands, email carries the flow.
- No report filing into Dropbox; email notification + site is enough.

## Open items
- Completed-report screen layout (view later from an old report if ever
  needed — deprioritized, no filing wanted).
- Build: set_login for tenanttracks + screening bot + applicant registry +
  app "Applicants" tab + lease-wizard signer picker from registry.
