"""
LeaseForms — fill the supporting FILLABLE PDFs that ride with a lease
====================================================================
Some packet documents aren't signature templates you just add — they're
fillable PDFs that need DATA put in (no signatures). Right now that's the CT
Standardized Rental Terms Summary Form (Public Act 25-44), which state law
requires as the cover page of every new/renewed lease from 2026-04-01.

This module fills those forms from the same lease data and returns the paths,
so the fill agent can add them to the signing packet as uploads. It's a
registry so more fillable forms can be added later without touching the rest.

Fields are filled via PyMuPDF (appearance streams are regenerated, so the
values render in any viewer, including SmartMLS Sign).
"""

import json
import os
from pathlib import Path

import fitz  # PyMuPDF

_TEMPLATE_DIR = Path(os.environ.get("LEASE_TEMPLATE_DIR", str(Path(__file__).with_name("templates"))))
_FOLDERS_JSON = Path(os.environ.get("LEASE_FOLDERS_JSON", r"C:\AIAgents\shared\lease_folders.json"))

# Point of Contact on the Rental Terms Summary depends on WHO manages the
# property: our own properties (folder-map tree "personal") -> Matt & Jay;
# properties managed for clients under the management company (tree "premio")
# -> Premio Property Management. Set both here (env-overridable).
POC_OWNED = os.environ.get(
    "LEASE_POC_OWNED",
    "Matthew Como (203) 232-0077; Jason Arcuri (203) 910-7602")
POC_PREMIO = os.environ.get(
    "LEASE_POC_PREMIO",
    "Premio Property Management, (203) 666-5300, PremioPropertyManagement@gmail.com")


class LeaseFormError(Exception):
    pass


def _find(names, prefix):
    return next((n for n in names if n and n.startswith(prefix)), None)


def _property_tree(property_key):
    """personal (ours) / premio (managed) / '' from the folder map."""
    try:
        m = json.loads(_FOLDERS_JSON.read_text(encoding="utf-8"))
        return (m.get(property_key) or {}).get("tree", "")
    except Exception:
        return ""


def _point_of_contact(data):
    if data.get("point_of_contact"):
        return data["point_of_contact"]
    tree = data.get("management_tree") or _property_tree(data.get("property_key", ""))
    return POC_PREMIO if tree == "premio" else POC_OWNED


def fill_rental_terms_summary(data, out_pdf):
    """Fill the CT PA 25-44 Rental Terms Summary from the lease data."""
    tpl = _TEMPLATE_DIR / "rental_terms_summary.pdf"
    if not tpl.exists():
        raise LeaseFormError(f"Rental Terms Summary template not found: {tpl}")
    doc = fitz.open(str(tpl))
    pg = doc[0]
    widgets = list(pg.widgets())
    names = [w.field_name for w in widgets]

    tenants = "; ".join(t["name"] for t in data["tenants"])
    ls = data.get("landlord_signer") or {}
    # Name of Landlord = the owner (LLC) AND the individual signing on its behalf.
    landlord = data["landlord"] + (f"; signed by {ls['name']}" if ls.get("name") else "")

    values = {
        _find(names, "Premises"): data["premises_address"],
        _find(names, "Name of Tenant"): tenants,
        "fill_3": landlord,                                  # Name of Landlord
        _find(names, "Point of Contact"): _point_of_contact(data),
        "fill_5": f"{data['term_start']} - {data['term_end']}",   # Lease Term (ASCII hyphen)
        _find(names, "Total periodic rent"): f"${data['rent']} monthly",
        _find(names, "Other Charges"): data.get("other_charges") or "None",
    }

    for w in widgets:
        v = values.get(w.field_name)
        if v is not None:
            w.field_value = str(v)
            w.update()

    Path(out_pdf).parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_pdf))
    doc.close()
    return str(out_pdf)


# The licensee's initials, stamped on the Disclosure of Interest's
# "(Licensee to initial below as applicable)" blanks.
LICENSEE_INITIALS = os.environ.get("LEASE_LICENSEE_INITIALS", "JA")
LICENSEE_NAME = os.environ.get("LEASE_LICENSEE_NAME", "Jason Arcuri")
# Signature image for the Licensee line (Jay signs the disclosure as sender;
# every send is gated by his phone approval). PNG dropped by Jay.
_LICENSEE_SIG = _TEMPLATE_DIR / "jay_signature.png"


