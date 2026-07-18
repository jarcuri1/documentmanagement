# SmartMLS Sign — real UI map (reverse-engineered live, 2026-07-18)

The live app differs substantially from the placeholder assumptions in
`lease_sender.py`. This is the confirmed map from driving the real app
(headed) while logged in as ARCURIJA. App is a React SPA at
`signings.smartmls.propkit.io`; SSO is Keycloak at `smartmls-sso.connectmls.com`.

## Auth (DONE, working)
- Landing `/auth` has a `button:has-text('Sign in with Smart MLS')`.
- Clicking it either silently returns to the dashboard (SSO cookie good) or
  lands on the Keycloak form: `#username`, `#password`, `#rememberMe`, `#kc-login`.
- `login_if_needed()` in lease_sender.py handles all three landings and logs in
  from Windows Credential Manager creds (set via `set_login.py`). PROVEN working.
- Dashboard marker = `[data-testid="signings-create-btn"]`.

## Create + upload flow
1. New Signing: `[data-testid="signings-create-btn"]`.
2. Startup screen (`/signings/edit?isStartUp=true`):
   - Name input: `[data-testid="signing-form-signing-details-dialog-edit-name-input"]`
   - Optional: mls-id, address, expiration, time inputs (same testid prefix).
   - Upload: click `[data-testid="signing-form-signing-upload-file-btn"]` ("Upload
     Document(s)") to mount the uploader, THEN
     `set_input_files("[data-testid='editor-document-uploader-input']", pdf)`.
   - Continue (enabled only after a doc is uploaded):
     `[data-testid="signing-form-save-and-continue-btn"]`.
3. Editor (`/signings/edit/<id>`).

## Editor
- Add more docs: `button:has-text('+ Add Document(s)')` → `button:has-text('Select Template')`.
- Template picker: each template is a row whose title is a `div.text-4.font-semibold`
  with the EXACT name. Select by name via `get_by_text(name, exact=True)`, then
  `button:has-text('Select')`. Confirm dialogs use
  `[data-testid="dialog-prompt-ok-btn"]` (="Save"/"Ok") /
  `[data-testid="dialog-prompt-cancel-btn"]`.
  Available template names seen: `single_family_lease` (uploaded doc),
  `Agent automated single_family_lease`, `Agent automated multi_family_lease`,
  `1_Wiring Fraud Advisory Notice - eXp Connecticut`,
  `protectyourfamily_pamphlet_2026_3 Lead`, and the two lead/interest disclosures.
- Add participant: `[data-testid="add-role"]` ("+ Add Participant"). Opens a
  participant section `[data-testid="element-participant"]` with:
    - Role*: `[data-testid="role-selector-participant"]` (free-text role name)
    - First name: `[data-testid="name-participant"]` (input inside)
    - Last name:  `[data-testid="lastname-participant"]` (input inside)
    - Email:      `[data-testid="email-participant"]` (input inside; also
                  matchable as `get_by_role("textbox", name="Email *")`)
    - Type: `[data-testid="type-participant"]` = Reviewer / Signer / Distribution
    - Optional address/city/state/zip fields.
    - Save = `[data-testid="dialog-prompt-ok-btn"]`, Cancel = `dialog-prompt-cancel-btn`.
- Send: `button:has-text('Send Signing')`. Save draft: `[data-testid="signing-form-save-draft-btn"]`.
- A sent signing shows `Resend signing` / `Withdraw` instead of Send.

## RESOLVED — how the overlay's fields land on the filled lease
Do NOT use *Add Document(s) → Select Template* for the overlay (that adds a
separate document). Instead, on the uploaded lease's row, click the gear
(`button:has(path[d^='M12 15.75'])`) → "Apply Signing Overlay" → pick the
overlay by name → Select → a field-mapping dialog opens (Role Options:
Tenant (1)/(2)/Landlord + All Fields, all pre-checked) → Select again to
confirm. The overlay's fields land on the FILLED lease and its roles become the
signing's participant roles. Confirmed by a real end-to-end send (2026-07-18).

## Send flow quirks (learned on the first real send)
- Participant Role is a generic dropdown (Landlord, Tenant, ...). Options gain a
  "(+Add new)" suffix once a contact exists, so match the role as a PREFIX.
- Saving a participant whose email matches an existing contact pops a
  "Do you want to merge the following contacts?" dialog → click **No**.
- SmartMLS BLOCKS send if two participants share an email (after normalizing
  Gmail +tags) unless each has a phone number — real signers have distinct
  emails so this is normally moot; the test used three distinct inboxes.
- Clicking **Send Signing** pops a "Save Contact Group?" dialog → click **No**;
  the send only commits after this is answered. Success = the invite email
  ("eSigning Invitation | <signing name>") arrives from smartmls@propkit.io.

## Packet templates — BLOCKED on template roles (Jay's GUI task)
Verified live: adding a 2nd+ template pops a "Multiple Template Warning"
(Signing Flow formatting resets; roles collapse to Stage 1 / order Any) with a
**Proceed** button — the sender handles it. But the packet templates carry their
OWN roles which merge into the signing: `1_Wiring Fraud Advisory Notice - eXp
Connecticut` injects **Landlord (1), Landlord (2), Seller (1), Seller (2)** (it
was built for sales). For leases every packet template must use the SAME role
trio as the overlays — **Tenant (1), Tenant (2), Landlord** — so the roles merge
instead of demanding Seller signers. Fix each template's roles in Sign >
Templates (Forms), then re-test with:
  python lease_sender.py --job <job.json> --no-send
(protectyourfamily pamphlet added no roles; the two Disclosure templates were
never reached — verify their roles too.)

## Test artifacts to clean up (drafts created while mapping)
Sign > Drafts: 6x "Lease - 123 Test St, 1st Floor, Waterbury CT - One" +
"ZZ PACKET PROBE / SELECTOR TEST / OVERLAY TEST / PARTICIPANT TEST - delete me"
(pages 1-3). In Progress: withdraw "33 George St test" (Jay's codegen test) and
the sent test signing "Lease - 123 Test St, 1st Floor, Waterbury CT - One".
Leave "Lucille" and "57 New ST Angel" alone — REAL signings. Test contacts:
Matthew Como / Test Tenant One / Test Tenant Two in Contacts.
