"""
RentXref — cross-check apartments.com rents against the Combined Empire sheet
============================================================================
Apartments.com is the source of truth for what a unit actually rents for.
The Google Sheet drifts — especially the "Premio Property Management" tab.
This flags the differences for Jay to review.

IRON RULE: this NEVER writes to the sheet. It reads both sides, matches units,
and posts ONE approval card listing what disagrees. Every correction is Jay's
to make by hand. `actions` on the card are review-only (done/skip) — there is
deliberately no "send" that could push a value anywhere.

WHERE THE RENT COMES FROM (no new scraping, no new login):
  ApartmentsAgent already downloads the landlord payments export every day at
  18:00 (`download_payments_csv` -> shared\\statements_tmp\\apts-payments-<year>.csv).
  That export carries `Type == "Monthly Rent Due"` rows: one per unit per month,
  with the charged amount in `Debit Amt`. The newest such row per unit IS the
  current rent. Reading a CSV that's already on disk beats driving the
  Edit-Rent wizard 57 times — apartments.com blocks headless, so any live
  browse would need a real Chrome window and Jay's Google session.

MATCHING (unit labels disagree wildly between the two systems):
  1. property + normalized unit label   ('2nd Floor Front' == 'Second fl front #2')
  2. property + tenant surname          (the reliable key — every rent row
                                         carries the tenant in `Initiated By`)
  Anything still unmatched is REPORTED, never guessed. A unit on apartments.com
  with no sheet row is itself a finding — that's the sheet being out of date.

SECTION 8: the Premio tab splits rent into col F (Section 8 portion) and col G
(tenant portion). Apartments.com bills only what the tenant owes, so a unit is
counted as agreeing if its rent matches EITHER col G or F+G. Both numbers go on
the card so Jay can see which reading applies.

RUN
  python rent_xref.py            # compare + post the card
  python rent_xref.py --dry      # print the report, post nothing
"""

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

# The supervisor captures stdout through a pipe, so Windows Python falls back
# to cp1252 and the first box-drawing char in the report kills the whole run
# (and then kills the error handler printing the failure). Force UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SHARED = Path(os.environ.get("LEASE_SHARED_ROOT", r"C:\AIAgents\shared"))
APPROVALS = SHARED / "approvals"
REPORT_FILE = SHARED / "rent_xref_report.json"
# NOT a "lease-" id: lease_watcher.py globs lease-*.json out of the shared
# decisions folder and pushes "Unexpected decision" for any verb it doesn't
# own. This card's done/skip would trip that every time Jay closed it.
CARD_PREFIX = "rx-"
CARD_ID = CARD_PREFIX + "RENTXREF"

SHEET_ID = os.environ.get("LEASE_SHEET_ID", "13gBHnNLf8PVD1j7locnJZdDTBndMadWCpLW4GbDnK50")
# col indexes are 0-based; header row is skipped
TABS = {
    # "Combined Empire" (owned): ...,Deposit,Rent Section 8,Rent Tenant,... (split since 2026-08-23)
    "Combined Empire": {"rent": [6, 5], "label": "owned"},
    # "Premio Property Management": ...,Deposit,Rent Section 8,Rent Tenant,...
    "Premio Property Management": {"rent": [6, 5], "label": "managed"},
}

# Export goes stale if the daily apartments run has been failing.
STALE_DAYS = 3

# Jay bills on his own behalf, so his name is on every charge row and on the
# offline payments he records. It can never identify a tenant.
LANDLORD_NAMES = {"jason arcuri", "jay arcuri"}

# A charge this small isn't a rent — it's a Section 8 residual, a proration, or
# a $1 placeholder. Comparing it to a full sheet rent produces noise, so these
# get their own bucket instead of screaming "$2,015 off".
MIN_PLAUSIBLE_RENT = 300

# Approval cards are clipped to 3000 chars by the app; build to fit.
BODY_CAP = 3000

ORDINALS = {"1st": "first", "2nd": "second", "3rd": "third", "4th": "fourth",
            "1": "first", "2": "second", "3": "third", "4": "fourth",
            "one": "first", "two": "second", "three": "third", "four": "fourth"}


