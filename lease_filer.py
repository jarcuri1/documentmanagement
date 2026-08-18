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

# Premio app API — writes the tenant row on Jay's master Google Sheet.
_PREMIO_APP = os.environ.get("PREMIO_APP_URL",
                             "https://stalwart-truffle-2dd64a.netlify.app")
_SHEET_ID = os.environ.get("LEASE_SHEET_ID",
                           "13gBHnNLf8PVD1j7locnJZdDTBndMadWCpLW4GbDnK50")
_SHEET_TABS = {"owned": "Combined Empire", "premio": "Premio Property Management"}


def _read_sheet_row(tab, property_addr, unit_name):
    """Current values of the unit row (link-shared CSV export) BEFORE we write,
    so the job record holds an undo trail. Same row-walk as edit-tenant.js:
    find the property in column A, then the unit in column B under it.
    Returns {tenant, phone, deposit, ...} or None."""
    import csv
    import io
    import urllib.parse
    import urllib.request
    url = (f"https://docs.google.com/spreadsheets/d/{_SHEET_ID}/gviz/tq"
           f"?tqx=out:csv&sheet={urllib.parse.quote(_SHEET_TABS[tab])}")
    with urllib.request.urlopen(url, timeout=30) as r:
        rows = list(csv.reader(io.StringIO(r.read().decode("utf-8"))))
    found = False
    for row in rows[1:]:
        prop = (row[0] if len(row) > 0 else "").strip()
        unit = (row[1] if len(row) > 1 else "").strip()
        if prop == property_addr:
            found = True
        elif prop and found:
            break   # walked into the next property
        if found and unit == unit_name:
            g = lambda i: row[i].strip() if len(row) > i else ""
            return {"tenant": g(2), "phone": g(3), "deposit": g(4),
                    "col_F": g(5), "col_G": g(6)}
    return None


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


def _money(v):
    """'$2,500' -> 2500 (int when whole); '' / junk -> None."""
    s = re.sub(r"[^0-9.]", "", str(v or ""))
    if not s:
        return None
    try:
        f = float(s)
        return int(f) if f == int(f) else f
    except ValueError:
        return None


# Local Dropbox folder root — maps a filed path to its Dropbox-relative path.
_DBX_LOCAL_ROOT = os.environ.get("LEASE_DROPBOX_LOCAL_ROOT", r"D:\Dropbox\Dropbox")

# Who may open lease links besides Jay (links are restricted, not public):
# Matt Como's accounts, per Jay 2026-07-23.
_LINK_VIEWERS = [e.strip() for e in os.environ.get(
    "LEASE_LINK_VIEWERS",
    "mattcomo87@gmail.com,mattcomocarpentry@gmail.com").split(",") if e.strip()]


def make_lease_url(local_pdf):
    """Shared Dropbox link for a filed lease (sheet col AA -> the app's Lease
    button). Auth: LEASE_DROPBOX_REFRESH_TOKEN + LEASE_DROPBOX_APP_KEY/
    LEASE_DROPBOX_APP_SECRET (long-lived, preferred) or LEASE_DROPBOX_TOKEN
    (raw access token — Dropbox expires these in ~4h). Degrades to '' —
    filing and the sheet update never depend on it."""
    try:
        rel = "/" + str(Path(local_pdf).resolve().relative_to(
            Path(_DBX_LOCAL_ROOT).resolve())).replace("\\", "/")
    except ValueError:
        return ""
    token = os.environ.get("LEASE_DROPBOX_TOKEN", "")
    refresh = os.environ.get("LEASE_DROPBOX_REFRESH_TOKEN", "")
    if not (token or refresh):
        return ""
    try:
        import dropbox
    except ImportError:
        print("WARN: `dropbox` package not installed — sheet omits leaseUrl")
        return ""
    try:
        if refresh:
            dbx = dropbox.Dropbox(
                oauth2_refresh_token=refresh,
                app_key=os.environ.get("LEASE_DROPBOX_APP_KEY", ""),
                app_secret=os.environ.get("LEASE_DROPBOX_APP_SECRET", ""))
        else:
            dbx = dropbox.Dropbox(token)
        from dropbox.sharing import (SharedLinkSettings, LinkAudience,
                                     MemberSelector, AccessLevel)
        # Restricted link: opens only for people with direct access to the
        # file — Jay (owner) plus the viewers granted below.
        settings = SharedLinkSettings(audience=LinkAudience.no_one)
        # Reuse-first: creating over an existing link makes the SDK choke
        # parsing the already-exists error when settings are attached.
        # The desktop client may still be uploading the just-moved PDF, so the
        # cloud path can lag the local one — retry briefly before giving up
        # (the watcher's backfill pass catches anything slower).
        url = None
        for attempt in range(3):
            try:
                links = dbx.sharing_list_shared_links(path=rel, direct_only=True).links
                url = links[0].url if links else \
                    dbx.sharing_create_shared_link_with_settings(rel, settings).url
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(20)
        if url and _LINK_VIEWERS:
            try:
                dbx.sharing_add_file_member(
                    rel, [MemberSelector.email(e) for e in _LINK_VIEWERS],
                    quiet=True, access_level=AccessLevel.viewer)
            except Exception as e:
                print(f"WARN: could not grant lease-link viewers ({e})")
        return url
    except Exception as e:
        print(f"WARN: Dropbox share link failed ({e}) — sheet omits leaseUrl")
        return ""


