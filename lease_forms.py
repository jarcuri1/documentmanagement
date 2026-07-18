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

import os
from pathlib import Path

import fitz  # PyMuPDF

_TEMPLATE_DIR = Path(os.environ.get("LEASE_TEMPLATE_DIR", str(Path(__file__).with_name("templates"))))

# The property manager / point of contact printed on the Rental Terms Summary.
# Override with LEASE_POINT_OF_CONTACT, or per-lease via intake "point_of_contact".
POINT_OF_CONTACT = os.environ.get(
    "LEASE_POINT_OF_CONTACT",
    "Premio Property Management, (203) 666-5300, PremioPropertyManagement@gmail.com")


class LeaseFormError(Exception):
    pass


def _find(names, prefix):
    return next((n for n in names if n and n.startswith(prefix)), None)


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
        _find(names, "Point of Contact"): data.get("point_of_contact") or POINT_OF_CONTACT,
        "fill_5": f"{data['term_start']} – {data['term_end']}",   # Lease Term
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


# Registry of fillable supporting forms. Add (name, filler_fn) to expand.
SUPPORTING_FORMS = [
    ("rental-terms-summary", fill_rental_terms_summary),
]


def fill_supporting_forms(data, out_dir, job_id):
    """Fill every registered supporting form and return the list of paths."""
    out = []
    for name, fn in SUPPORTING_FORMS:
        out.append(fn(data, Path(out_dir) / f"{job_id}-{name}.pdf"))
    return out
