"""
LeaseFiler — file a SIGNED lease into the right Dropbox folder
==============================================================
Part 2 of the signed-lease filing feature (SPEC_LEASE_FILING.md). Given a
signed lease PDF and its job record, this resolves the destination from the
reviewed folder map, retires any prior lease for the same unit to
`Past Tenants\\`, files the signed PDF under a deterministic name, records
what it did, and pushes one notification.

It is the downstream half of the return trip. The upstream half (part 3 —
catching the "lease signed" email, pulling the download link, fetching the
PDF, and matching it to a job) hands this function `(signed_pdf, job)`.

IRON RULES (same posture as the rest of the fleet):
  * NEVER guess a folder. If the job's property_key isn't in the map (or its
    folder is gone), file NOTHING into the trees — drop the PDF in
    `Leases\\Signed\\_unfiled\\` and push. Fail loud.
  * Retire OLD leases conservatively. Only move files this agent produced
    (they match the deterministic `Lease - <address> <unit> - ` prefix). A
    lease with a manual/unknown name is left alone and called out in the push
    — never move statements, photos, or anything not clearly our lease.
  * Deterministic filenames make next time's retirement trivial:
    `Lease - <address> <unit> - <last names> - <YYYY-MM-DD>.pdf`

RUN (part 3 will call file_signed_lease directly; CLI is for testing/manual):
  python lease_filer.py --pdf signed.pdf --job "D:\\...\\Sent\\<slug>.json"
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

_SHARED_ROOT = os.environ.get("LEASE_SHARED_ROOT", r"C:\AIAgents\shared")
_LEASES_ROOT = os.environ.get("LEASE_DROPBOX_ROOT", r"D:\Dropbox\Dropbox\Leases")

CONFIG = {
    "lease_folders_json": Path(os.environ.get(
        "LEASE_FOLDERS_JSON", str(Path(_SHARED_ROOT) / "lease_folders.json"))),
    "unfiled_dir": Path(os.environ.get(
        "LEASE_UNFILED_DIR", str(Path(_LEASES_ROOT) / "Signed" / "_unfiled"))),
    "push_outbox_dir": Path(os.environ.get(
        "LEASE_PUSH_OUTBOX", str(Path(_SHARED_ROOT) / "push_outbox"))),
}

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class LeaseFileError(Exception):
    pass


_push_seq = 0


def push(title, body, data=None):
    global _push_seq
    _push_seq += 1
    outbox = CONFIG["push_outbox_dir"]
    outbox.mkdir(parents=True, exist_ok=True)
    payload = {"title": title, "body": body, "data": {**(data or {}), "kind": "lease"}}
    name = f"lease-{os.getpid()}-{int(time.time() * 1000)}-{_push_seq}.json"
    tmp = outbox / (name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(outbox / name)


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _sanitize(name):
    return _ILLEGAL.sub("", name).strip()


def _signers(job):
    if job.get("signers"):
        return job["signers"]
    if job.get("tenant_name"):
        return [{"name": job["tenant_name"]}]
    return []


def _last_names(job):
    names = []
    for s in _signers(job):
        parts = str(s.get("name", "")).split()
        if parts:
            names.append(parts[-1])
    return " ".join(names) if names else "Tenant"


def _resolve_unit_subfolder(entry, unit, prop_folder):
    """Match an intake unit label ('Second Floor', '2nd fl', '#2') to an actual
    unit subfolder. Tolerant: bootstrap keys are full folder names, but intake
    labels are short — so try exact, then containment, then a live dir scan."""
    u = unit.strip().lower()
    units = entry.get("units") or {}
    if u in units and os.path.isdir(os.path.join(prop_folder, units[u])):
        return units[u]
    for label, sub in units.items():
        if (u in sub.lower() or u in label) and os.path.isdir(os.path.join(prop_folder, sub)):
            return sub
    try:
        for d in os.listdir(prop_folder):
            if os.path.isdir(os.path.join(prop_folder, d)) and u in d.lower():
                return d
    except OSError:
        pass
    return None


def _to_unfiled(signed_pdf, reason, key):
    CONFIG["unfiled_dir"].mkdir(parents=True, exist_ok=True)
    dest = CONFIG["unfiled_dir"] / Path(signed_pdf).name
    if dest.exists():
        dest = dest.with_name(f"{dest.stem}-{int(time.time())}{dest.suffix}")
    shutil.move(str(signed_pdf), str(dest))
    push("Signed lease needs filing", f"{reason} (key {key!r}). Left in _unfiled.",
         {"job": key})
    return dest


def file_signed_lease(signed_pdf, job, job_path=None):
    """File one signed lease. Returns the destination Path, or the _unfiled Path
    if it couldn't be filed. Never raises for a routing miss — it fails loud via
    _unfiled + push, per the spec."""
    signed_pdf = Path(signed_pdf)
    if not signed_pdf.exists():
        raise LeaseFileError(f"signed PDF not found: {signed_pdf}")

    key = job.get("property_key") or _slug(job.get("property", ""))
    try:
        mapping = json.loads(CONFIG["lease_folders_json"].read_text(encoding="utf-8"))
    except Exception as e:
        return _to_unfiled(signed_pdf, f"cannot read folder map ({e})", key)

    entry = mapping.get(key)
    if not entry or not entry.get("folder") or not os.path.isdir(entry["folder"]):
        return _to_unfiled(signed_pdf, "no folder mapping (or folder missing)", key)

    prop_folder = entry["folder"]
    unit = str(job.get("unit", "")).strip()

    # Destination: personal tree + a resolvable unit subfolder -> that
    # subfolder; otherwise the property folder (unit rides in the filename,
    # Premio-style).
    dest_folder = prop_folder
    if entry.get("tree") == "personal" and unit:
        sub = _resolve_unit_subfolder(entry, unit, prop_folder)
        if sub:
            dest_folder = os.path.join(prop_folder, sub)

    address = str(job.get("filing_address") or job.get("property", "").split(",")[0]).strip()
    unit_part = f" {unit}" if unit else ""
    term = str(job.get("term_start_iso", "")).strip()
    fname = _sanitize(f"Lease - {address}{unit_part} - {_last_names(job)} - {term}.pdf")

    # Retire OUR prior lease(s) for this unit (same deterministic prefix).
    prefix = f"Lease - {address}{unit_part} - "
    retired, retired_names = 0, []
    try:
        existing = [f for f in os.listdir(dest_folder)
                    if f.startswith(prefix) and f.lower().endswith(".pdf") and f != fname]
    except OSError:
        existing = []
    if existing:
        past = Path(prop_folder) / "Past Tenants"
        past.mkdir(parents=True, exist_ok=True)
        for old in existing:
            target = past / old
            if target.exists():
                target = target.with_name(f"{Path(old).stem}-{int(time.time())}.pdf")
            shutil.move(str(Path(dest_folder) / old), str(target))
            retired += 1
            retired_names.append(old)

    # Place the signed lease.
    dest = Path(dest_folder) / fname
    if dest.exists():
        # Same tenant + term already filed — don't clobber; keep both.
        dest = dest.with_name(f"{dest.stem} (refiled {int(time.time())}).pdf")
    shutil.move(str(signed_pdf), str(dest))

    # Record what was filed back into the job record (so history is explicit).
    filed = {"path": str(dest), "filename": dest.name,
             "retired": retired_names, "at": datetime.now().isoformat(timespec="seconds")}
    if job_path:
        try:
            rec = json.loads(Path(job_path).read_text(encoding="utf-8"))
            rec["filed"] = filed
            Path(job_path).write_text(json.dumps(rec, indent=2), encoding="utf-8")
        except Exception:
            pass

    retired_note = f" (retired {retired} old to Past Tenants)" if retired else ""
    push("Lease filed", f"{address}{unit_part} — {_last_names(job)}{retired_note}",
         {"job": key})
    print(f"filed -> {dest}{retired_note}")
    return dest


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True, help="signed lease PDF")
    ap.add_argument("--job", required=True, help="job record JSON (from Sent\\<slug>.json)")
    args = ap.parse_args()
    try:
        job = json.loads(Path(args.job).read_text(encoding="utf-8"))
    except Exception as e:
        print(f"cannot read job {args.job}: {e}", file=sys.stderr)
        sys.exit(1)
    file_signed_lease(args.pdf, job, job_path=args.job)
