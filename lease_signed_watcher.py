"""
LeaseSignedWatcher — retrieve executed leases from email and file them
======================================================================
Part 3 (final piece) of the signed-lease filing feature. Watches the Gmail
inbox for SmartMLS Sign completion emails, pulls the executed lease PDF out
of the attachments, correlates it back to the job that sent it, and hands it
to lease_filer.file_signed_lease (which files it into the right Dropbox
folder and retires the old one).

HOW IT PLUGS IN
  * Reuses the Email Agent's Gmail OAuth token (token_premio.json +
    credentials.json in the EmailAgent dir) — no second login, and this
    script never modifies the Email Agent.
  * Dedupes by Gmail message id in its own state file, and searches ALL mail
    (not just unread/inbox), so it works regardless of what the Email Agent
    does to the message (archive to a label, mark read, etc.). No races.
  * Runs on a schedule (add to the supervisor fleet, or cron) like the other
    agents.

THE EMAIL (from the screenshots)
  From:    SmartMLS Sign
  Subject: eSigning Completed | <signing name>
  Body:    "...fully executed documents are attached..."
  Attach:  the executed lease PDF + CT disclosures + a signing Certificate.

CORRELATION (which job is this?)
  Our sender names every signing "Lease - <property> - <surname>", and Smart
  Sign echoes that into the subject after "| ". So the primary match is:
  subject name == a Sent job's signing_name. Fallback: read the property and
  tenant surnames out of the executed PDF and match a Sent job that way. If
  nothing matches, the PDF goes to Leases\\Signed\\_unfiled\\ with a push —
  never guessed into a folder.

RUN (fleet PC, after the Email Agent has authenticated once):
  python lease_signed_watcher.py           # process new completions, exit
  python lease_signed_watcher.py --loop    # poll forever
  python lease_signed_watcher.py --dry-run # match + report; download nothing,
                                           # file nothing, mark nothing
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

_SHARED_ROOT = os.environ.get("LEASE_SHARED_ROOT", r"C:\AIAgents\shared")
_LEASES_ROOT = os.environ.get("LEASE_DROPBOX_ROOT", r"D:\Dropbox\Dropbox\Leases")

CONFIG = {
    # Where the Email Agent keeps its Google OAuth files (reused read-only).
    "email_agent_dir": Path(os.environ.get("LEASE_EMAIL_AGENT_DIR", r"C:\AIAgents\EmailAgent")),
    "token_file": os.environ.get("LEASE_GMAIL_TOKEN", "token_premio.json"),
    # All inboxes to search. Completion mail goes to every participant, and the
    # only address on EVERY signing is realtorarcuri (listing-agent rule) — the
    # premio inbox alone misses Jay-as-landlord jobs (found 2026-07-23).
    "token_files": [t.strip() for t in os.environ.get(
        "LEASE_GMAIL_TOKENS", "token_premio.json,token_realtor.json").split(",") if t.strip()],
    "gmail_query": os.environ.get("LEASE_GMAIL_QUERY", 'subject:"eSigning Completed" newer_than:30d'),
    "sent_dir": Path(os.environ.get("LEASE_SENT_DIR", str(Path(_LEASES_ROOT) / "Sent"))),
    "unfiled_dir": Path(os.environ.get("LEASE_UNFILED_DIR", str(Path(_LEASES_ROOT) / "Signed" / "_unfiled"))),
    "work_dir": Path(os.environ.get("LEASE_SIGNED_WORK", str(Path(_LEASES_ROOT) / "Signed" / "_incoming"))),
    "state_file": Path(os.environ.get("LEASE_SIGNED_STATE",
                       str(Path(r"C:\AIAgents\LeaseAgent\state") / "signed_processed.json"))),
    "push_outbox_dir": Path(os.environ.get("LEASE_PUSH_OUTBOX", str(Path(_SHARED_ROOT) / "push_outbox"))),
    "poll_seconds": int(os.environ.get("LEASE_SIGNED_POLL", "300")),
}

_GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
# Attachment names that are NOT the lease we file (disclosures + certificate).
_NOT_LEASE = ("certificate", "disclosure", "protect", "standardized", "lead", "addendum", "cover")


# ----------------------------------------------------------------------
# Pure, testable core
# ----------------------------------------------------------------------
def _norm(s):
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def signing_name_from_subject(subject):
    """'eSigning Completed | Lease - 61 Cliff... - Smith' -> the part after '|'."""
    if "|" in subject:
        return subject.split("|", 1)[1].strip()
    return re.sub(r"(?i)^\s*esigning completed\s*[-:]?\s*", "", subject).strip()


def pick_executed_lease(attachments):
    """Choose the executed lease PDF from the attachment set. Prefers a PDF
    whose name contains 'lease' and not a disclosure/certificate word; breaks
    ties by size (the full executed doc is the largest). Returns the chosen
    attachment dict or None if none can be confidently identified."""
    pdfs = [a for a in attachments if str(a.get("filename", "")).lower().endswith(".pdf")]
    if not pdfs:
        return None

    def score(a):
        n = a["filename"].lower()
        s = 0.0
        if "lease" in n:
            s += 100
        if any(x in n for x in _NOT_LEASE):
            s -= 200
        s += (a.get("size", 0) or 0) / 1e6
        return s

    best = max(pdfs, key=score)
    n = best["filename"].lower()
    if "lease" not in n and any(x in n for x in _NOT_LEASE):
        return None   # only disclosures/cert present — can't identify the lease
    return best


def correlate_job(signing_name, sent_dir, pdf_text=""):
    """Find the Sent job this completion belongs to. Primary: subject name ==
    job signing_name. Fallback: every signer surname + the property's leading
    street number both appear in the executed PDF text. Returns (job, path) or
    (None, None)."""
    target = _norm(signing_name)
    jobs = []
    for jf in Path(sent_dir).glob("*.json"):
        try:
            jobs.append((json.loads(jf.read_text(encoding="utf-8")), jf))
        except Exception:
            continue

    for job, jf in jobs:
        if target and _norm(job.get("signing_name", "")) == target:
            return job, jf

    if pdf_text:
        text = pdf_text.lower()
        for job, jf in jobs:
            signers = job.get("signers") or ([{"name": job.get("tenant_name", "")}]
                                             if job.get("tenant_name") else [])
            surnames = [str(s.get("name", "")).split()[-1].lower() for s in signers if s.get("name")]
            num = re.match(r"\s*(\d+)", str(job.get("property", "")))
            if surnames and all(sn in text for sn in surnames) and (not num or num.group(1) in text):
                return job, jf
    return None, None


# ----------------------------------------------------------------------
# Notifications / state
# ----------------------------------------------------------------------
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


def _load_processed():
    try:
        return set(json.loads(CONFIG["state_file"].read_text(encoding="utf-8")).get("ids", []))
    except Exception:
        return set()


def _save_processed(ids):
    CONFIG["state_file"].parent.mkdir(parents=True, exist_ok=True)
    CONFIG["state_file"].write_text(json.dumps({"ids": sorted(ids)}), encoding="utf-8")


# ----------------------------------------------------------------------
# Gmail (lazy imports so the pure core is testable without google libs)
# ----------------------------------------------------------------------
def _gmail_service(token_name=None):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    token = CONFIG["email_agent_dir"] / (token_name or CONFIG["token_file"])
    if not token.exists():
        raise RuntimeError(f"Gmail token not found: {token} (has the Email Agent authenticated?)")
    creds = Credentials.from_authorized_user_file(str(token), _GMAIL_SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _iter_attachments(payload):
    fn = payload.get("filename")
    body = payload.get("body", {}) or {}
    if fn and body.get("attachmentId"):
        yield {"filename": fn, "size": body.get("size", 0), "attachmentId": body["attachmentId"]}
    for part in payload.get("parts", []) or []:
        yield from _iter_attachments(part)


def _download_attachment(service, msg_id, att):
    import base64
    a = service.users().messages().attachments().get(
        userId="me", messageId=msg_id, id=att["attachmentId"]).execute()
    return base64.urlsafe_b64decode(a["data"])


def _pdf_text(pdf_path):
    try:
        import fitz
        doc = fitz.open(str(pdf_path))
        return "\n".join(pg.get_text() for pg in doc[:3])
    except Exception:
        return ""


# ----------------------------------------------------------------------
# Manual filing cards — when a completed signing matches no sent job, ask
# Jay where it goes (Owned / Managed / Don't file -> property -> unit).
# Card ids use the 'leasefile-' prefix: lease_watcher owns 'lease-' and
# would otherwise eat our decisions.
# ----------------------------------------------------------------------
_FOLDERS_JSON = Path(_SHARED_ROOT) / "lease_folders.json"
_CARDS_DIR = Path(_SHARED_ROOT) / "approvals" / "pending"
_DECISIONS_DIR = Path(_SHARED_ROOT) / "approvals" / "decisions"


def _folder_choices():
    """lease_folders.json -> {'personal': [{key,label,units[]}], 'premio': [...]}"""
    try:
        mapping = json.loads(_FOLDERS_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {"personal": [], "premio": []}
    out = {"personal": [], "premio": []}
    for key, e in sorted(mapping.items()):
        tree = "premio" if e.get("tree") == "premio" else "personal"
        label = Path(e.get("folder", key)).name
        units = sorted(set((e.get("units") or {}).values()))
        out[tree].append({"key": key, "label": label, "units": units})
    return out


def _guess_property(choices, signing_name, pdf_text):
    """Cheap deduction: which property label's tokens appear in the signing
    name or the lease text? First match wins; None when nothing does."""
    hay = re.sub(r"[^a-z0-9]+", " ", f"{signing_name} {pdf_text}".lower())
    for tree in ("personal", "premio"):
        for p in choices[tree]:
            toks = re.sub(r"[^a-z0-9]+", " ", p["label"].lower()).split()
            # require the street number + first street word (e.g. "128 walnut")
            if len(toks) >= 2 and f"{toks[0]} {toks[1]}" in hay:
                return {"tree": tree, "property_key": p["key"]}
    return None


def _queue_filing_card(pdf_name, signing_name, pdf_text):
    choices = _folder_choices()
    cid = "leasefile-" + re.sub(r"[^a-z0-9]+", "-", signing_name.lower()).strip("-")[:40] \
          + f"-{int(time.time())}"
    card = {
        "id": cid,
        "agent": "lease",
        "kind": "lease_file",
        "created_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "title": f"Where does this lease go? — {signing_name}",
        "subject": f"File signed lease: {signing_name}",
        "body": (f"'{signing_name}' completed on SmartMLS Sign but doesn't match "
                 f"any lease I sent, so I can't file it on my own. Tell me where "
                 f"it belongs (or Don't file to leave it alone)."),
        "actions": ["file", "skip"],
        "fields": {
            "pdf": pdf_name,
            "signing_name": signing_name,
            "guess": _guess_property(choices, signing_name, pdf_text),
            "choices": choices,
        },
    }
    _CARDS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _CARDS_DIR / (cid + ".json.tmp")
    tmp.write_text(json.dumps(card, indent=2), encoding="utf-8")
    tmp.replace(_CARDS_DIR / (cid + ".json"))


def consume_filing_decisions():
    """Act on decided filing cards (runs every pipeline tick, no Gmail/browser
    needed). Decision text is JSON from the app: {pdf, property_key, unit,
    label}."""
    from lease_filer import file_signed_lease
    handled = 0
    if not _DECISIONS_DIR.exists():
        return 0
    for f in sorted(_DECISIONS_DIR.glob("leasefile-*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            info = json.loads(d.get("text") or "{}")
        except Exception as e:
            print(f"bad filing decision {f.name}: {e}", file=sys.stderr)
            f.rename(f.with_suffix(".json.bad"))
            continue
        pdf = CONFIG["unfiled_dir"] / (info.get("pdf") or "")
        action = d.get("action")
        if action == "file" and info.get("property_key") and pdf.exists():
            job = {
                "property_key": info["property_key"],
                "unit": info.get("unit", ""),
                "filing_address": info.get("label") or info["property_key"],
                "tenant_name": info.get("signing_name") or "Manual",
                "term_start_iso": datetime.now().strftime("%Y-%m-%d"),
            }
            file_signed_lease(pdf, job)   # pushes its own "Lease filed"
        elif action == "file":
            push("Filing failed",
                 f"Couldn't file '{info.get('pdf')}' — file missing or no "
                 f"property picked. It's still in _unfiled.", {})
        else:   # skip / Don't file
            skipped = CONFIG["unfiled_dir"] / "skipped"
            if pdf.exists():
                skipped.mkdir(parents=True, exist_ok=True)
                target = skipped / pdf.name
                if target.exists():
                    target = target.with_name(f"{target.stem}-{int(time.time())}{target.suffix}")
                pdf.rename(target)
        f.unlink()
        handled += 1
    return handled


def process_once(dry_run=False):
    from lease_filer import file_signed_lease
    processed = _load_processed()
    handled = 0
    try:
        if not dry_run:
            handled += consume_filing_decisions()
    except Exception as e:
        print(f"filing decisions error (continuing): {e}", file=sys.stderr)
    for token_name in CONFIG["token_files"]:
        try:
            service = _gmail_service(token_name)
        except Exception as e:
            print(f"[{token_name}] skipped: {e}", file=sys.stderr)
            continue
        acct = Path(token_name).stem
        listed = service.users().messages().list(userId="me", q=CONFIG["gmail_query"], maxResults=25).execute()
        msgs = listed.get("messages", [])
        handled += _process_account(service, acct, msgs, processed, dry_run, file_signed_lease)
    _save_processed(processed)
    return handled


def _process_account(service, acct, msgs, processed, dry_run, file_signed_lease):
    handled = 0
    for m in msgs:
        mid = m["id"]
        # Legacy state entries are bare ids (premio-only era); new ones are
        # account-prefixed since ids are per-mailbox.
        key = f"{acct}:{mid}"
        if mid in processed or key in processed:
            continue
        full = service.users().messages().get(userId="me", id=mid, format="full").execute()
        headers = {h["name"].lower(): h["value"] for h in full["payload"].get("headers", [])}
        subject = headers.get("subject", "")
        sender = headers.get("from", "")
        # sanity: must look like a SmartMLS Sign completion
        if "esigning completed" not in subject.lower():
            continue
        name = signing_name_from_subject(subject)
        atts = list(_iter_attachments(full["payload"]))
        lease_att = pick_executed_lease(atts)

        if dry_run:
            job, _ = correlate_job(name, CONFIG["sent_dir"])
            print(f"[DRY-RUN] {subject!r} -> name={name!r} "
                  f"attach={lease_att['filename'] if lease_att else None} "
                  f"job={'MATCH' if job else 'no-match(name); PDF fallback at run time'}")
            continue

        if not lease_att:
            push("Signed lease needs filing",
                 f"'{name}' completed but no lease PDF found among attachments — file by hand.",
                 {"name": name})
            processed.add(key)
            continue

        CONFIG["work_dir"].mkdir(parents=True, exist_ok=True)
        tmp = CONFIG["work_dir"] / f"{mid}-{re.sub(r'[^A-Za-z0-9._-]+', '_', lease_att['filename'])}"
        tmp.write_bytes(_download_attachment(service, mid, lease_att))

        job, jf = correlate_job(name, CONFIG["sent_dir"], pdf_text=_pdf_text(tmp))
        if not job:
            pdf_text = _pdf_text(tmp)
            CONFIG["unfiled_dir"].mkdir(parents=True, exist_ok=True)
            dest = CONFIG["unfiled_dir"] / lease_att["filename"]
            if dest.exists():
                dest = dest.with_name(f"{dest.stem}-{int(time.time())}{dest.suffix}")
            tmp.replace(dest)
            _queue_filing_card(dest.name, name, pdf_text)
            push("Signed lease needs filing",
                 f"'{name}' doesn't match anything I sent — pick where it goes "
                 f"on the filing card in the app.", {"name": name})
        elif job.get("filed"):
            # Sign emails one completion per role instance (and to every inbox
            # we watch) — the job is already filed, so this is a duplicate copy.
            tmp.unlink(missing_ok=True)
        else:
            file_signed_lease(tmp, job, job_path=str(jf))   # files it + retires old + pushes

        processed.add(key)
        handled += 1

    _save_processed(processed)
    return handled


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.loop:
        while True:
            try:
                process_once(dry_run=args.dry_run)
            except Exception as e:
                print(f"[{datetime.now().isoformat(timespec='seconds')}] error (continuing): {e}",
                      file=sys.stderr)
            time.sleep(CONFIG["poll_seconds"])
    else:
        try:
            process_once(dry_run=args.dry_run)
        except Exception as e:
            # Transient DNS blips on gmail.googleapis.com were failing the whole
            # pipeline tick (and paging Jay). One short retry absorbs those;
            # anything persistent still fails loudly.
            if "unable to find the server" in str(e).lower() or "getaddrinfo" in str(e).lower():
                print(f"[transient] {e} — retrying once in 8s", file=sys.stderr)
                time.sleep(8)
                process_once(dry_run=args.dry_run)
            else:
                raise
