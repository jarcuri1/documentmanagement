"""
HealthStore — the one place the health agent keeps what it knows about you
==========================================================================
A single SQLite file. Everything else (the ingest server, the Tonal sync, the
coach) reads and writes through here, so the data has one shape no matter
where it came from.

Tables
  profile          key/value: name, birth year, height, goals, ...
  medical          conditions, surgeries, injuries, medications, allergies,
                   restrictions — anything your history says the coach must
                   respect. `active=0` keeps old items as history.
  pain_log         aches and pains: where, which side, 0-10, what kind, what
                   set it off. Open until you mark it resolved.
  steps            one row per day per source (phone, watch, ...). The
                   day's number is the MAX across sources, never the sum —
                   iPhone + Watch both count the same walk.
  workouts         Tonal and anything else (walks, runs, rides): when, how
                   long, volume, muscle groups.

The DB holds medical information. It lives OUTSIDE the repo by default
(HEALTH_DATA_DIR) and the folder is git-ignored in case it is pointed inside.
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

DEFAULT_DATA_DIR = Path(os.environ.get(
    "HEALTH_DATA_DIR", str(Path(__file__).with_name("data"))))

MEDICAL_KINDS = ("condition", "surgery", "injury", "medication", "allergy",
                 "restriction", "note")

# Body areas the coach knows how to reason about. Free text is accepted too;
# this list just keeps the common ones spelled the same way for trend queries.
BODY_AREAS = ("neck", "upper back", "lower back", "shoulder", "elbow", "wrist",
              "hand", "chest", "abdomen", "hip", "glute", "hamstring", "quad",
              "knee", "calf", "shin", "ankle", "foot", "head", "other")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profile (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS medical (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    name       TEXT NOT NULL,
    details    TEXT DEFAULT '',
    since      TEXT DEFAULT '',
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pain_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at   TEXT NOT NULL,
    area        TEXT NOT NULL,
    side        TEXT DEFAULT '',
    severity    INTEGER NOT NULL,
    kind        TEXT DEFAULT '',
    trigger     TEXT DEFAULT '',
    notes       TEXT DEFAULT '',
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS steps (
    day    TEXT NOT NULL,
    source TEXT NOT NULL,
    count  INTEGER NOT NULL,
    PRIMARY KEY (day, source)
);
CREATE TABLE IF NOT EXISTS workouts (
    ext_id        TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    title         TEXT DEFAULT '',
    duration_min  REAL DEFAULT 0,
    volume_lbs    REAL DEFAULT 0,
    calories      REAL DEFAULT 0,
    muscle_groups TEXT DEFAULT '[]',
    details       TEXT DEFAULT '{}'
);
"""


def _now():
    return datetime.now().isoformat(timespec="seconds")


class HealthStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else DEFAULT_DATA_DIR / "health.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(_SCHEMA)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------
    def set_profile(self, key, value):
        with self._db() as db:
            db.execute("INSERT INTO profile(key, value) VALUES(?, ?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, str(value)))

    def profile(self):
        with self._db() as db:
            return {r["key"]: r["value"] for r in db.execute("SELECT * FROM profile")}

    # ------------------------------------------------------------------
    # Medical history
    # ------------------------------------------------------------------
    def add_medical(self, kind, name, details="", since=""):
        kind = kind.lower().strip()
        if kind not in MEDICAL_KINDS:
            raise ValueError(f"kind must be one of {', '.join(MEDICAL_KINDS)}")
        with self._db() as db:
            cur = db.execute(
                "INSERT INTO medical(kind, name, details, since, created_at) "
                "VALUES(?, ?, ?, ?, ?)", (kind, name.strip(), details, since, _now()))
            return cur.lastrowid

    def set_medical_active(self, item_id, active):
        with self._db() as db:
            cur = db.execute("UPDATE medical SET active=? WHERE id=?",
                             (1 if active else 0, item_id))
            return cur.rowcount == 1

    def medical(self, include_inactive=False):
        q = "SELECT * FROM medical"
        if not include_inactive:
            q += " WHERE active=1"
        with self._db() as db:
            return [dict(r) for r in db.execute(q + " ORDER BY kind, id")]

    # ------------------------------------------------------------------
    # Aches and pains
    # ------------------------------------------------------------------
    def log_pain(self, area, severity, side="", kind="", trigger="", notes="",
                 logged_at=None):
        severity = int(severity)
        if not 0 <= severity <= 10:
            raise ValueError("severity is 0-10")
        with self._db() as db:
            cur = db.execute(
                "INSERT INTO pain_log(logged_at, area, side, severity, kind, "
                "trigger, notes) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (logged_at or _now(), area.lower().strip(), side.lower().strip(),
                 severity, kind, trigger, notes))
            return cur.lastrowid

    def resolve_pain(self, pain_id):
        with self._db() as db:
            cur = db.execute("UPDATE pain_log SET resolved_at=? "
                             "WHERE id=? AND resolved_at IS NULL", (_now(), pain_id))
            return cur.rowcount == 1

    def open_pains(self):
        """Unresolved complaints, latest entry per area+side (so logging your
        knee daily shows the trend's newest reading, with the history count)."""
        with self._db() as db:
            rows = [dict(r) for r in db.execute(
                "SELECT * FROM pain_log WHERE resolved_at IS NULL "
                "ORDER BY logged_at")]
        by_spot = {}
        for r in rows:
            spot = (r["area"], r["side"])
            prev = by_spot.get(spot)
            r["entries"] = (prev["entries"] + 1) if prev else 1
            r["first_logged"] = prev["first_logged"] if prev else r["logged_at"]
            r["severities"] = (prev["severities"] if prev else []) + [r["severity"]]
            by_spot[spot] = r
        return sorted(by_spot.values(), key=lambda r: -r["severity"])

    def pain_history(self, days=30):
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        with self._db() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM pain_log WHERE logged_at >= ? ORDER BY logged_at",
                (since,))]

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------
    def upsert_steps(self, day, count, source="phone"):
        day = day if isinstance(day, str) else day.isoformat()
        with self._db() as db:
            db.execute("INSERT INTO steps(day, source, count) VALUES(?, ?, ?) "
                       "ON CONFLICT(day, source) DO UPDATE SET count=excluded.count",
                       (day[:10], source, int(count)))

    def daily_steps(self, days=14):
        since = (date.today() - timedelta(days=days - 1)).isoformat()
        with self._db() as db:
            return [dict(r) for r in db.execute(
                "SELECT day, MAX(count) AS steps FROM steps WHERE day >= ? "
                "GROUP BY day ORDER BY day", (since,))]

    # ------------------------------------------------------------------
    # Workouts
    # ------------------------------------------------------------------
    def upsert_workout(self, ext_id, source, started_at, title="", duration_min=0,
                       volume_lbs=0, calories=0, muscle_groups=None, details=None):
        with self._db() as db:
            db.execute(
                "INSERT INTO workouts(ext_id, source, started_at, title, "
                "duration_min, volume_lbs, calories, muscle_groups, details) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(ext_id) DO UPDATE SET "
                "source=excluded.source, started_at=excluded.started_at, "
                "title=excluded.title, duration_min=excluded.duration_min, "
                "volume_lbs=excluded.volume_lbs, calories=excluded.calories, "
                "muscle_groups=excluded.muscle_groups, details=excluded.details",
                (str(ext_id), source, started_at, title, float(duration_min or 0),
                 float(volume_lbs or 0), float(calories or 0),
                 json.dumps(sorted(set(muscle_groups or []))),
                 json.dumps(details or {})))

    def workouts(self, days=14):
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        with self._db() as db:
            rows = [dict(r) for r in db.execute(
                "SELECT * FROM workouts WHERE started_at >= ? ORDER BY started_at",
                (since,))]
        for r in rows:
            r["muscle_groups"] = json.loads(r["muscle_groups"] or "[]")
            r["details"] = json.loads(r["details"] or "{}")
        return rows

    # ------------------------------------------------------------------
    # Snapshot — everything the coach needs, in one dict
    # ------------------------------------------------------------------
    def snapshot(self, days=14):
        steps = self.daily_steps(days)
        counts = [s["steps"] for s in steps]
        return {
            "today": date.today().isoformat(),
            "profile": self.profile(),
            "medical": self.medical(),
            "open_pains": self.open_pains(),
            "pain_history_30d": self.pain_history(30),
            "steps": {
                "daily": steps,
                "avg": round(sum(counts) / len(counts)) if counts else None,
                "days_reported": len(counts),
            },
            "workouts": self.workouts(days),
        }
