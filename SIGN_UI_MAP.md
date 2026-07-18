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

## OPEN DESIGN QUESTION (blocks the sender rewrite)
Applying the overlay template via *Add Document(s) → Select Template* adds it as a
SEPARATE document next to the uploaded filled lease — it does NOT overlay its
fields onto the uploaded lease, and it does NOT auto-create participants.

Since the overlay was built on the BLANK reference PDF, this yields two lease
copies (filled-but-fieldless + fields-but-blank-data). Need Jay's intended
mechanism for landing the overlay's signature fields on the FILLED lease before
coding the assembly step. Candidates to investigate:
  - a per-document "apply template/fields to THIS document" action in the editor,
  - or building the field layout so the sender draws fields by coordinate,
  - or a different template type ("form" applied to an uploaded doc).

## Test artifacts to clean up (drafts I created while mapping)
Signings named `ZZ SELECTOR TEST - delete me` / `SELECTOR TEST` and draft id
50604. Signing 50600 is the (sent) test from Jay's codegen walkthrough.
