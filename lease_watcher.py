"""
LeaseWatcher — approval-decision consumer for the lease pipeline
================================================================
Piece 1 of 3 (see HANDOFF_LEASE_AGENT.md). Runs on the fleet PC as a
long-lived loop. It consumes decisions off the EXISTING approvals rails
(shared\\approvals\\decisions) -- the same pipeline every other fleet agent
uses -- and turns an approved lease into a real send by handing the job to
lease_sender.py.

It is deliberately dumb and fail-closed:
  * It only ever acts on decisions addressed to the lease agent. Other
    agents' decisions are never read for meaning and never touched.
  * On `send` it CLAIMS the job (Pending -> Sending) BEFORE launching the
    browser. A job in Sending is never picked again, so a crash or a
    Dropbox lock can never re-send a lease to a tenant. (Anti-double-send.)
  * On `reject` it moves the job + pdf to Rejected and pings.
  * `feedback` is acknowledged but not acted on in v1 (belongs to the fill
    agent, piece 3).
  * It never guesses. A decision it cannot map to exactly one valid job is
    NOT executed -- it's flagged for you and left for manual resolution.

=====================================================================
FLEET CONTRACT  --  the ONE place the approvals-rails schema lives
=====================================================================
The decision-file shape encoded in the accessor functions below MIRRORS
what the supervisor / Samantha app already writes for other agents. It was
built from the handoff, not from a real file. This block is the SELECTORS
dict of this script: if a real decision JSON uses different keys, fix the
four accessors here and NOTHING ELSE changes.

Assumed decision file (one JSON object per file in decisions_dir):
  {
    "kind": "lease",                 # or "agent": "lease"
    "action": "send",                # send | reject | feedback
    "job": "123-main-smith",         # maps to Pending\\123-main-smith.json
    "feedback": "..."                # only for action == feedback
  }
The job id maps to the job file by stem: <job>.json / <job>.pdf in Pending.
The `send`/`reject` decision carries NO lease data -- the job JSON in
Dropbox is the single source of truth (the character-for-character
recipient check in lease_sender reads the job file, never the decision).

RUN (fleet PC, after `python lease_sender.py --setup` is done):
  python lease_watcher.py            # watch forever
  python lease_watcher.py --once     # handle whatever's pending now, exit
  python lease_watcher.py --dry-run  # parse + match + REPORT only; no
                                     # moves, no claims, no sends, no ledger

All paths default to the documented Windows layout and can be overridden
with env vars (LEASE_DECISIONS_DIR, LEASE_PENDING_DIR, ...) so the whole
thing can be exercised off the fleet PC against temp folders.
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
# CONFIG — env-overridable; defaults match HANDOFF_LEASE_AGENT.md
# ----------------------------------------------------------------------
_SHARED_ROOT = os.environ.get("LEASE_SHARED_ROOT", r"C:\AIAgents\shared")
_LEASES_ROOT = os.environ.get("LEASE_DROPBOX_ROOT", r"C:\Users\Jay\Dropbox\Leases")


def _path(env_key: str, *default_parts, root: str) -> Path:
    override = os.environ.get(env_key)
    return Path(override) if override else Path(root, *default_parts)


CONFIG = {
    # Approvals rails (shared with the rest of the fleet — we only READ here)
    "decisions_dir": _path("LEASE_DECISIONS_DIR", "approvals", "decisions", root=_SHARED_ROOT),
    # Dropbox lease pipeline
    "pending_dir":  _path("LEASE_PENDING_DIR",  "Pending",  root=_LEASES_ROOT),
    "sending_dir":  _path("LEASE_SENDING_DIR",  "Sending",  root=_LEASES_ROOT),
    "rejected_dir": _path("LEASE_REJECTED_DIR", "Rejected", root=_LEASES_ROOT),
    # Agent-owned state (idempotency ledger — never a shared file)
    "state_dir":    _path("LEASE_STATE_DIR", root=r"C:\AIAgents\LeaseAgent\state"),
    "notify_file":  _path("LEASE_NOTIFY_FILE", "notifications", "lease_agent.jsonl", root=_SHARED_ROOT),
    # The sender we hand claimed jobs to
    "sender_script": Path(os.environ.get(
        "LEASE_SENDER_SCRIPT", str(Path(__file__).with_name("lease_sender.py")))),
    "python_exe": os.environ.get("LEASE_PYTHON", sys.executable),
    # Timing
    "poll_seconds":      int(os.environ.get("LEASE_POLL_SECONDS", "10")),
    "settle_seconds":    int(os.environ.get("LEASE_SETTLE_SECONDS", "3")),    # ignore files younger than this (partial write guard)
    "job_grace_seconds": int(os.environ.get("LEASE_JOB_GRACE_SECONDS", "120")),  # wait this long for a job's Dropbox sync before declaring it missing
    "stale_minutes":     int(os.environ.get("LEASE_STALE_MINUTES", "30")),    # a job stuck in Sending longer than this gets flagged
}

_EMAIL_TOKEN = r"[^@\s]+@[^@\s]+\.[^@\s]+"
_EMAIL_RE = re.compile(rf"^{_EMAIL_TOKEN}$")
_REQUIRED_JOB_FIELDS = ["property", "tenant_name", "tenant_email", "pdf_path", "signing_name"]


# ======================================================================
# FLEET CONTRACT accessors — EDIT HERE (and only here) to match reality
# ======================================================================
_LEASE_TAGS = {"lease"}


def decision_is_lease(d: dict) -> bool:
    """True iff this decision is addressed to the lease agent."""
    return d.get("kind") in _LEASE_TAGS or d.get("agent") in _LEASE_TAGS


def decision_action(d: dict) -> str:
    """Normalized action: 'send' | 'reject' | 'feedback' | '' (unknown)."""
    raw = d.get("action") or d.get("decision") or ""
    return str(raw).strip().lower()


def decision_job_id(d: dict) -> str:
    """The key that maps a decision back to its job file stem in Pending."""
    for key in ("job", "job_id", "item", "item_id", "id"):
        if d.get(key):
            return str(d[key]).strip()
    return ""


def decision_uid(path: Path, d: dict) -> str:
    """A stable, unique id for the idempotency ledger (one decision = once)."""
    return str(d.get("decision_id") or d.get("id") or path.stem).strip()


def decision_feedback(d: dict) -> str:
    return str(d.get("feedback") or "").strip()
# ======================================================================
# END FLEET CONTRACT
# ======================================================================


def notify(level: str, message: str, job: str = ""):
    """Append a notification for the fleet notifier / supervisor Expo push.

    Same jsonl sink LeaseAgent uses; if the supervisor expects a different
    drop, change it in both places (or factor a shared notifier).
    """
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "agent": "LeaseWatcher",
        "level": level,          # info | success | error
        "job": job,
        "message": message,
    }
    path = CONFIG["notify_file"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"[{entry['ts']}] {level.upper()}: {message}")


def validate_job(job_path: Path):
    """Mirror of lease_sender.load_job — the watcher is the gatekeeper, so a
    malformed job is rejected BEFORE it can be claimed into Sending.

    Returns (job_dict, "") on success or (None, reason) on failure.
    """
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except Exception as e:
        return None, f"unreadable / invalid JSON: {e}"
    if not isinstance(job, dict):
        return None, "job JSON is not an object"
    missing = [k for k in _REQUIRED_JOB_FIELDS if not job.get(k)]
    if missing:
        return None, f"missing fields: {missing}"
    if not _EMAIL_RE.match(str(job["tenant_email"])):
        return None, f"tenant_email looks malformed: {job['tenant_email']!r}"
    if not Path(job["pdf_path"]).exists():
        return None, f"lease PDF not found: {job['pdf_path']}"
    return job, ""


class Ledger:
    """Agent-owned record of decision uids already handled. Never touches a
    shared file, so it can't race the other agents on the decisions dir."""

    def __init__(self, path: Path):
        self.path = path
        self._seen = set()
        if path.exists():
            self._seen = {
                line.strip() for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            }

    def __contains__(self, uid: str) -> bool:
        return uid in self._seen

    def add(self, uid: str):
        if uid in self._seen:
            return
        self._seen.add(uid)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(uid + "\n")


