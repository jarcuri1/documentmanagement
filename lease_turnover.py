"""Classify a just-filed lease signing for Apartments.com payment actions.

Jay's rules (2026-08-10):
- NEW tenant (row previously held someone else): terminate the old tenant's
  future payments + cancel their residency, then set the new tenant up to
  pay online. Both confirm-first — nothing touches Apartments.com until Jay
  approves the card.
- RENEWAL (same tenant re-signing): no payment/residency changes; only if
  the rent amount changed, queue a payment-amount update for verification.
- VACANT row (no previous tenant): setup-only, nothing to cancel.

Pure logic — queueing/cards happen in lease_filer. Run this file directly
for the self-test.
"""

import re

# Sheet tenant cells are informal: "Katherine Turner (WHA)", "Ibrahim &
# Issa Dababneh #B", "Brittany & Jose Bolorin". Strip decorations before
# comparing.
_DECOR = re.compile(r"\([^)]*\)|#\S+|\bwha\b|\bs8\b", re.I)


def _name_tokens(s):
    s = _DECOR.sub(" ", str(s or ""))
    return {t for t in re.split(r"[^a-z]+", s.lower()) if len(t) >= 2 and t != "and"}


def same_tenant(prev, new):
    """True when the previous sheet tenant and the new lease signer(s) are
    the same people. Token-overlap: informal sheet strings rarely match the
    lease's legal names exactly ("Bobby" vs "Matteson Robert" is a real
    mismatch we must call DIFFERENT; "Katelyn Goff" vs "Goff Katelyn" the
    same). Two shared tokens = same person; one shared token counts only
    when either side has just one token total (single-name sheet entries
    like "Sheila")."""
    a, b = _name_tokens(prev), _name_tokens(new)
    if not a or not b:
        return False
    shared = a & b
    if len(shared) >= 2:
        return True
    return len(shared) == 1 and (len(a) == 1 or len(b) == 1)


def _money(v):
    try:
        return round(float(re.sub(r"[^0-9.]", "", str(v))) or 0)
    except ValueError:
        return 0


def classify_turnover(job):
    """job = the Sent record AFTER filing + sheet update (needs
    sheet_update.previous and .requested). Returns a dict describing what
    Apartments.com needs, or None when there is nothing to do (renewal with
    unchanged rent, or no sheet coords = client lease / old app build)."""
    upd = (job.get("sheet_update") or {})
    req, prev = upd.get("requested") or {}, upd.get("previous") or {}
    if not req.get("propertyAddress"):
        return None
    new_tenant = req.get("tenantName", "")
    old_tenant = _DECOR.sub(" ", str(prev.get("tenant") or "")).strip()
    new_rent = _money(req.get("rent"))
    # Sheet rent lives in col F (tenant portion) — G is the Section-8 total.
    old_rent = _money(prev.get("col_F"))

    base = {
        "property": req["propertyAddress"],
        "unit": req.get("unitName", ""),
        "new_tenant": new_tenant,
        "old_tenant": old_tenant or None,
        "rent": new_rent,
        "lease_start": req.get("leaseStart", ""),
        "lease_end": req.get("leaseEnd", ""),
    }
    if old_tenant and same_tenant(old_tenant, new_tenant):
        if new_rent and old_rent and new_rent != old_rent:
            return {**base, "kind": "renewal_rent_change", "old_rent": old_rent,
                    "actions": ["update_payment_amount"]}
        # Renewal, rent unchanged: no card, but the filer pushes a note so
        # Jay knows it was checked.
        return {**base, "kind": "renewal_no_change", "actions": []}
    if old_tenant:
        return {**base, "kind": "turnover",
                "actions": ["cancel_old_payments", "end_old_residency",
                            "setup_new_payments"]}
    return {**base, "kind": "move_in", "actions": ["setup_new_payments"]}


if __name__ == "__main__":
    cases = [
        # (prev tenant, new tenantName, prev col_F, new rent, expected kind)
        ("Sheila", "Matteson Robert & Matteson Heather", "1650", "2500", "turnover"),
        ("Katelyn Goff", "Katelyn Goff", "2500", "2500", "renewal_no_change"),
        ("Katelyn Goff", "Goff Katelyn", "2500", "2600", "renewal_rent_change"),
        ("", "New Person", "", "1200", "move_in"),
        ("Katherine Turner (WHA)", "Katherine Turner", "1300", "1300", "renewal_no_change"),
        ("Ibrahim & Issa Dababneh #B", "John Smith", "1700", "1800", "turnover"),
        ("Sheila", "Sheila Johnson", "1650", "1650", "renewal_no_change"),
    ]
    for prev, new, old_rent, rent, want in cases:
        job = {"sheet_update": {
            "requested": {"propertyAddress": "1 Test St", "unitName": "Main",
                          "tenantName": new, "rent": rent},
            "previous": {"tenant": prev, "col_F": old_rent}}}
        got = classify_turnover(job)
        kind = got["kind"] if got else None
        status = "ok " if kind == want else "FAIL"
        print(f"{status} {prev!r} -> {new!r}: {kind}")