def backfill_lease_urls(sent_dir, max_age_days=3):
    """Heal recently filed jobs whose Dropbox share link failed at filing time
    (cloud sync lagged the local move — the 168 Lucille lease hit this). For
    each such job: create the link now and re-send the SAME sheet row values
    plus leaseUrl, preserving the original before-values undo trail."""
    import urllib.request
    healed = 0
    for jf in Path(sent_dir).glob("*.json"):
        try:
            rec = json.loads(jf.read_text(encoding="utf-8"))
        except Exception:
            continue
        filed = rec.get("filed") or {}
        if not filed.get("path") or filed.get("url"):
            continue
        try:
            age = datetime.now() - datetime.fromisoformat(filed.get("at", ""))
            if age.days > max_age_days:
                continue
        except ValueError:
            continue
        url = make_lease_url(filed["path"])
        if not url:
            continue    # still not synced (or no token) — next tick retries
        filed["url"] = url
        body = None
        upd = rec.get("sheet_update") or {}
        if isinstance(upd.get("requested"), dict) and upd.get("response", {}).get("success"):
            body = dict(upd["requested"], leaseUrl=url)
            try:
                req = urllib.request.Request(
                    f"{_PREMIO_APP}/.netlify/functions/edit-tenant",
                    data=json.dumps(body).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=45) as r:
                    if json.loads(r.read().decode("utf-8")).get("success"):
                        upd["requested"]["leaseUrl"] = url
                        upd["url_backfilled_at"] = datetime.now().isoformat(timespec="seconds")
            except Exception as e:
                print(f"WARN: leaseUrl backfill sheet post failed ({e})")
        jf.write_text(json.dumps(rec, indent=2), encoding="utf-8")
        healed += 1
        print(f"backfilled lease link: {jf.name}")
    return healed


# Approvals rail (shared with maintenance/lease-filing cards). Our decision
# id prefix is `aptpay-` — unclaimed by any other consumer; lease_watcher owns
# `lease-` and the filing cards own `leasefile-`, so never reuse those.
_APPROVALS = Path(_SHARED_ROOT) / "approvals"
APT_LEASE_QUEUE = Path(_SHARED_ROOT) / "apartments_lease_queue"

_ACTION_WORDS = {
    "cancel_old_payments": "cancel {old}'s future payments",
    "end_old_residency": "end {old}'s residency",
    "setup_new_payments": "set {new} up to pay online",
    "update_payment_amount": "update {new}'s payment amount to ${rent}",
}


def queue_turnover_card(job, job_path=None):
    """After a lease files + the sheet row updates, put an Apartments.com
    payments card on the approvals rail. NOTHING touches Apartments.com from
    this path — an approved card only queues a job for the payments agent."""
    from lease_turnover import classify_turnover
    plan = classify_turnover(job)
    if not plan:
        return None
    where = f"{plan['property']}" + (f" / {plan['unit']}" if plan["unit"] else "")
    if plan["kind"] == "renewal_no_change":
        push("Renewal — no payment changes",
             f"{where}: {plan['new_tenant']} re-signed at the same rent "
             f"(${plan['rent']}). Apartments.com left untouched.",
             {"job": job.get("property_key", "")})
        return None

    wants = [_ACTION_WORDS[a].format(old=plan.get("old_tenant") or "the old tenant",
                                     new=plan["new_tenant"], rent=plan["rent"])
             for a in plan["actions"]]
    kind_line = {"turnover": f"NEW tenant (was {plan.get('old_tenant')})",
                 "move_in": "move-in to a vacant unit",
                 "renewal_rent_change":
                     f"renewal, rent ${plan.get('old_rent')} -> ${plan['rent']}"}[plan["kind"]]
    cid = "aptpay-" + re.sub(r"[^a-z0-9]+", "-",
                             f"{plan['property']} {plan['unit']}".lower()).strip("-") \
          + f"-{int(time.time())}"
    card = {
        "id": cid, "agent": "lease", "kind": "apartments_payments",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "title": f"Apartments.com: {where} — {plan['new_tenant']}",
        "subject": f"Payments update: {where}",
        "from": "Lease pipeline",
        "body": (f"Lease signed at {where} — {kind_line}.\n"
                 f"Rent ${plan['rent']}, term {plan['lease_start']} to "
                 f"{plan['lease_end']}.\n\nOn approve I will (on Apartments.com):\n"
                 + "\n".join(f"  - {w}" for w in wants)
                 + "\n\nNothing happens until you approve."),
        "actions": ["approve", "skip"],
        "fields": plan,
    }
    (_APPROVALS / "pending").mkdir(parents=True, exist_ok=True)
    tmp = _APPROVALS / "pending" / f"{cid}.json.tmp"
    tmp.write_text(json.dumps(card, indent=2), encoding="utf-8")
    tmp.replace(_APPROVALS / "pending" / f"{cid}.json")
    if job_path:
        try:
            rec = json.loads(Path(job_path).read_text(encoding="utf-8"))
            rec["turnover_card"] = {"id": cid, "kind": plan["kind"],
                                    "at": card["created_at"]}
            Path(job_path).write_text(json.dumps(rec, indent=2), encoding="utf-8")
        except Exception:
            pass
    return cid


