"""
LeaseFill — fills the single-family lease and queues it for approval
====================================================================
Piece 3 of the pipeline (see HANDOFF_LEASE_AGENT.md). Takes a structured
intake, overlays the tenant/lease data onto the landlord's OWN approved
lease PDF, and drops the completed (unsigned) lease + a job file into
Dropbox\\Leases\\Pending, then writes an approval card onto the existing
approvals rails so the supervisor can push it to your phone.

Why overlay the real PDF instead of rebuilding it: the legal text stays
byte-for-byte the landlord's approved document. We only draw values onto
the blank lines. Signature / initial blocks are left untouched -- those are
placed by Authentisign, not here.

FAIL-CLOSED: the blanks are located by the label to their left (robust to
minor template edits), and if a REQUIRED blank can't be found the whole job
aborts. A half-filled lease is never produced or queued.

SINGLE vs MULTI tenant: the template has two tenant slots. `tenants` may hold
one or two people; each becomes an Authentisign signer, so each needs an
email. Slot two is left blank for a single-tenant lease.

SENSITIVE DATA: SSNs, if supplied, are written ONLY onto the lease PDF. They
are never written into the job file or the approval card (both of which sync
to Dropbox / get pushed to a phone). SSN is optional -- omit it and the line
is left blank.

RUN:
  python lease_fill.py --intake intake.json      # fill + queue for approval
  python lease_fill.py --intake intake.json --dry-run   # build PDF only,
                        # write it next to the intake, queue NOTHING

INTAKE FORMAT (intake.json):
{
  "landlord": "Premio Property Management LLC",
  "property": "123 Main St Apt 2, Waterbury CT",
  "premises_address": "123 Main St Apt 2, Waterbury, CT 06702",
  "agreement_date": "2026-07-16",           # ISO or free text; omit = today
  "term_start": "2026-08-01",
  "term_end":   "2027-07-31",
  "rent": "2,150",
  "deposit": "2,150",
  "utilities": { "water": "City", "wastewater": "Sewer", "fuel": "Oil" },
  "tenants": [
    { "name": "John Smith", "email": "jsmith@example.com",
      "address": "123 Main St Apt 2", "city_state_zip": "Waterbury, CT 06702",
      "ssn": "XXX-XX-1234" }
  ]
}
"""

import argparse
import json
import re
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

import fitz  # PyMuPDF

# ----------------------------------------------------------------------
# CONFIG — env-overridable; defaults mirror lease_watcher / the handoff
# ----------------------------------------------------------------------
import os

_SHARED_ROOT = os.environ.get("LEASE_SHARED_ROOT", r"C:\AIAgents\shared")
_LEASES_ROOT = os.environ.get("LEASE_DROPBOX_ROOT", r"C:\Users\Jay\Dropbox\Leases")


def _p(env_key, *default_parts, root):
    v = os.environ.get(env_key)
    return Path(v) if v else Path(root, *default_parts)


CONFIG = {
    "template_pdf": Path(os.environ.get(
        "LEASE_TEMPLATE_PDF", str(Path(__file__).with_name("templates") / "single_family_lease.pdf"))),
    "pending_dir":     _p("LEASE_PENDING_DIR", "Pending", root=_LEASES_ROOT),
    "pending_cards_dir": _p("LEASE_CARDS_DIR", "approvals", "pending", root=_SHARED_ROOT),
    # Dropbox-relative root used to build the phone-openable path in the card
    "dropbox_rel_root": os.environ.get("LEASE_DROPBOX_REL_ROOT", "/Leases"),
    "text_color": (0.0, 0.0, 0.55),   # dark blue, so fills read as filled-in
    "font": "helv",
    "font_size": 10.0,
}


class LeaseFillError(Exception):
    pass


# ----------------------------------------------------------------------
# Blank detection — find every underscore run with the text to its left
# ----------------------------------------------------------------------
def _blank_runs(page):
    runs = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            chars = [c for sp in line["spans"] for c in sp["chars"]]
            i, n = 0, len(chars)
            while i < n:
                if chars[i]["c"] == "_":
                    j = i
                    while j < n and (chars[j]["c"] == "_" or
                                     (chars[j]["c"] == " " and j + 1 < n and chars[j + 1]["c"] == "_")):
                        j += 1
                    x0 = chars[i]["bbox"][0]
                    x1 = chars[j - 1]["bbox"][2]
                    y1 = max(c["bbox"][3] for c in chars[i:j])
                    left = "".join(c["c"] for c in chars[:i])
                    runs.append({"x0": x0, "x1": x1, "y": y1, "w": x1 - x0, "left": left})
                    i = j
                else:
                    i += 1
    return runs


def _marker(page, text):
    """Baseline anchor for a list marker like '1.' / '2.' (name goes after it)."""
    for w in page.get_text("words"):
        if w[4] == text:
            return (w[0], w[3])  # x0, y-bottom
    return None


