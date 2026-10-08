"""
Tonal sync — strength detail (movements, weights, volume) straight from Tonal
=============================================================================
Tonal has NO public API. There are two ways Tonal data reaches this agent:

  A. RELIABLE (recommended): Tonal app -> Settings -> Apple Health (or Health
     Connect on Android) ON. Every Tonal session is then written to your
     phone's health store, and the phone feed (ingest.py) carries it here,
     tagged source "tonal". You get date, duration, calories — enough for
     recovery and scheduling — but not per-movement weights.

  B. EXPERIMENTAL (this file): log in to Tonal's own backend the way the
     Tonal app does and pull workout history with volume and muscle groups.
     The endpoints are undocumented and can change or break without notice,
     and using them may be against Tonal's terms of service — your call.
     Every endpoint is env-overridable so a change is a config fix, and a
     failure here never affects anything else in the agent.

CONFIG (env)
  TONAL_EMAIL, TONAL_PASSWORD       your Tonal login
  TONAL_AUTH_CLIENT_ID              the Tonal app's Auth0 client id (required
                                    for B; not shipped here — find it from a
                                    community Tonal client or the app's login
                                    request)
  TONAL_AUTH_URL                    default https://tonal.auth0.com/oauth/token
  TONAL_API_BASE                    default https://api.tonal.com
  TONAL_USERINFO_PATH               default /v6/users/userinfo
  TONAL_ACTIVITIES_PATH             default /v6/users/{user_id}/workout-activities

RUN
  python health.py sync-tonal              # pulls the last 30 days
  python health.py sync-tonal --days 90
"""

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta

AUTH_URL = os.environ.get("TONAL_AUTH_URL", "https://tonal.auth0.com/oauth/token")
API_BASE = os.environ.get("TONAL_API_BASE", "https://api.tonal.com").rstrip("/")
USERINFO_PATH = os.environ.get("TONAL_USERINFO_PATH", "/v6/users/userinfo")
ACTIVITIES_PATH = os.environ.get("TONAL_ACTIVITIES_PATH",
                                 "/v6/users/{user_id}/workout-activities")


class TonalError(RuntimeError):
    pass


def _request(url, data=None, token=None, timeout=30):
    headers = {"Accept": "application/json", "User-Agent": "health-agent/1.0"}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raise TonalError(f"{e.code} from {url}: {e.read()[:300]!r}") from None
    except urllib.error.URLError as e:
        raise TonalError(f"could not reach {url}: {e.reason}") from None


def login():
    email = os.environ.get("TONAL_EMAIL")
    password = os.environ.get("TONAL_PASSWORD")
    client_id = os.environ.get("TONAL_AUTH_CLIENT_ID")
    if not (email and password and client_id):
        raise TonalError("set TONAL_EMAIL, TONAL_PASSWORD and TONAL_AUTH_CLIENT_ID "
                         "(or use the Apple Health route — see tonal_client.py)")
    resp = _request(AUTH_URL, {
        "grant_type": "password", "username": email, "password": password,
        "client_id": client_id, "scope": "openid offline_access",
    })
    token = (resp or {}).get("id_token") or (resp or {}).get("access_token")
    if not token:
        raise TonalError("login returned no token")
    return token


def _muscles(activity):
    """Pull muscle-group names out of whatever shape the activity carries."""
    found = set()
    for key in ("muscleGroups", "targetedMuscleGroups", "bodyRegions"):
        for m in activity.get(key) or []:
            found.add(str(m.get("name") if isinstance(m, dict) else m).lower())
    for mv in activity.get("movements") or activity.get("workoutSetActivity") or []:
        if isinstance(mv, dict):
            for m in mv.get("muscleGroups") or []:
                found.add(str(m.get("name") if isinstance(m, dict) else m).lower())
    return sorted(found - {"", "none"})


def _movements(activity):
    out = []
    for mv in activity.get("movements") or activity.get("workoutSetActivity") or []:
        if not isinstance(mv, dict):
            continue
        name = mv.get("name") or mv.get("movementName") or (mv.get("movement") or {}).get("name")
        if name:
            out.append({k: v for k, v in {
                "name": name,
                "reps": mv.get("repCount") or mv.get("reps"),
                "weight_lbs": mv.get("avgWeight") or mv.get("weight"),
                "volume_lbs": mv.get("volume") or mv.get("totalVolume"),
            }.items() if v not in (None, "")})
    return out


def sync(store, days=30):
    """Pull recent Tonal workouts into the store. Returns the count saved."""
    token = login()
    me = _request(API_BASE + USERINFO_PATH, token=token) or {}
    user_id = me.get("id") or me.get("userId")
    if not user_id:
        raise TonalError("userinfo had no user id — TONAL_USERINFO_PATH may have changed")

    url = API_BASE + ACTIVITIES_PATH.format(user_id=user_id)
    activities = _request(url, token=token) or []
    if isinstance(activities, dict):  # some versions wrap the list
        activities = activities.get("items") or activities.get("data") or []

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    saved = 0
    for a in activities:
        start = str(a.get("beginTime") or a.get("startTime") or a.get("createdAt") or "")
        if not start or start[:10] < cutoff:
            continue
        overview = a.get("workoutPreview") or a.get("overview") or {}
        dur = a.get("totalDuration") or a.get("duration") or overview.get("totalDuration") or 0
        store.upsert_workout(
            ext_id=f"tonal-{a.get('id') or a.get('activityId') or start}",
            source="tonal",
            started_at=start[:19],
            title=overview.get("workoutTitle") or a.get("workoutTitle") or a.get("title") or "Tonal workout",
            duration_min=round(float(dur or 0) / 60, 1),  # Tonal reports seconds
            volume_lbs=a.get("totalVolume") or overview.get("totalVolume") or 0,
            calories=a.get("totalCalories") or overview.get("totalCalories") or 0,
            muscle_groups=_muscles(a),
            details={"movements": _movements(a)},
        )
        saved += 1
    return saved
