# Handoff: LeaseAgent ↔ Samantha & Supervisor

**From:** the LeaseAgent build (lease_fill.py, lease_watcher.py, lease_sender.py)
**To:** whoever owns the Samantha app and the Supervisor approvals rails
**Purpose:** LeaseAgent is built and working end-to-end EXCEPT for two seams
where it touches your systems. I coded those seams against *assumed* schemas,
isolated in one block each, so they're trivial to correct. This doc shows you
exactly what LeaseAgent writes and reads, gives you my assumptions to red-line,
and lists the few artifacts I need back to lock it to your real contracts.

**The fastest possible reply:** paste **one real decision file** and **one real
pending card** from an existing agent (fb-agent, social, whatever), plus answers
to the numbered questions. That's enough for me to finish.

---

## What LeaseAgent does (so the seams make sense)

```
Fill agent  ──writes──▶  D:\Dropbox\Dropbox\Leases\Pending\<job>.pdf + <job>.json
     │                    and an approval CARD to  shared\approvals\pending\
     │
  [ Jay approves on his phone in the Samantha app ]
     │
Supervisor  ──writes──▶  a DECISION file to  shared\approvals\decisions\
     │
Watcher     ──reads the decision──▶  claims the job (Pending → Sending) ──▶
Sender      ──drives Authentisign, sends the lease, files it to  Sent\ / Failed\
```

- Job types: single-family and multifamily leases, one or two tenants (each a
  signer). The lease PDF is produced by filling Jay's own Word template.
- The **job JSON is the single source of truth** for what gets sent. The
  approval decision is a bare go/no-go — it carries no lease data.