def resolve_fields(doc):
    """Locate every fillable blank on pages 1-2 and return field -> (x, y).

    Raises LeaseFillError if a structurally-required blank is missing (the
    template changed) -- we never silently drop a field on a legal doc.
    """
    p1_runs, p2_runs = _blank_runs(doc[0]), _blank_runs(doc[1])
    f = {}

    def one(runs, pred, why):
        hits = [r for r in runs if pred(r)]
        if not hits:
            raise LeaseFillError(f"could not locate blank for {why}")
        return hits[0]

    def ordered(runs, pred):
        return sorted([r for r in runs if pred(r)], key=lambda r: r["y"])

    # Page 1 singletons
    f["agreement_date"] = one(p1_runs, lambda r: "entered into on" in r["left"], "agreement date")
    f["landlord"] = one(p1_runs, lambda r: "Landlord:" in r["left"], "landlord")
    f["premises"] = one(p1_runs, lambda r: r["left"].strip() == "" and r["x0"] < 80 and r["w"] > 300, "premises address")

    # Page 1 per-tenant (document order = tenant 1 then tenant 2)
    addr = ordered(p1_runs, lambda r: "Address:" in r["left"])
    csz = ordered(p1_runs, lambda r: r["left"].strip() == "" and r["x0"] > 100)
    ssn = ordered(p1_runs, lambda r: "Social Security Number" in r["left"])
    if len(addr) < 2 or len(csz) < 2 or len(ssn) < 2:
        raise LeaseFillError("expected two tenant slots (address/city-state-zip/ssn) on page 1")
    f["t_addr"], f["t_csz"], f["t_ssn"] = addr, csz, ssn
    f["t_name_marker"] = [_marker(doc[0], "1."), _marker(doc[0], "2.")]

    # Page 2 singletons
    f["term_start"] = one(p2_runs, lambda r: "commence on" in r["left"], "term start")
    f["term_end"] = one(p2_runs, lambda r: r["left"].strip() == "" and r["x0"] < 80 and r["y"] < 110, "term end")
    f["rent"] = one(p2_runs, lambda r: "the sum of $" in r["left"], "monthly rent")
    f["deposit"] = one(p2_runs, lambda r: "security deposit of $" in r["left"], "security deposit")

    # Page 2 utility checkboxes, in document order
    boxes = doc[1].search_for("[ ]")
    if len(boxes) < 7:
        raise LeaseFillError(f"expected 7 utility checkboxes, found {len(boxes)}")
    f["boxes"] = boxes  # City, Well, Sewer, Septic, Oil, Gas, Propane
    return f


# ----------------------------------------------------------------------
# Rendering the values onto the page
# ----------------------------------------------------------------------
def _put(page, x, y_bottom, text):
    page.insert_text((x, y_bottom - 2.5), str(text),
                     fontsize=CONFIG["font_size"], fontname=CONFIG["font"],
                     color=CONFIG["text_color"])


def _check(page, box_rect):
    x = (box_rect[0] + box_rect[2]) / 2 - 2.6
    _put(page, x, box_rect[3], "X")


_UTIL_INDEX = {"city": 0, "well": 1, "sewer": 2, "septic": 3, "oil": 4, "gas": 5, "propane": 6}


def fill_pdf(data, out_path):
    doc = fitz.open(str(CONFIG["template_pdf"]))
    fld = resolve_fields(doc)
    p1, p2 = doc[0], doc[1]

    _put(p1, fld["agreement_date"]["x0"] + 2, fld["agreement_date"]["y"], data["agreement_date"])
    _put(p1, fld["landlord"]["x0"] + 2, fld["landlord"]["y"], data["landlord"])
    _put(p1, fld["premises"]["x0"] + 2, fld["premises"]["y"], data["premises_address"])

    for ti, t in enumerate(data["tenants"][:2]):
        mk = fld["t_name_marker"][ti]
        if mk:
            _put(p1, mk[0] + 14, mk[1], t["name"])
        _put(p1, fld["t_addr"][ti]["x0"] + 2, fld["t_addr"][ti]["y"], t.get("address", ""))
        _put(p1, fld["t_csz"][ti]["x0"] + 2, fld["t_csz"][ti]["y"], t.get("city_state_zip", ""))
        if t.get("ssn"):  # optional; PDF only, never persisted elsewhere
            _put(p1, fld["t_ssn"][ti]["x0"] + 2, fld["t_ssn"][ti]["y"], t["ssn"])

    _put(p2, fld["term_start"]["x0"] + 2, fld["term_start"]["y"], data["term_start"])
    _put(p2, fld["term_end"]["x0"] + 2, fld["term_end"]["y"], data["term_end"])
    _put(p2, fld["rent"]["x0"] + 2, fld["rent"]["y"], data["rent"])
    _put(p2, fld["deposit"]["x0"] + 2, fld["deposit"]["y"], data["deposit"])

    util = data.get("utilities", {})
    for kind in ("water", "wastewater", "fuel"):
        val = (util.get(kind) or "").strip().lower()
        if not val:
            continue
        if val not in _UTIL_INDEX:
            raise LeaseFillError(f"unknown {kind} option {val!r} "
                                 f"(expected one of {sorted(_UTIL_INDEX)})")
        _check(p2, fld["boxes"][_UTIL_INDEX[val]])

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    doc.close()


