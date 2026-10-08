"""
Ingest — turn whatever your phone sends into steps + workouts in the store
==========================================================================
Neither Apple Health nor Android Health Connect has a cloud API, so the phone
has to PUSH its data out. Every supported path lands here:

  1. Health Auto Export (iPhone app) -> REST API automation -> ingest_server.
     Payload: {"data": {"metrics": [{"name": "step_count", "data": [...]}],
                        "workouts": [...]}}
     Tonal writes every session to Apple Health, so Tonal workouts arrive on
     this same feed (tagged source "tonal").
  2. iOS Shortcut / Android Tasker / anything -> ingest_server with the simple
     shape {"date": "2026-10-07", "steps": 8123, "source": "android"}
     (or a list of those).
  3. Apple Health full export (Health app -> profile -> Export All Health
     Data -> export.xml) -> `python health.py import-apple export.xml`.
  4. CSV with `date,steps[,source]` -> `python health.py import-steps file.csv`.

Every parser is tolerant: unknown metrics and fields are ignored, a bad row
is skipped, and the return value says how much landed.
"""

import csv
import hashlib
import json
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime

STEP_METRIC_NAMES = {"step_count", "steps", "stepcount"}
APPLE_STEP_TYPE = "HKQuantityTypeIdentifierStepCount"


def _day(value):
    """'2026-10-07 00:00:00 -0400' / '2026-10-07T08:00:00Z' / '2026-10-07' -> '2026-10-07'."""
    s = str(value).strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    raise ValueError(f"unrecognized date: {value!r}")


def _iso(value):
    """Best-effort ISO timestamp (keeps local wall-clock time, drops the offset
    so string comparison in SQLite works across sources)."""
    s = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.replace("Z", "+0000"), fmt).strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
    return s[:19]


def _qty(v):
    if isinstance(v, dict):
        v = v.get("qty", 0)
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _looks_tonal(*values):
    return any("tonal" in str(v).lower() for v in values if v)


def _workout_id(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


def _add_steps(store, per_day_source):
    for (day, source), count in per_day_source.items():
        store.upsert_steps(day, round(count), source)
    return len(per_day_source)


# ----------------------------------------------------------------------
# JSON payloads (ingest server)
# ----------------------------------------------------------------------
def ingest_payload(store, payload):
    """Accepts a Health Auto Export payload or the simple {date, steps} shape
    (single object or list). Returns {"step_days": n, "workouts": n}."""
    if isinstance(payload, list):
        return _ingest_simple(store, payload)
    if isinstance(payload, dict) and "steps" in payload and "date" in payload:
        return _ingest_simple(store, [payload])
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    return _ingest_auto_export(store, data)


def _ingest_simple(store, rows):
    per = defaultdict(float)
    for r in rows:
        try:
            per[(_day(r["date"]), str(r.get("source") or "phone"))] = _qty(r["steps"])
        except (KeyError, ValueError, TypeError):
            continue
    return {"step_days": _add_steps(store, per), "workouts": 0}


def _ingest_auto_export(store, data):
    per = defaultdict(float)
    for metric in data.get("metrics", []) or []:
        if str(metric.get("name", "")).lower() not in STEP_METRIC_NAMES:
            continue
        for sample in metric.get("data", []) or []:
            try:
                # Unaggregated exports send many samples per day: sum them.
                per[(_day(sample["date"]), str(sample.get("source") or "phone"))] += _qty(sample.get("qty"))
            except (KeyError, ValueError):
                continue

    n_workouts = 0
    for w in data.get("workouts", []) or []:
        start = w.get("start") or w.get("startDate")
        if not start:
            continue
        name = w.get("name") or w.get("workoutActivityType") or "Workout"
        src = w.get("source") or w.get("sourceName") or ""
        # Health Auto Export reports duration in seconds.
        duration_min = _qty(w.get("duration")) / 60
        store.upsert_workout(
            ext_id=w.get("id") or _workout_id("hae", start, name),
            source="tonal" if _looks_tonal(name, src, w.get("metadata")) else "apple_health",
            started_at=_iso(start),
            title=name,
            duration_min=round(duration_min, 1),
            calories=_qty(w.get("activeEnergyBurned") or w.get("activeEnergy")),
            details={k: w[k] for k in ("distance", "avgHeartRate", "maxHeartRate", "intensity")
                     if k in w},
        )
        n_workouts += 1
    return {"step_days": _add_steps(store, per), "workouts": n_workouts}


# ----------------------------------------------------------------------
# Files
# ----------------------------------------------------------------------
def import_steps_csv(store, path, default_source="phone"):
    per = defaultdict(float)
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            row = {k.strip().lower(): v for k, v in row.items() if k}
            try:
                count = _qty((row.get("steps") or row.get("step_count") or "").replace(",", ""))
                per[(_day(row["date"]), row.get("source") or default_source)] += count
            except (KeyError, ValueError):
                continue
    return {"step_days": _add_steps(store, per), "workouts": 0}


def import_apple_health_xml(store, path, since=None):
    """Stream Apple's export.xml (it can be gigabytes) — steps summed per
    day per device, workouts kept with Tonal tagged. `since` = 'YYYY-MM-DD'."""
    per = defaultdict(float)
    n_workouts = 0
    for _, el in ET.iterparse(path, events=("end",)):
        tag = el.tag
        if tag == "Record" and el.get("type") == APPLE_STEP_TYPE:
            day = (el.get("startDate") or "")[:10]
            if day and (not since or day >= since):
                per[(day, el.get("sourceName") or "iphone")] += _qty(el.get("value"))
        elif tag == "Workout":
            start = el.get("startDate") or ""
            if start and (not since or start[:10] >= since):
                kind = (el.get("workoutActivityType") or "Workout").replace(
                    "HKWorkoutActivityType", "")
                src = el.get("sourceName") or ""
                dur = _qty(el.get("duration"))
                if el.get("durationUnit", "min") == "s":
                    dur /= 60
                store.upsert_workout(
                    ext_id=_workout_id("ahx", start, kind, src),
                    source="tonal" if _looks_tonal(src) else "apple_health",
                    started_at=_iso(start),
                    title=kind,
                    duration_min=round(dur, 1),
                    calories=_qty(el.get("totalEnergyBurned")),
                )
                n_workouts += 1
        if tag in ("Record", "Workout"):
            el.clear()  # keep memory flat on huge exports
    return {"step_days": _add_steps(store, per), "workouts": n_workouts}


def import_json_file(store, path):
    with open(path, encoding="utf-8") as f:
        return ingest_payload(store, json.load(f))