def fill_disclosure_of_interest(data, out_pdf):
    """Fill the CT Disclosure of Present or Contemplated Interest (flat PDF,
    stamped by coordinate): the Subject Property Address plus the licensee
    initials on the applicable items. Which items depends on who runs the
    property (verified against Jay's live demo, 2026-07-20):
      owned (tree 'personal'): item 2 (Seller's/Landlord's Agent) + its
        'An entity in which Licensee has a substantial ownership interest'
        sub-line + item 3 (ownership interest)
      managed (tree 'premio'): item 3 only.
    Replaces the old SmartMLS Sign template for this form — its fill-in boxes
    are canvas-drawn and reject synthetic input, so the PDF layer it is."""
    tpl = _TEMPLATE_DIR / "disclosure_of_interest.pdf"
    if not tpl.exists():
        raise LeaseFormError(f"Disclosure of Interest template not found: {tpl}")
    tree = data.get("management_tree") or _property_tree(data.get("property_key", "")) or "personal"
    # (x, baseline_y) stamp points measured from the template geometry.
    marks = {
        "item2":        (84, 333),    # '2.___' initial blank
        "item2_entity": (111, 383),   # '___An entity in which Licensee has ...'
        "item3":        (86, 409),    # '3. ___' initial blank
    }
    picked = ["item2", "item2_entity", "item3"] if tree != "premio" else ["item3"]

    doc = fitz.open(str(tpl))
    pg = doc[0]
    pg.insert_text((203, 144), data["premises_address"], fontname="helv",
                   fontsize=10, color=(0, 0, 0))
    for name in picked:
        x, y = marks[name]
        pg.insert_text((x, y), LICENSEE_INITIALS, fontname="helv",
                       fontsize=10, color=(0, 0, 0))
    # Licensee signature block: signature image (when provided) + date on the
    # 'Licensee / Date' lines, printed name on its line. Tenant/Landlord
    # acknowledgment fields come from the SmartMLS Sign overlay, not here.
    from datetime import date as _date
    today = f"{_date.today().month}/{_date.today().day}/{_date.today().year}"
    if _LICENSEE_SIG.exists():
        # signature sits on the line ending at y=448.6 (x 72-324)
        pg.insert_image(fitz.Rect(80, 412, 240, 447), filename=str(_LICENSEE_SIG),
                        keep_proportion=True)
    pg.insert_text((436, 445), today, fontname="helv", fontsize=10, color=(0, 0, 0))
    pg.insert_text((75, 484), LICENSEE_NAME, fontname="helv", fontsize=10,
                   color=(0, 0, 0))
    Path(out_pdf).parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_pdf))
    doc.close()
    # Read-back verification: the address and every initial must be present.
    chk = fitz.open(str(out_pdf))
    txt = chk[0].get_text()
    chk.close()
    if data["premises_address"].split(",")[0] not in txt:
        raise LeaseFormError("Disclosure of Interest: address stamp did not land")
    if txt.count(LICENSEE_INITIALS) < len(picked):
        raise LeaseFormError("Disclosure of Interest: initial stamps did not land")
    return str(out_pdf)


def fill_lead_disclosure_rentals(data, out_pdf):
    """Pre-fill the federal lead-paint disclosure (RENTALS version, flat PDF):
      - (e) agent's acknowledgment initials (Jay is the lessor's agent)
      - Lessor's Agent signature line: signature image (when provided) + date
      - Address of Property/Unit at the bottom
    Landlord initials ((a)(ii)/(b)(ii) + signature) and tenant initials
    ((c)(ii)/(d) + signatures) are e-sign fields from the SmartMLS overlay
    'agent automated lead_rentals' — people must initial their own statements.
    Replaces the old Sign template, which was the SALES version of the form."""
    tpl = _TEMPLATE_DIR / "lead_disclosure_rentals.pdf"
    if not tpl.exists():
        raise LeaseFormError(f"Lead disclosure (rentals) template not found: {tpl}")
    from datetime import date as _date
    today = f"{_date.today().month}/{_date.today().day}/{_date.today().year}"
    doc = fitz.open(str(tpl))
    pg = doc[0]
    # (e) — Lessor's Agent has informed the lessor of their obligations
    pg.insert_text((60, 479), LICENSEE_INITIALS, fontname="helv", fontsize=10, color=(0, 0, 0))
    # Certification row 3: Lessor's Agent signature (x24-155) + Date (x156-281)
    if _LICENSEE_SIG.exists():
        pg.insert_image(fitz.Rect(28, 618, 150, 648), filename=str(_LICENSEE_SIG),
                        keep_proportion=True)
    pg.insert_text((162, 646), today, fontname="helv", fontsize=10, color=(0, 0, 0))
    # Address of Property/Unit (bottom line)
    pg.insert_text((30, 681), data["premises_address"], fontname="helv",
                   fontsize=10, color=(0, 0, 0))
    Path(out_pdf).parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_pdf))
    doc.close()
    chk = fitz.open(str(out_pdf))
    txt = chk[0].get_text()
    chk.close()
    if data["premises_address"].split(",")[0] not in txt or LICENSEE_INITIALS not in txt:
        raise LeaseFormError("Lead disclosure (rentals): stamps did not land")
    return str(out_pdf)


# Registry of fillable supporting forms. Add (name, filler_fn) to expand.
SUPPORTING_FORMS = [
    ("rental-terms-summary", fill_rental_terms_summary),
    ("disclosure-of-interest", fill_disclosure_of_interest),
    ("lead-disclosure-rentals", fill_lead_disclosure_rentals),
]


def fill_supporting_forms(data, out_dir, job_id):
    """Fill every registered supporting form and return the list of paths."""
    out = []
    for name, fn in SUPPORTING_FORMS:
        out.append(fn(data, Path(out_dir) / f"{job_id}-{name}.pdf"))
    return out