- Anti-double-send is already handled (claim-first move to `Sending\`); a lease
  can never be sent twice even on a crash.

---

## SEAM 1 — Approval DECISIONS  (Supervisor writes, Watcher reads)

LeaseAgent's watcher polls `shared\approvals\decisions\` and acts on decisions
addressed to the lease agent. It **only ever reads** this folder — it never
writes or deletes here, and it ignores decisions belonging to other agents.

**My assumed decision-file shape** (please correct):
```json
{
  "kind": "lease",              // or "agent": "lease"
  "action": "send",             // send | reject | feedback
  "job": "123-main-st-smith",   // must map to Pending\123-main-st-smith.json
  "feedback": "..."             // only for action == feedback
}
```

**Questions — Supervisor:**
1. Paste one **real decision file** verbatim (any agent). I need the exact keys.
2. Which field carries the **action/verdict**, and what are its exact values
   (`send`/`reject`/`approve`/`deny`/…)?
3. How does a decision say **which item/job** it belongs to — a `job` field, an
   `id` that matches the pending card's id, the filename, something else?
4. How is the decision **addressed to an agent** so the lease watcher only takes
   its own — a `kind`/`agent` field? What value identifies the lease agent?
5. **Lifecycle:** after Jay decides, who removes the decision file and the
   pending card — the Supervisor, or the consuming agent? (Right now the watcher
   keeps its own local "already-handled" ledger and touches nothing shared. If
   your convention is that the consumer deletes/moves the decision, tell me and
   I'll follow it instead.)
6. Is there a stable **unique decision id** I can dedupe on (so the same
   approval is never executed twice)?

---

## SEAM 2 — Approval CARD  (Fill agent writes, Samantha renders)

When a lease is filled, LeaseAgent writes a card to `shared\approvals\pending\`
so the Supervisor can push it and Jay can approve it in the Samantha app.

**My assumed card shape** (please correct):
```json
{
  "kind": "lease",
  "agent": "lease",
  "id": "123-main-st-smith",
  "job": "123-main-st-smith",
  "action_options": ["send", "reject"],
  "title": "Lease ready to send: 123 Main St Apt 2, Waterbury CT",
  "fields": {
    "lease_type": "single_family",
    "property": "123 Main St Apt 2, Waterbury CT",
    "tenants": [{ "name": "John Smith", "email": "jsmith@example.com" }],
    "rent": "$1,850/mo",
    "deposit": "$1,850",
    "term": "August 1, 2026 – July 31, 2027"
  },
  "pdf_dropbox_path": "/Leases/Pending/123-main-st-smith.pdf",
  "created": "2026-07-16T10:42:40"
}
```

**Questions — Samantha / Supervisor:**
7. Paste one **real pending card** verbatim. I need the exact keys and the file
   naming convention in `shared\approvals\pending\`.
8. What makes the app render a **lease card** and NOT the email card? (The
   LeaseAgent handoff explicitly warned about the early bug where the wrong card
   type rendered — I want to feed the exact `kind`/`agent`/type values that make
   it render correctly with Send/Reject actions.)
9. How does the card give Jay a **tap-to-open link to the filled PDF** on his
   phone? Options I can support:
   - a **Dropbox path** the app resolves itself (what I do now:
     `pdf_dropbox_path`), or
   - a **pre-made Dropbox share URL** I generate and put in the card (tell me the
     field name), or
   - something else.
10. Exact field names the card must use for the body the app displays
    (property, tenants, rent, term, etc.) — or is a generic `fields` object fine?
11. Which **actions** should a lease card offer — just `send`/`reject`, or also
    `feedback` (send-back-for-changes)? What key lists them?

---

## SEAM 3 — Notifications / push  (Supervisor Expo pipeline)

Both agents currently append a line to `shared\notifications\lease_agent.jsonl`
(`{ts, agent, level, job, message}`) for the old notifier to push. The LeaseAgent
handoff said push now goes through the **Supervisor's Expo pipeline**.

**Questions — Supervisor:**
12. What is the **real way to trigger a phone push** now — still that jsonl, a
    different file/folder, or an API/queue call? Give me the exact
    location/shape and I'll point both agents at it.
13. Any required fields (severity, title, deep-link back into the app) you want
    in the notification?

---

## SEAM 4 — Intake entry point  (Samantha /chat)

The fill agent currently takes a structured `intake.json`. The open question from
the LeaseAgent handoff is how Jay's tenant info actually arrives.

**Questions — Samantha:**
14. How should Jay start a lease — a **freeform message to Samantha /chat**
    ("new single-family lease, 12 Elm, John Smith jsmith@…, $1850, 8/1/26–7/31/27,
    city water/septic/oil"), or a **structured form**?
15. If it's chat: how does the parsed result reach the fill agent — the app
    writes an `intake.json` to a folder LeaseAgent watches, calls it, or drops a
    Firestore doc? Tell me the mechanism and I'll build the front-end to match.

For reference, the intake the fill agent needs to end up with:
```json
{
  "lease_type": "single_family",        // or "multi_family"
  "landlord": "Premio Property Management LLC",
  "property": "123 Main St Apt 2, Waterbury CT",
  "premises_address": "123 Main St Apt 2, Waterbury, CT 06702",
  "term_start": "2026-08-01", "term_end": "2027-07-31",
  "rent": "1,850", "deposit": "1,850",
  "utilities": { "water": "City", "wastewater": "Septic", "fuel": "Oil" },
  "tenants": [
    { "name": "John Smith", "email": "jsmith@example.com",
      "address": "123 Main St Apt 2", "city_state_zip": "Waterbury, CT 06702",
      "ssn": "optional — goes on the lease only, never stored" }
  ]
}
```

---

## For reference — the job-file contract (LeaseAgent-internal)

This is what the fill agent writes to `Pending\<job>.json` and the sender
consumes. Included so you can see nothing lease-specific needs to live on your
rails — the decision just needs to point at the job by id.
```json
{
  "property": "123 Main St Apt 2, Waterbury CT",
  "signing_name": "Lease - 123 Main St Apt 2, Waterbury CT - Smith",
  "signers": [{ "name": "John Smith", "email": "jsmith@example.com" }],
  "pdf_path": "D:\\Dropbox\\Dropbox\\Leases\\Pending\\123-main-st-smith.pdf"
}
```

---

## Summary — what I need back

- [ ] **One real decision file** (Seam 1) + answers to Q1–Q6
- [ ] **One real pending card** (Seam 2) + answers to Q7–Q11
- [ ] **Push mechanism** (Seam 3) — Q12–Q13
- [ ] **Intake entry point** (Seam 4) — Q14–Q15

With the two pasted examples alone I can lock both contract blocks; the rest
tightens the edges. Nothing else in the lease pipeline is waiting on you.
