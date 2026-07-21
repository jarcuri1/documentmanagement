"""
LeaseWatcher — approval-decision consumer for the lease pipeline
================================================================
Piece 1 of 3 (see HANDOFF_LEASE_AGENT.md). Runs on the fleet PC as a
long-lived loop. It consumes decisions off the Supervisor's approvals rails
(shared\\approvals\\decisions) and turns an approved lease into a real send
by handing the job to lease_sender.py.

This matches the REAL supervisor contract (ANSWERS_LEASEAGENT.md):

  * Decisions are addressed by ID PREFIX. The lease agent owns `lease-*.json`
    and touches nothing else in that folder (fb-*, teach-*, ... are other
    agents' business).
  * A decision file is {id, action, text, decided_at}. There is no `job`
    field: the decision id IS the card id, and the card id encodes the job as
    `lease-<slug>`. Strip the prefix to get the job slug -> Pending\\<slug>.json.
  * DELETE-ON-CONSUME (fleet convention, replaces a local ledger): on `send`,
    claim the job (Pending -> Sending) then delete the decision. Claim-first
    already makes a double-send impossible; if we crash between claim and
    delete, the next run sees a decision whose job is no longer in Pending ->
    treat as already handled, delete, move on.
  * Status pushes go to shared\\push_outbox\\ (the jsonl notifier is dead).
    Only meaningful events push: a send that needs attention, a stale job.

RUN (fleet PC, after `python lease_sender.py --setup` is done):
  python lease_watcher.py            # watch forever
  python lease_watcher.py --once     # handle whatever's pending now, exit
  python lease_watcher.py --dry-run  # parse + match + REPORT; no moves, no
                                     # sends, no deletes, no pushes

All paths default to the documented layout and are env-overridable
(LEASE_DECISIONS_DIR, LEASE_PENDING_DIR, ...) so it can be exercised off the
fleet PC against temp folders.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ----------------------------------------------------------------------
# CONFIG — env-overridable; defaults match the fleet layout
# ----------------------------------------------------------------------
_SHARED_ROOT = os.environ.get("LEASE_SHARED_ROOT", r"C:\AIAgents\shared")
_LEASES_ROOT = os.environ.get("LEASE_DROPBOX_ROOT", r"D:\Dropbox\Dropbox\Leases")

LEASE_PREFIX = "lease-"   # our slice of the shared decisions folder


def _path(env_key, *default_parts, root):
    override = os.environ.get(env_key)
    return Path(override) if override else Path(root, *default_parts)


CONFIG = {
    "decisions_dir": _path("LEASE_DECISIONS_DIR", "approvals", "decisions", root=_SHARED_ROOT),
    "push_outbox_dir": _path("LEASE_PUSH_OUTBOX", "push_outbox", root=_SHARED_ROOT),
    "pending_dir":  _path("LEASE_PENDING_DIR",  "Pending",  root=_LEASES_ROOT),
    "sending_dir":  _path("LEASE_SENDING_DIR",  "Sending",  root=_LEASES_ROOT),
    "rejected_dir": _path("LEASE_REJECTED_DIR", "Rejected", root=_LEASES_ROOT),
    "sender_script": Path(os.environ.get(
        "LEASE_SENDER_SCRIPT", str(Path(__file__).with_name("lease_sender.py")))),
    "python_exe": os.environ.get("LEASE_PYTHON", sys.executable),
    "poll_seconds":   int(os.environ.get("LEASE_POLL_SECONDS", "10")),
    "settle_seconds": int(os.environ.get("LEASE_SETTLE_SECONDS", "3")),
    "stale_minutes":  int(os.environ.get("LEASE_STALE_MINUTES", "30")),
}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_REQUIRED_JOB_FIELDS = ["property", "pdf_path", "signing_name"]


def _signers_of(job: dict) -> list:
    if job.get("signers"):
        return job["signers"]
    if job.get("tenant_name") and job.get("tenant_email"):
        return [{"name": job["tenant_name"], "email": job["tenant_email"]}]
    return []


# ======================================================================
# FLEET CONTRACT accessors — the decision-file shape, verbatim from the
# running supervisor.js. Edit here (and only here) if the supervisor changes.
#   decision file: { "id", "action", "text", "decided_at" }
#   addressing:    filename == id, our slice is the `lease-` prefix
#   job mapping:   slug = id without the `lease-` prefix
# ======================================================================
def decision_is_ours(decision_id: str) -> bool:
    return decision_id.startswith(LEASE_PREFIX)


def decision_action(d: dict) -> str:
    return str(d.get("action") or "").strip().lower()


def job_slug_from_id(decision_id: str) -> str:
    return decision_id[len(LEASE_PREFIX):] if decision_is_ours(decision_id) else decision_id


def decision_feedback(d: dict) -> str:
    return str(d.get("text") or "").strip()
# ======================================================================
# END FLEET CONTRACT
# ======================================================================


_push_seq = 0


def push(title: str, body: str, data: dict = None):
    """Drop a status push onto the Supervisor's push_outbox rail (swept every
    15s, pushed to Jay's phone, file deleted). Atomic write so the sweep never
    reads a half-written file."""
    global _push_seq
    _push_seq += 1
    outbox = CONFIG["push_outbox_dir"]
    outbox.mkdir(parents=True, exist_ok=True)
    payload = {"title": title, "body": body, "data": {**(data or {}), "kind": "lease"}}
    name = f"lease-{os.getpid()}-{int(time.time() * 1000)}-{_push_seq}.json"
    tmp = outbox / (name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(outbox / name)
    print(f"[{datetime.now().isoformat(timespec='seconds')}] PUSH {title}: {body}")


def log(msg: str):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}")


def validate_job(job_path: Path):
    """Mirror of lease_sender.load_job — the watcher is the gatekeeper, so a
    malformed job is rejected before it can be claimed into Sending.
    Returns (job_dict, "") on success or (None, reason) on failure."""
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except Exception as e:
        return None, f"unreadable / invalid JSON: {e}"
    if not isinstance(job, dict):
        return None, "job JSON is not an object"
    missing = [k for k in _REQUIRED_JOB_FIELDS if not job.get(k)]
    if missing:
        return None, f"missing fields: {missing}"
    signers = _signers_of(job)
    if not signers:
        return None, "job has no signers (need signers[] or tenant_name/tenant_email)"
    for i, s in enumerate(signers, 1):
        if not s.get("name") or not s.get("email"):
            return None, f"signer {i} missing name/email: {s!r}"
        if not _EMAIL_RE.match(str(s["email"])):
            return None, f"signer {i} email looks malformed: {s['email']!r}"
    if not Path(job["pdf_path"]).exists():
        return None, f"lease PDF not found: {job['pdf_path']}"
    return job, ""


class LeaseWatcher:
    def __init__(self, once: bool = False, dry_run: bool = False):
        self.once = once
        self.dry_run = dry_run
        self._stop = False
        self._stale_warned = set()

    # ---- lifecycle ---------------------------------------------------
    def run(self):
        for d in ("sending_dir", "rejected_dir", "push_outbox_dir"):
            CONFIG[d].mkdir(parents=True, exist_ok=True)
        signal.signal(signal.SIGINT, self._request_stop)
        try:
            signal.signal(signal.SIGTERM, self._request_stop)
        except (ValueError, AttributeError):
            pass
        mode = "DRY-RUN " if self.dry_run else ""
        log(f"{mode}LeaseWatcher up. Decisions: {CONFIG['decisions_dir']} (lease-*.json)")
        while not self._stop:
            try:
                self.check_stale_sending()
                self.scan_decisions()
            except Exception as e:
                log(f"tick error (continuing): {e}")
            if self.once:
                break
            self._sleep(CONFIG["poll_seconds"])
        log(f"{mode}LeaseWatcher stopped.")

    def _request_stop(self, *_):
        self._stop = True

    def _sleep(self, seconds):
        for _ in range(seconds * 2):
            if self._stop:
                return
            time.sleep(0.5)

    # ---- decisions ---------------------------------------------------
    def scan_decisions(self):
        d_dir = CONFIG["decisions_dir"]
        if not d_dir.exists():
            return
        now = time.time()
        # Only our slice of the shared folder. Never touch other prefixes.
        for path in sorted(d_dir.glob(f"{LEASE_PREFIX}*.json"), key=lambda p: p.stat().st_mtime):
            try:
                if now - path.stat().st_mtime < CONFIG["settle_seconds"]:
                    continue
            except FileNotFoundError:
                continue
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue  # partial write; retry next tick
            if not isinstance(d, dict):
                continue
            decision_id = str(d.get("id") or path.stem)
            self.handle_decision(path, d, decision_id)

    def handle_decision(self, path, d, decision_id):
        action = decision_action(d)
        slug = job_slug_from_id(decision_id)
        if action == "send":
            self.handle_send(path, slug)
        elif action == "reject":
            self.handle_reject(path, slug)
        else:
            # No feedback button ships until the fill agent honors it, so any
            # other verb is unexpected. Surface it and clear it.
            if self.dry_run:
                log(f"[DRY-RUN] {decision_id}: unexpected action {action!r}")
                return
            push("Lease agent", f"Unexpected decision {action!r} for {slug} — ignored.",
                 {"job": slug})
            self._delete_decision(path)

    # ---- send --------------------------------------------------------
    def handle_send(self, path, slug):
        job_json = CONFIG["pending_dir"] / f"{slug}.json"
        if not job_json.exists():
            # Already handled (claim-first + delete-on-consume, crash-safe) or
            # the job never landed. Either way this decision is spent.
            if self.dry_run:
                log(f"[DRY-RUN] send {slug}: no job in Pending (already handled?)")
                return
            log(f"send {slug}: no job in Pending — treating as already handled, "
                f"clearing decision.")
            self._delete_decision(path)
            return

        job, reason = validate_job(job_json)
        if job is None:
            if self.dry_run:
                log(f"[DRY-RUN] send {slug}: job INVALID ({reason})")
                return
            push("Lease not sent", f"{slug}: approved job is invalid ({reason}).",
                 {"job": slug})
            self._delete_decision(path)
            return

        if self.dry_run:
            emails = ", ".join(s["email"] for s in _signers_of(job))
            log(f"[DRY-RUN] would claim + send {slug} -> {emails} ({job['property']})")
            return

        claimed = self.claim(job_json)
        if claimed is None:
            self._delete_decision(path)   # stuck prior run; flagged in claim()
            return
        # Delete the decision the instant the claim succeeds (fleet convention).
        # From here a crash leaves the job in Sending with no decision, so it is
        # never re-sent; the stale sweep will flag it.
        self._delete_decision(path)
        log(f"claimed {slug}; sending lease for {job['property']}.")
        rc = self.run_sender(claimed)
        self.reconcile(slug, claimed, rc)

    def claim(self, job_json):
        dest = CONFIG["sending_dir"] / job_json.name
        if dest.exists():
            push("Lease stuck", f"cannot claim {job_json.name}: already in Sending "
                 f"(a prior run is stuck). Resolve it first.", {"job": job_json.stem})
            return None
        try:
            shutil.move(str(job_json), str(dest))
            return dest
        except Exception as e:
            push("Lease error", f"claim failed for {job_json.name}: {e}",
                 {"job": job_json.stem})
            return None

    def run_sender(self, sending_path):
        cmd = [CONFIG["python_exe"], str(CONFIG["sender_script"]), "--job", str(sending_path)]
        # Hard wall-clock cap: Sign's page can wedge its own main thread, and a
        # blocked page.evaluate has NO Playwright timeout — without this cap one
        # hung sender freezes the whole lease lane (every later tick skips on
        # the overlap guard). Kill the whole tree (sender + its Chrome).
        cap_s = int(os.environ.get("LEASE_SENDER_TIMEOUT_S", "1500"))
        try:
            proc = subprocess.Popen(cmd)
        except FileNotFoundError as e:
            push("Lease error", f"could not launch lease_sender ({e}).",
                 {"job": sending_path.stem})
            return 1
        try:
            return proc.wait(timeout=cap_s)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True)
            push("Lease stuck", f"{sending_path.stem}: sender hit the "
                 f"{cap_s // 60}-minute cap and was killed. Job left in Sending. "
                 f"Check Audit + Authentisign before re-queuing — do NOT assume "
                 f"nothing was sent.", {"job": sending_path.stem})
            return 124

    def reconcile(self, slug, claimed, rc):
        """lease_sender owns all post-launch placement; the watcher never moves
        the job here. We only push anything a human must look at. (A clean send
        or a clean pre-send abort already pushed from lease_sender.)"""
        still = claimed.exists()
        if rc == 0 and still:
            push("Lease needs review", f"{slug}: sender exited 0 but the job is "
                 f"still in Sending — inconsistent, check it.", {"job": slug})
        elif rc not in (0, 1) or (rc != 0 and still):
            push("Lease needs review", f"{slug}: sender exited {rc} with the job left "
                 f"in Sending — a send may have gone out. Verify in Authentisign "
                 f"before any resend; do NOT blindly retry.", {"job": slug})

    # ---- reject ------------------------------------------------------
    def handle_reject(self, path, slug):
        job_json = CONFIG["pending_dir"] / f"{slug}.json"
        if self.dry_run:
            log(f"[DRY-RUN] would reject {slug} -> Rejected")
            return
        CONFIG["rejected_dir"].mkdir(parents=True, exist_ok=True)
        if job_json.exists():
            try:
                job = json.loads(job_json.read_text(encoding="utf-8"))
                pdf = Path(job.get("pdf_path", ""))
                if pdf.exists():
                    self._safe_move(pdf, CONFIG["rejected_dir"] / pdf.name)
            except Exception as e:
                log(f"reject {slug}: could not move pdf ({e}); moving json only.")
            self._safe_move(job_json, CONFIG["rejected_dir"] / job_json.name)
            log(f"{slug}: rejected in the app -> moved to Rejected.")
        else:
            log(f"reject {slug}: no job in Pending (already handled?) — clearing decision.")
        self._delete_decision(path)

    # ---- helpers -----------------------------------------------------
    def _delete_decision(self, path):
        try:
            path.unlink(missing_ok=True)
        except Exception as e:
            log(f"could not delete decision {path.name}: {e}")

    @staticmethod
    def _safe_move(src, dest):
        if dest.exists():
            dest = dest.with_name(f"{src.stem}-{int(time.time())}{src.suffix}")
        shutil.move(str(src), str(dest))

    def check_stale_sending(self):
        s_dir = CONFIG["sending_dir"]
        if not s_dir.exists():
            return
        cutoff = CONFIG["stale_minutes"] * 60
        now = time.time()
        present = set()
        for job_json in s_dir.glob("*.json"):
            present.add(job_json.name)
            try:
                age = now - job_json.stat().st_mtime
            except FileNotFoundError:
                continue
            if age >= cutoff and job_json.name not in self._stale_warned:
                if not self.dry_run:
                    push("Lease stuck", f"{job_json.stem} has sat in Sending for "
                         f"{int(age // 60)}m — a send likely died mid-run. Not retried; "
                         f"check Audit + Authentisign.", {"job": job_json.stem})
                self._stale_warned.add(job_json.name)
        self._stale_warned &= present


if __name__ == "__main__":
    LeaseWatcher(
        once="--once" in sys.argv,
        dry_run="--dry-run" in sys.argv,
    ).run()