def update_sheet_tenant(job, job_path=None, lease_url=""):
    """Write the new tenant onto Jay's master sheet via the Premio app's
    edit-tenant function, using the EXACT row strings the wizard captured
    (job['sheet'] = {tab, property, unit}). Reads the row's current values
    first and records them in the job record, so every update is reversible.
    Never raises — a miss is pushed for Jay, the lease is already filed."""
    import urllib.request

    sheet = job.get("sheet") or {}
    if not (sheet.get("property") and sheet.get("unit")):
        return None   # client lease / old app build — nothing to update
    tenant_names = " & ".join(s["name"] for s in _signers(job) if s.get("name"))
    body = {
        "sheetType": "owned" if sheet.get("tab") != "premio" else "premio",
        "propertyAddress": sheet["property"],
        "unitName": sheet["unit"],
        "tenantName": tenant_names,
        "deposit": _money(job.get("deposit")) or 0,
        "rent": _money(job.get("rent")),
        "leaseStart": job.get("term_start_iso", ""),
        "leaseEnd": job.get("term_end_iso", ""),
    }
    if lease_url:
        body["leaseUrl"] = lease_url
    try:
        previous = _read_sheet_row(body["sheetType"], sheet["property"], sheet["unit"])
    except Exception as e:
        previous = {"unreadable": str(e)}
    try:
        req = urllib.request.Request(
            f"{_PREMIO_APP}/.netlify/functions/edit-tenant",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=45) as r:
            resp = json.loads(r.read().decode("utf-8"))
        ok = bool(resp.get("success"))
    except Exception as e:
        resp, ok = {"error": str(e)}, False

    rec_update = {"requested": body, "previous": previous, "response": resp,
                  "at": datetime.now().isoformat(timespec="seconds")}
    job["sheet_update"] = rec_update   # callers classify turnover off this
    if job_path:
        try:
            rec = json.loads(Path(job_path).read_text(encoding="utf-8"))
            rec["sheet_update"] = rec_update
            Path(job_path).write_text(json.dumps(rec, indent=2), encoding="utf-8")
        except Exception:
            pass

    if ok:
        push("Sheet updated",
             f"{sheet['property']} / {sheet['unit']}: tenant -> {tenant_names}, "
             f"rent {body['rent']}, deposit {body['deposit']}.",
             {"job": job.get("property_key", "")})
    else:
        push("Sheet update FAILED",
             f"{sheet['property']} / {sheet['unit']} ({tenant_names}): "
             f"{resp.get('error') or resp}. Update the row by hand.",
             {"job": job.get("property_key", "")})
    return ok


def file_signed_lease(signed_pdf, job, job_path=None, retire=True):
    """File one signed lease. Returns the destination Path, or the _unfiled Path
    if it couldn't be filed. Never raises for a routing miss — it fails loud via
    _unfiled + push, per the spec.

    retire=False: place the PDF without retiring same-prefix leases — for
    card-filed documents (the 'water 168 lucille' card evicted the Mattesons'
    real lease to Past Tenants on 8/18; only pipeline-sent leases may retire)."""
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

    # Destination: a resolvable unit subfolder -> that subfolder (any tree —
    # some managed properties are structured too, e.g. 227 Whitewood's
    # Bar/Laundromat/Moon Mart); otherwise the property folder (unit rides
    # in the filename, flat-Premio-style).
    dest_folder = prop_folder
    if unit:
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
                    if retire and f.startswith(prefix) and f.lower().endswith(".pdf") and f != fname]
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
    lease_url = make_lease_url(dest)
    filed = {"path": str(dest), "filename": dest.name, "url": lease_url or None,
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

    # Turnover: write the new tenant onto the master sheet (its own push;
    # failure never un-files the lease).
    update_sheet_tenant(job, job_path=job_path, lease_url=lease_url)
    try:
        queue_turnover_card(job, job_path=job_path)
    except Exception as e:
        print(f"WARN: turnover card failed ({e}) — lease is filed regardless")
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