# ── helpers ──────────────────────────────────────────────────────────────────
def street_key(s):
    """Must stay in lockstep with streetKey() in supervisor.js and
    _street_key() in apartments_agent.py — range addresses skip the second
    number ('117-119 Straits' -> '117 straits')."""
    m = re.match(r"\s*(\d+[a-z]?)[\s-]+(?:\d+[\s-]+)?([a-z]+)", str(s).lower())
    return f"{m.group(1)} {m.group(2)}" if m else str(s).lower()[:12]


def unit_key(s, prop=""):
    """Squash a unit label to something comparable across the two systems.
    'Second fl front #2', '2nd Floor Front', '14 Sylvan Second Floor Front'
    all land on 'secondfloorfront'."""
    s = str(s or "").strip().lower()
    if prop:   # labels sometimes repeat the property ('61 Cliff Second Floor')
        head = re.match(r"\s*(\d+[a-z]?)[\s-]+(?:\d+[\s-]+)?([a-z]+)", str(prop).lower())
        if head:
            s = re.sub(rf"^\s*{re.escape(head.group(1))}\s+{re.escape(head.group(2))}\w*\s*", "", s)
            s = re.sub(rf"^\s*{re.escape(head.group(2))}\w*\s*", "", s)
    s = re.sub(r"#\s*\d+\s*$", "", s)
    s = re.sub(r"\b(apt|apartment|unit|no|number)\b", " ", s)
    s = re.sub(r"\bfl\b|\bflr\b|\bfloor\b", " floor ", s)
    s = re.sub(r"\bback\b", "rear", s)
    s = re.sub(r"\bfrnt\b", "front", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return "".join(ORDINALS.get(t, t) for t in s.split()) or "main"


_NAME_NOISE = {"jr", "sr", "ii", "iii", "and", "the", "llc", "mr", "mrs", "ms"}


def name_keys(s):
    """Every surname-ish token in a tenant field, so 'Asia Stanford & Carl
    McElya' can be matched from either side of the ampersand."""
    s = re.sub(r"[^a-z ]+", " ", str(s or "").lower())
    return {t for t in s.split() if len(t) > 2 and t not in _NAME_NOISE}


def money(s):
    s = re.sub(r"[^0-9.\-]", "", str(s or ""))
    try:
        return round(float(s), 2)
    except Exception:
        return None


def dollars(v):
    return "—" if v is None else f"${v:,.2f}".replace(".00", "")


def push(title, body):
    outbox = SHARED / "push_outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    payload = {"title": title, "body": body, "data": {"kind": "lease"}}
    name = f"lease-{os.getpid()}-{int(time.time() * 1000)}.json"
    tmp = outbox / (name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(outbox / name)


# ── side A: apartments.com ───────────────────────────────────────────────────
def find_export():
    """Newest apts-payments-<year>.csv the apartments agent has downloaded."""
    d = SHARED / "statements_tmp"
    files = sorted(d.glob("apts-payments-*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def read_apartments(path):
    """Latest 'Monthly Rent Due' charge per unit. Returns {key: row}.

    Tenant names come ONLY from `Payment` rows. Every 'Monthly Rent Due' row is
    initiated by the landlord (the system bills on his behalf), so its
    `Initiated By` is always Jay — useless for identifying who lives there, and
    actively harmful as a match key since it would match every unit at once.
    Jay's own name is dropped from payment rows too (he records offline rent)."""
    out = {}
    names = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        prop, unit = r.get("Property") or "", r.get("Unit") or ""
        who = (r.get("Initiated By") or "").strip()
        if who and (r.get("Type") or "").strip() == "Payment" and who.lower() not in LANDLORD_NAMES:
            names.setdefault((prop, unit), set()).add(who)
        if (r.get("Type") or "").strip() != "Monthly Rent Due":
            continue
        k = (street_key(prop), unit_key(unit, prop))
        when = (r.get("Initiated On") or "")
        if k in out and when <= out[k]["as_of"]:
            continue
        out[k] = {"street": street_key(prop), "unit_key": k[1], "property": prop.split(", US")[0],
                  "unit": unit.strip(), "rent": money(r.get("Debit Amt")), "as_of": when,
                  "tenant": "", "_pu": (prop, unit)}
    for v in out.values():
        v["tenant"] = " & ".join(sorted(names.get(v["_pu"], set())))
        v.pop("_pu", None)
    return out


# ── side B: the Combined Empire spreadsheet ──────────────────────────────────
def read_tab(name):
    url = (f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq"
           f"?tqx=out:csv&sheet={urllib.parse.quote(name)}")
    with urllib.request.urlopen(url, timeout=45) as r:
        return list(csv.reader(io.StringIO(r.read().decode("utf-8"))))


def read_sheet():
    """{key: row} across both tabs. Property rows carry col A; unit rows don't."""
    out = {}
    for tab, cfg in TABS.items():
        prop = None
        for row in read_tab(tab)[1:]:
            g = lambda i: row[i].strip() if len(row) > i else ""
            if g(0):
                prop = g(0)
                continue
            if not prop or not (g(1) or g(2)):
                continue
            rents = [money(g(i)) for i in cfg["rent"]]
            out[(street_key(prop), unit_key(g(1), prop))] = {
                "tab": tab, "kind": cfg["label"], "property": prop, "unit": g(1),
                "tenant": g(2), "rent": rents[0], "rent_s8": rents[1] if len(rents) > 1 else None,
                "street": street_key(prop),
            }
    return out


# ── matching ─────────────────────────────────────────────────────────────────
def match(apts, sheet):
    """Layered: unit label, then tenant surname within the same property.
    Returns (pairs, apts_only, sheet_only). Nothing is ever guessed."""
    pairs, used = [], set()
    for k, a in apts.items():
        if k in sheet:
            pairs.append((a, sheet[k], "unit"))
            used.add(k)
    for k, a in apts.items():
        if k in used:
            continue
        want = name_keys(a.get("tenant"))
        if not want:
            continue
        hits = [sk for sk, s in sheet.items()
                if sk not in used and s["street"] == a["street"] and (want & name_keys(s["tenant"]))]
        if len(hits) == 1:            # ambiguous -> leave it unmatched, don't guess
            pairs.append((a, sheet[hits[0]], "tenant"))
            used.add(k)
            used.add(hits[0])
    matched_apts = {id(p[0]) for p in pairs}
    apts_only = [a for a in apts.values() if id(a) not in matched_apts]
    matched_sheet = {id(p[1]) for p in pairs}
    sheet_only = [s for s in sheet.values() if id(s) not in matched_sheet]
    return pairs, apts_only, sheet_only


def compare(pairs):
    """A managed unit agrees if apartments.com matches the tenant portion OR
    the Section 8 + tenant total (both tabs since 2026-08-23)."""
    agree, flags, partial = [], [], []
    for a, s, how in pairs:
        ar = a["rent"]
        if ar is None:
            continue
        # Both tabs carry the split now: agree if apartments.com matches the
        # tenant portion OR the Section 8 + tenant total.
        tenant_part, s8 = s["rent"], s["rent_s8"]
        total = round((tenant_part or 0) + (s8 or 0), 2)
        ok = ar == tenant_part or (s8 and ar == total)
        sheet_shown = tenant_part
        rec = {
            "property": s["property"], "unit": s["unit"], "apts_unit": a["unit"],
            "tenant": s["tenant"] or a["tenant"], "tab": s["tab"], "kind": s["kind"],
            "apts_rent": ar, "sheet_rent": sheet_shown, "sheet_s8": s.get("rent_s8"),
            "delta": None if sheet_shown is None else round(ar - sheet_shown, 2),
            "as_of": a["as_of"], "matched_by": how,
        }
        if ok:
            agree.append(rec)
        elif ar < MIN_PLAUSIBLE_RENT and (sheet_shown or 0) >= MIN_PLAUSIBLE_RENT:
            partial.append(rec)      # tenant residual, not a rent change
        else:
            flags.append(rec)

    # Group by property, worst property first, so transposed units (61 Cliff's
    # first/third floor swap) land next to each other instead of pages apart.
    worst = {}
    for f in flags:
        worst[f["property"]] = max(worst.get(f["property"], 0), abs(f["delta"] or 0))
    flags.sort(key=lambda f: (-worst[f["property"]], f["property"], str(f["unit"])))
    partial.sort(key=lambda f: (f["property"], str(f["unit"])))
    return agree, flags, partial


# ── report ───────────────────────────────────────────────────────────────────
def build_body(res):
    L = []
    stale = res["export_age_days"]
    L.append(f"Apartments.com rents as of the {res['export_date']} export"
             + (f"  ⚠️ {stale} days old — the daily 18:00 apartments run may be failing" if stale > STALE_DAYS else "")
             + ".")
    L.append(f"Checked {res['compared']} units — {len(res['agree'])} agree, {len(res['flags'])} disagree"
             + (f", {len(res['partial'])} partial-payment (check separately)." if res["partial"] else "."))
    L.append("Apartments.com is right; the sheet is what needs fixing. Nothing here is auto-corrected.")

    if res["flags"]:
        L.append("\n━━ RENT MISMATCHES ━━")
        cur = None
        for f in res["flags"]:
            if f["property"] != cur:
                cur = f["property"]
                L.append(f"\n{cur}   [{f['tab']}]")
            d = f["delta"]
            arrow = "▲" if d and d > 0 else "▼"
            note = ""
            if f["kind"] == "managed" and f["sheet_s8"]:
                note = f"   (sheet also shows {dollars(f['sheet_s8'])} Section 8)"
            L.append(f"  {f['unit'] or f['apts_unit']} — {f['tenant'] or '?'}")
            L.append(f"     apartments.com {dollars(f['apts_rent'])}   vs sheet {dollars(f['sheet_rent'])}"
                     f"   {arrow} {dollars(abs(d)) if d is not None else '?'}{note}")

    if res["partial"]:
        L.append("\n━━ TENANT PAYS ONLY PART — CHECK, DON'T ASSUME ━━")
        L.append("(apartments.com bills a small residual here; the rest is likely Section 8. "
                 "Not necessarily a sheet error.)")
        for f in res["partial"]:
            L.append(f"  {f['property']} — {f['unit'] or f['apts_unit']} — {f['tenant'] or '?'}")
            L.append(f"     apartments.com bills {dollars(f['apts_rent'])}   sheet rent {dollars(f['sheet_rent'])}")

    # The mismatches above are the point of the card and always survive. The
    # two reference lists below fill whatever room is left and then say how
    # many they dropped — better a clean "+9 more" than a sentence cut in half.
    footer = "\nDone/Skip closes this card. Nothing is written to the sheet either way."
    budget = BODY_CAP - len("\n".join(L)) - len(footer) - 120

    def add_list(header, note, items, render):
        nonlocal budget
        if not items:
            return
        head = [f"\n{header}", note]
        cost = len("\n".join(head))
        if cost > budget:
            return
        budget -= cost
        L.extend(head)
        shown = 0
        for it in items:
            line = render(it)
            if len(line) + 1 > budget:
                break
            budget -= len(line) + 1
            L.append(line)
            shown += 1
        if shown < len(items):
            L.append(f"  …and {len(items) - shown} more — full list in shared\\rent_xref_report.json")

    add_list("━━ ON APARTMENTS.COM, NOT FOUND ON THE SHEET ━━",
             "(either missing from the sheet, or the unit is labelled too differently to match)",
             res["apts_only"],
             lambda a: f"  {a['property']} — {a['unit'] or '(no unit)'} — {a['tenant'] or '?'}   {dollars(a['rent'])}")
    add_list("━━ ON THE SHEET, NO RENT CHARGE ON APARTMENTS.COM ━━",
             "(not collected through apartments.com, or vacant — FYI only)",
             res["sheet_only_managed"],
             lambda s: f"  {s['property']} — {s['unit']} — {s['tenant'] or '?'}   sheet {dollars(s['rent'])}")

    L.append(footer)
    return "\n".join(L)


def clear_prior():
    """Nobody else owns rx-*, so this sweeps its own leavings: a decision file
    just means Jay closed the last card. Drop it and any stale pending copy so
    each run posts one current card, never a stack of them."""
    for sub in ("decisions", "pending", "done"):
        d = APPROVALS / sub
        if not d.exists():
            continue
        for p in d.glob(f"{CARD_PREFIX}*.json"):
            try:
                p.unlink()
            except Exception:
                pass


def post_card(res):
    APPROVALS.joinpath("pending").mkdir(parents=True, exist_ok=True)
    n = len(res["flags"])
    item = {
        # NOT agent/kind "lease": ApprovalsScreen routes both to LeaseCard,
        # whose "Send" releases a real signing. kind "rent_xref" gets the
        # read-only RentXrefCard (Reviewed / Later, nothing else).
        "id": CARD_ID, "agent": "rent_xref", "kind": "rent_xref",
        "created_at": datetime.now().isoformat(),
        "title": (f"💰 Rent cross-check — {n} mismatch{'es' if n != 1 else ''} to review"
                  if n else "💰 Rent cross-check — sheet matches apartments.com"),
        "from": "Lease Agent (review only — the sheet is never changed)",
        "subject": "Rent cross-check",
        "body": build_body(res)[:3000],
        "actions": ["done", "skip"],
    }
    (APPROVALS / "pending" / f"{CARD_ID}.json").write_text(json.dumps(item, indent=2), encoding="utf-8")
    return item


def run(dry=False):
    export = find_export()
    if not export:
        msg = ("No apartments.com payments export on disk yet — it lands from the daily "
               "18:00 Apartments.com run. Run that agent once, then re-run the rent check.")
        print("⚠️ " + msg)
        if not dry:
            push("💰 Rent cross-check skipped", msg)
        return 0

    age = (datetime.now() - datetime.fromtimestamp(export.stat().st_mtime)).days
    apts = read_apartments(export)
    sheet = read_sheet()
    pairs, apts_only, sheet_only = match(apts, sheet)
    agree, flags, partial = compare(pairs)

    res = {
        "generated_at": datetime.now().isoformat(),
        "export": str(export), "export_date": datetime.fromtimestamp(export.stat().st_mtime).strftime("%b %d"),
        "export_age_days": age,
        "apartments_units": len(apts), "sheet_units": len(sheet),
        "compared": len(agree) + len(flags) + len(partial),
        "agree": agree, "flags": flags, "partial": partial,
        "apts_only": sorted(apts_only, key=lambda a: (a["property"], a["unit"])),
        # Owned units are often collected outside apartments.com; only the
        # managed side is interesting as "missing", and even then it's FYI.
        "sheet_only_managed": sorted([s for s in sheet_only if s["kind"] == "managed"],
                                     key=lambda s: (s["property"], s["unit"])),
    }

    REPORT_FILE.write_text(json.dumps(res, indent=2), encoding="utf-8")
    print(build_body(res))
    print(f"\n[matched by unit label: {sum(1 for p in pairs if p[2] == 'unit')}, "
          f"by tenant name: {sum(1 for p in pairs if p[2] == 'tenant')}, "
          f"unmatched: {len(apts_only)}]")

    if dry:
        print("\n(--dry: no card posted, no push)")
        return 0

    clear_prior()
    post_card(res)
    n = len(flags)
    push("💰 Rent cross-check",
         f"{n} rent mismatch{'es' if n != 1 else ''} between apartments.com and the sheet — card posted for review."
         if n else "Sheet matches apartments.com on every unit I could compare.")
    print(f"\n🔔 Card {CARD_ID} posted ({n} mismatches)")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="print the report, post no card")
    a = ap.parse_args()
    try:
        sys.exit(run(dry=a.dry))
    except Exception as e:
        print(f"⚠️ rent_xref failed: {e}")
        try:
            push("⚠️ Rent cross-check failed", str(e)[:200])
        except Exception:
            pass
        sys.exit(1)