# ----------------------------------------------------------------------
# Intake -> normalized data
# ----------------------------------------------------------------------
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _fmt_date(v):
    if not v:
        return date.today().strftime("%B %-d, %Y")
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(v, fmt).strftime("%B %-d, %Y")
        except ValueError:
            pass
    return str(v)  # already free text; use as-is


def normalize_intake(raw):
    if not isinstance(raw, dict):
        raise LeaseFillError("intake is not a JSON object")
    tenants = raw.get("tenants") or []
    if not (1 <= len(tenants) <= 2):
        raise LeaseFillError("intake must have 1 or 2 tenants")
    for i, t in enumerate(tenants, 1):
        if not t.get("name"):
            raise LeaseFillError(f"tenant {i} missing name")
        if not t.get("email") or not _EMAIL_RE.match(t["email"]):
            raise LeaseFillError(f"tenant {i} missing/invalid email: {t.get('email')!r}")
    required = ["landlord", "property", "premises_address", "term_start", "term_end", "rent", "deposit"]
    missing = [k for k in required if not raw.get(k)]
    if missing:
        raise LeaseFillError(f"intake missing fields: {missing}")
    return {
        "landlord": raw["landlord"],
        "property": raw["property"],
        "premises_address": raw["premises_address"],
        "agreement_date": _fmt_date(raw.get("agreement_date")),
        "term_start": _fmt_date(raw["term_start"]),
        "term_end": _fmt_date(raw["term_end"]),
        "rent": raw["rent"],
        "deposit": raw["deposit"],
        "utilities": raw.get("utilities", {}),
        "tenants": tenants,
    }


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def job_id_for(data):
    surname = data["tenants"][0]["name"].split()[-1]
    return f"{_slug(data['property'])}-{_slug(surname)}"[:80]


def build_job(data, job_id, pdf_path):
    """The job contract lease_watcher / lease_sender consume. NO SSN here."""
    signers = [{"name": t["name"], "email": t["email"]} for t in data["tenants"]]
    surname = data["tenants"][0]["name"].split()[-1]
    return {
        "property": data["property"],
        "signing_name": f"Lease - {data['property']} - {surname}",
        "signers": signers,
        # legacy single-signer mirror so an older consumer still works
        "tenant_name": signers[0]["name"],
        "tenant_email": signers[0]["email"],
        "pdf_path": str(pdf_path),
    }


# ----------------------------------------------------------------------
# APPROVALS-RAILS CONTRACT — the approval card. Mirror your real card shape
# here (this is the write-side twin of lease_watcher's FLEET CONTRACT block)
# and nowhere else. NO SSN is ever placed on the card.
# ----------------------------------------------------------------------
def build_card(data, job_id, pdf_path):
    rel = f"{CONFIG['dropbox_rel_root'].rstrip('/')}/Pending/{Path(pdf_path).name}"
    return {
        "kind": "lease",
        "agent": "lease",
        "id": job_id,
        "job": job_id,
        "action_options": ["send", "reject"],
        "title": f"Lease ready to send: {data['property']}",
        "fields": {
            "property": data["property"],
            "tenants": [{"name": t["name"], "email": t["email"]} for t in data["tenants"]],
            "rent": f"${data['rent']}/mo",
            "deposit": f"${data['deposit']}",
            "term": f"{data['term_start']} – {data['term_end']}",
        },
        "pdf_dropbox_path": rel,   # the app resolves this to a tap-to-open link
        "created": datetime.now().isoformat(timespec="seconds"),
    }


def _atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def process_intake(intake_path, dry_run=False):
    raw = json.loads(Path(intake_path).read_text(encoding="utf-8"))
    data = normalize_intake(raw)
    job_id = job_id_for(data)

    if dry_run:
        out = Path(intake_path).with_name(f"{job_id}.pdf")
        fill_pdf(data, out)
        print(f"[DRY-RUN] filled PDF written to {out} (nothing queued)")
        return out

    pending = CONFIG["pending_dir"]
    pdf_path = pending / f"{job_id}.pdf"
    # Fill to a temp then move, so a half-written PDF is never visible in Pending.
    tmp_pdf = pending / f".{job_id}.pdf.tmp"
    fill_pdf(data, tmp_pdf)
    Path(tmp_pdf).replace(pdf_path)

    job = build_job(data, job_id, pdf_path)
    _atomic_write(pending / f"{job_id}.json", json.dumps(job, indent=2))

    # Card LAST: only advertise the job once the PDF + job file are in place.
    card = build_card(data, job_id, pdf_path)
    _atomic_write(CONFIG["pending_cards_dir"] / f"{job_id}.json", json.dumps(card, indent=2))

    print(f"queued {job_id}: {pdf_path}  (+ job file + approval card)")
    return job_id


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--intake", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    try:
        process_intake(args.intake, dry_run=args.dry_run)
    except LeaseFillError as e:
        print(f"LEASE FILL ABORTED: {e}", file=sys.stderr)
        sys.exit(1)
