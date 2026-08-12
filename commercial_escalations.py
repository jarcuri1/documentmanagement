"""Commercial lease escalation checker (fleet: 1st of the month, 9:15).

Reads shared\\commercial_leases.json (the same facts Samantha answers from),
works out what each commercial tenant SHOULD be paying today per the lease's
built-in schedule, and compares against the master sheet. A mismatch puts an
`aptpay-` card on the approvals rail: approve = sheet fixed (rentOnly) +
Apartments.com payment-amount job queued for the payments agent. Also nags
about expirations and renewal-notice windows inside 90 days.

Never touches anything without Jay approving the card.
"""

import json
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

from lease_filer import _read_sheet_row, _APPROVALS, push

SHARED = Path(os.environ.get("FLEET_SHARED", r"C:\AIAgents\shared"))
LEASES_FILE = SHARED / "commercial_leases.json"
STATE_FILE = SHARED / "commercial_escalations_state.json"


def money(v):
    try:
        return round(float(re.sub(r"[^0-9.]", "", str(v)) or 0))
    except ValueError:
        return 0


def scheduled_rent(schedule, today):
    cur = None
    for step in schedule:
        if date.fromisoformat(step["from"]) <= today:
            cur = step["rent"]
    return cur


def card_escalation(lease, sheet_rent, due_rent, state):
    cid = f"aptpay-esc-{lease['key']}-{int(time.time())}"
    prior = state.get(lease["key"], {})
    if prior.get("rent") == due_rent:
        return None   # already carded this amount; Jay skipped or it's pending
    plan = {
        "kind": "escalation",
        "property": lease["sheet"]["property"],
        "unit": lease["sheet"]["unit"],
        "new_tenant": lease["tenant"].split("(")[0].strip(),
        "old_tenant": None,
        "rent": due_rent,
        "lease_start": "", "lease_end": lease.get("expires", ""),
        "actions": ["update_payment_amount"],
        "sheet_fix": {**lease["sheet"], "rent": due_rent},
    }
    card = {
        "id": cid, "agent": "lease", "kind": "apartments_payments",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "title": f"Lease escalation: {plan['new_tenant']} -> ${due_rent}",
        "subject": f"Built-in rent increase: {lease['premises']}",
        "from": "Commercial lease schedule",
        "body": (f"{plan['new_tenant']} at {lease['premises']}.\n"
                 f"The lease schedule puts rent at ${due_rent} now, but the "
                 f"books say ${sheet_rent}.\n\nOn approve:\n"
                 f"  - fix the sheet to ${due_rent}\n"
                 f"  - queue the Apartments.com payment-amount update\n\n"
                 f"Nothing happens until you approve."),
        "actions": ["approve", "skip"],
        "fields": plan,
    }
    pend = _APPROVALS / "pending"
    pend.mkdir(parents=True, exist_ok=True)
    tmp = pend / f"{cid}.json.tmp"
    tmp.write_text(json.dumps(card, indent=2), encoding="utf-8")
    tmp.replace(pend / f"{cid}.json")
    state[lease["key"]] = {"rent": due_rent,
                           "at": datetime.now().isoformat(timespec="seconds")}
    return cid


def main():
    today = date.today()
    data = json.loads(LEASES_FILE.read_text(encoding="utf-8"))
    state = {}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    lines, warnings, carded = [], [], 0
    for lease in data.get("leases", []):
        due = scheduled_rent(lease.get("schedule", []), today)
        sheet = lease.get("sheet") or {}
        if due and sheet.get("unit"):
            try:
                row = _read_sheet_row(sheet["tab"], sheet["property"], sheet["unit"])
                sheet_rent = money(row.get("col_G") or row.get("col_F"))
            except Exception as e:
                warnings.append(f"{lease['key']}: sheet row unreadable ({e})")
                sheet_rent = None
            if sheet_rent is not None:
                if sheet_rent == due:
                    lines.append(f"{lease['key']}: ${due} correct")
                else:
                    lines.append(f"{lease['key']}: lease says ${due}, books say ${sheet_rent} -> card")
                    if card_escalation(lease, sheet_rent, due, state):
                        carded += 1
        exp = lease.get("expires")
        if exp:
            days = (date.fromisoformat(exp) - today).days
            if days < 0:
                warnings.append(f"{lease['key']}: lease EXPIRED {exp} — tenant on holdover, needs a new lease")
            elif days <= 90:
                warnings.append(f"{lease['key']}: lease ends {exp} ({days} days) — renewal-notice window is open")
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    body = "\n".join(lines) or "no schedules to check"
    if warnings:
        body += "\n\nWATCH:\n" + "\n".join(f"- {w}" for w in warnings)
    if carded or warnings:
        push("Commercial leases: action needed" if carded else "Commercial leases: watch list",
             body, {"kind": "lease"})
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