class LeaseWatcher:
    def __init__(self, once: bool = False, dry_run: bool = False):
        self.once = once
        self.dry_run = dry_run
        self.ledger = Ledger(CONFIG["state_dir"] / "processed_decisions.txt")
        self._stop = False
        self._first_seen = {}   # decision uid -> first-seen wall time (grace window)
        self._stale_warned = set()  # job names in Sending we've already flagged

    # ---- lifecycle ---------------------------------------------------
    def run(self):
        for d in ("sending_dir", "rejected_dir", "state_dir"):
            CONFIG[d].mkdir(parents=True, exist_ok=True)
        signal.signal(signal.SIGINT, self._request_stop)
        try:
            signal.signal(signal.SIGTERM, self._request_stop)
        except (ValueError, AttributeError):
            pass  # SIGTERM not settable on some platforms / threads

        mode = "DRY-RUN " if self.dry_run else ""
        notify("info", f"{mode}LeaseWatcher up. Decisions: {CONFIG['decisions_dir']}")
        while not self._stop:
            try:
                self.check_stale_sending()
                self.scan_decisions()
            except Exception as e:  # a bad tick must never kill the loop
                notify("error", f"watcher tick error (continuing): {e}")
            if self.once:
                break
            self._sleep(CONFIG["poll_seconds"])
        notify("info", f"{mode}LeaseWatcher stopped.")

    def _request_stop(self, *_):
        self._stop = True

    def _sleep(self, seconds: int):
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
        for path in sorted(d_dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
            try:
                if now - path.stat().st_mtime < CONFIG["settle_seconds"]:
                    continue  # possibly still being written
            except FileNotFoundError:
                continue
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue  # partial / non-JSON; retry a later tick
            if not isinstance(d, dict) or not decision_is_lease(d):
                continue  # not ours — never act on or move another agent's decision
            uid = decision_uid(path, d)
            if uid in self.ledger:
                continue
            self.handle_decision(path, d, uid)

    def handle_decision(self, path: Path, d: dict, uid: str):
        action = decision_action(d)
        job_id = decision_job_id(d)
        if action == "send":
            self.handle_send(uid, job_id)
        elif action == "reject":
            self.handle_reject(uid, job_id)
        elif action == "feedback":
            notify("info", f"feedback decision for {job_id or '?'}: "
                           f"{decision_feedback(d)!r} — regeneration is the fill "
                           f"agent's job (piece 3), no action in v1.", job_id)
            if not self.dry_run:
                self.ledger.add(uid)
        else:
            notify("error", f"decision {uid!r} has unknown action "
                            f"{action!r} — ignored.", job_id)
            if not self.dry_run:
                self.ledger.add(uid)

    # ---- send --------------------------------------------------------
    def handle_send(self, uid: str, job_id: str):
        if not job_id:
            notify("error", f"send decision {uid!r} carries no job id — ignored.")
            if not self.dry_run:
                self.ledger.add(uid)
            return

        job_json = CONFIG["pending_dir"] / f"{job_id}.json"
        if not job_json.exists():
            # Could be Dropbox sync lag. Wait out a grace window before giving up.
            first = self._first_seen.setdefault(uid, time.time())
            if time.time() - first < CONFIG["job_grace_seconds"]:
                return  # try again next tick; do NOT mark processed yet
            notify("error", f"approved SEND for {job_id!r} but no job file appeared "
                            f"in Pending after {CONFIG['job_grace_seconds']}s — giving "
                            f"up. Resolve manually.", job_id)
            if not self.dry_run:
                self.ledger.add(uid)
            return

        job, reason = validate_job(job_json)
        if job is None:
            notify("error", f"approved job {job_id!r} is invalid ({reason}) — NOT sent.",
                   job_id)
            if not self.dry_run:
                self.ledger.add(uid)
            return

        if self.dry_run:
            notify("info", f"[DRY-RUN] would claim + send {job_id!r} -> "
                           f"{job['tenant_email']} ({job['property']}).", job_id)
            return

        claimed = self.claim(job_json)
        if claimed is None:
            self.ledger.add(uid)  # claim refused (stuck prior run); flagged in claim()
            return

        notify("info", f"claimed {job_id!r}; sending lease for {job['property']} "
                       f"-> {job['tenant_email']}.", job_id)
        rc = self.run_sender(claimed)
        self.reconcile(job_id, claimed, rc)
        self.ledger.add(uid)

    def claim(self, job_json: Path):
        """Claim-first move Pending -> Sending. Returns the new path or None.

        This is the anti-double-send invariant: once here, the job is out of
        every pickable location before the browser ever opens.
        """
        dest = CONFIG["sending_dir"] / job_json.name
        if dest.exists():
            notify("error", f"cannot claim {job_json.name}: already present in "
                            f"Sending — a prior run is stuck. Resolve it first.",
                   job_json.stem)
            return None
        try:
            shutil.move(str(job_json), str(dest))
            return dest
        except Exception as e:
            notify("error", f"claim failed for {job_json.name}: {e}", job_json.stem)
            return None

    def run_sender(self, sending_path: Path) -> int:
        cmd = [CONFIG["python_exe"], str(CONFIG["sender_script"]),
               "--job", str(sending_path)]
        try:
            return subprocess.run(cmd).returncode
        except FileNotFoundError as e:
            notify("error", f"could not launch lease_sender ({e}).", sending_path.stem)
            return 1

    def reconcile(self, job_id: str, claimed: Path, rc: int):
        """Interpret the sender's exit. The watcher NEVER moves the job here —
        lease_sender owns all post-launch placement (Sent / Failed / left in
        Sending). We only surface anything that needs a human."""
        still_sending = claimed.exists()
        if rc == 0 and not still_sending:
            return  # sent + filed to Sent; lease_sender already pinged success
        if rc == 0 and still_sending:
            notify("error", f"{job_id!r}: sender exited 0 but job is still in "
                            f"Sending — inconsistent, review manually.", job_id)
        elif rc == 1 and not still_sending:
            return  # clean pre-send abort; lease_sender filed it to Failed + pinged
        else:
            # rc 2/3 (or any non-zero with the job left in Sending): a send may
            # have gone out. Do NOT resend. Flag for manual verification.
            notify("error", f"{job_id!r}: lease_sender exited {rc} with the job left "
                            f"in Sending — a send may have gone out. Verify in "
                            f"Authentisign before any resend; do NOT blindly retry.",
                   job_id)

    # ---- reject ------------------------------------------------------
    def handle_reject(self, uid: str, job_id: str):
        if not job_id:
            notify("error", f"reject decision {uid!r} carries no job id — ignored.")
            if not self.dry_run:
                self.ledger.add(uid)
            return

        job_json = CONFIG["pending_dir"] / f"{job_id}.json"
        if self.dry_run:
            notify("info", f"[DRY-RUN] would reject {job_id!r} -> Rejected.", job_id)
            return

        CONFIG["rejected_dir"].mkdir(parents=True, exist_ok=True)
        moved_any = False
        # Move the pdf too, if we can read its path from the job first.
        if job_json.exists():
            try:
                job = json.loads(job_json.read_text(encoding="utf-8"))
                pdf = Path(job.get("pdf_path", ""))
                if pdf.exists():
                    self._safe_move(pdf, CONFIG["rejected_dir"] / pdf.name)
                    moved_any = True
            except Exception as e:
                notify("error", f"reject {job_id!r}: could not read job to locate pdf "
                                f"({e}); moving json only.", job_id)
            self._safe_move(job_json, CONFIG["rejected_dir"] / job_json.name)
            moved_any = True

        if moved_any:
            notify("info", f"{job_id!r}: rejected in the app -> moved to Rejected.", job_id)
        else:
            notify("error", f"reject {job_id!r}: no job file found in Pending — "
                            f"nothing to move.", job_id)
        self.ledger.add(uid)

    @staticmethod
    def _safe_move(src: Path, dest: Path):
        if dest.exists():
            dest = dest.with_name(f"{src.stem}-{int(time.time())}{src.suffix}")
        shutil.move(str(src), str(dest))

    # ---- stale sweep -------------------------------------------------
    def check_stale_sending(self):
        """Flag jobs marooned in Sending (a run that died mid-send). Never
        retried automatically — only Jay can know if the invite went out."""
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
                notify("error", f"STALE: {job_json.name} has sat in Sending for "
                                f"{int(age // 60)}m — a send likely died mid-run. NOT "
                                f"retried. Check Audit + Authentisign, then resolve by "
                                f"hand.", job_json.stem)
                self._stale_warned.add(job_json.name)
        self._stale_warned &= present  # forget names once resolved


if __name__ == "__main__":
    LeaseWatcher(
        once="--once" in sys.argv,
        dry_run="--dry-run" in sys.argv,
    ).run()
