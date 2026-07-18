"""
LeaseIntake — ask the questions, write the intake the fill agent uses
====================================================================
The day-one entry point for starting a lease. It interviews you for
everything the lease needs — lease type, address, term, rent, deposit, the
utility checkboxes (City/Well water, Sewer/Septic, Oil/Gas/Propane), and each
tenant — then writes an intake.json into Leases\\Intake, where the fill agent
(lease_fill.py --drain / --watch) picks it up, fills the lease, and queues it
for your phone approval.

This is the interim for Samantha /chat: when /chat lands, its lease skill
writes the identical intake.json to the same folder and this script is no
longer needed. The JSON contract does not change.

RUN (on the fleet PC):
  python lease_intake.py            # interview -> writes the intake
  python lease_intake.py --print    # interview -> print the JSON, write nothing
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

_LEASES_ROOT = os.environ.get("LEASE_DROPBOX_ROOT", r"D:\Dropbox\Dropbox\Leases")
INTAKE_DIR = Path(os.environ.get("LEASE_INTAKE_DIR", str(Path(_LEASES_ROOT) / "Intake")))
DEFAULT_LANDLORD = os.environ.get("LEASE_DEFAULT_LANDLORD", "Premio Property Management LLC")


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def ask(prompt, default="", required=True, validate=None):
    hint = f" [{default}]" if default else ""
    while True:
        val = input(f"{prompt}{hint}: ").strip()
        if not val and default:
            val = default
        if not val and not required:
            return ""
        if not val:
            print("  (required)")
            continue
        if validate:
            err = validate(val)
            if err:
                print(f"  {err}")
                continue
        return val


def ask_choice(prompt, options):
    opts = " / ".join(f"{i+1}) {o}" for i, o in enumerate(options))
    while True:
        val = input(f"{prompt}  {opts}: ").strip().lower()
        if val.isdigit() and 1 <= int(val) <= len(options):
            return options[int(val) - 1]
        for o in options:
            if val == o.lower():
                return o
        print(f"  pick 1-{len(options)}")


def _valid_email(v):
    return None if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v) else "not a valid email"


def _valid_date(v):
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            datetime.strptime(v, fmt)
            return None
        except ValueError:
            pass
    return "use YYYY-MM-DD or MM/DD/YYYY"


def interview():
    print("\n=== New lease ===\n")
    lease_type = "single_family" if ask_choice("Lease type", ["single-family", "multi-family"]) == "single-family" else "multi_family"

    landlord = ask("Landlord", default=DEFAULT_LANDLORD)
    prop = ask("Property (full, e.g. '61 Cliff St, 2nd Floor, Naugatuck CT')")
    premises = ask("Premises address as it should print on the lease", default=prop)
    key_default = _slug(re.split(r",", prop)[0])
    property_key = ask("Folder-map key (for filing; must match lease_folders.json)", default=key_default)
    unit = ask("Unit label (e.g. 'Second Floor', '#2') — blank if none", required=False)
    filing_address = ask("Street address for the filed filename (no unit/city)",
                         default=re.split(r",", prop)[0].strip())

    term_start = ask("Term start", validate=_valid_date)
    term_end = ask("Term end", validate=_valid_date)
    rent = ask("Monthly rent (number, no $)")
    deposit = ask("Security deposit", default=rent)

    utilities = {}
    if lease_type == "single_family":
        print("\n-- Utilities (single-family checkboxes) --")
        utilities = {
            "water": ask_choice("Water", ["City", "Well"]),
            "wastewater": ask_choice("Wastewater", ["Sewer", "Septic"]),
            "fuel": ask_choice("Fuel", ["Oil", "Gas", "Propane"]),
        }

    tenants = []
    n = int(ask_choice("How many tenants", ["1", "2"]))
    for i in range(1, n + 1):
        print(f"\n-- Tenant {i} --")
        t = {
            "name": ask(f"Tenant {i} full name"),
            "email": ask(f"Tenant {i} email", validate=_valid_email),
            "address": ask(f"Tenant {i} street address", default=filing_address),
            "city_state_zip": ask(f"Tenant {i} city, state, zip"),
        }
        ssn = ask(f"Tenant {i} SSN (optional — goes on the lease only, never stored elsewhere)", required=False)
        if ssn:
            t["ssn"] = ssn
        tenants.append(t)

    return {
        "lease_type": lease_type,
        "landlord": landlord,
        "property": prop,
        "premises_address": premises,
        "property_key": property_key,
        "unit": unit,
        "filing_address": filing_address,
        "term_start": term_start,
        "term_end": term_end,
        "rent": rent,
        "deposit": deposit,
        "utilities": utilities,
        "tenants": tenants,
    }


def main():
    just_print = "--print" in sys.argv
    intake = interview()

    surname = intake["tenants"][0]["name"].split()[-1]
    slug = f"{_slug(intake['property'])}-{_slug(surname)}"[:80]
    text = json.dumps(intake, indent=2)

    print("\n=== Review ===")
    print(text)
    if input("\nWrite this intake? (y/N): ").strip().lower() != "y":
        print("Cancelled.")
        return

    if just_print:
        print("\n(--print) not written.")
        return

    INTAKE_DIR.mkdir(parents=True, exist_ok=True)
    out = INTAKE_DIR / f"{slug}.json"
    out.write_text(text, encoding="utf-8")
    print(f"\nWrote {out}")
    print("The fill agent will pick it up, fill the lease, bundle the packet, and "
          "send it to your phone for approval.")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
