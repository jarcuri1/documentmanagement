# Lease intake wizard — design notes (from Jay, 2026-07-19)

Replaces the typing-heavy `lease_intake.py` interview with tap-through box
selections. Output contract UNCHANGED: writes the same intake.json into
Dropbox\Leases\Intake that lease_fill consumes. Target surface: phone app
("New Lease" screen); optionally a terminal version first.

## Flow
1. **Box 1 — Lease type**: Single-family / Multi-family.
2. **Box 2 — Whose property?**
   - **Mine** = owned OR Premio-managed — everything in
     C:\AIAgents\shared\lease_folders.json (66 properties; tree
     personal/premio distinguishes owned vs managed for the RTS
     point-of-contact).
   - **Real-estate client** = a brokerage-side lease deal for a landlord
     client (NOT property management). Nothing is known up front, so this
     branch asks the FULL question set (property address, premises wording,
     owner/landlord name, landlord signer + email, utilities, ...). Jay:
     "built out slowly with more questions to fill in all the spots" — grow
     this branch iteratively. Filing also differs (no folder-map key).
3. **Box 3 — Property picker** (Mine only): list from lease_folders.json,
   filtered by Box 2.
4. **Box 4 — Unit picker**: the property's units{} from the folder map.
5. **Per-lease facts** (always asked): tenant name(s)/email(s) — MUST be
   distinct emails (SmartMLS demands phones when emails repeat) — term
   start/end, rent, deposit, **animals** (default none; else Name/Breed/Color
   per pet; multi-family template has pet lines, single-family template
   currently has NO pet section — template gap for Jay to fill).
6. **Property memory**: shared\lease_property_facts.json keyed by
   property_key+unit. First lease for a unit asks utilities / owner LLC /
   premises wording and SAVES them; later leases pre-fill and skip those
   questions. The system asks less the more it's used.

## Build order (after the live send test passes)
1. lease_property_facts.json store + read/merge in lease_fill normalization.
2. Terminal wizard (quick win, validates the flow).
3. Phone-app "New Lease" screen posting intake.json via the supervisor.
