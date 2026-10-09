"""Offline tests: store, every ingest path, the HTTP server, and the coach's
tool loop against a fake Claude client. Run:  python -m pytest -q  (or
python test_health_agent.py)."""

import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import vault

# Tests use a throwaway key via the env override, never the real keyring.
os.environ["HEALTH_KEY"] = vault.format_recovery_key(vault.new_key())

import coach  # noqa: E402
import ingest  # noqa: E402
from health_store import HealthStore  # noqa: E402
from ingest_server import ThreadingHTTPServer, make_handler  # noqa: E402

TODAY = date.today().isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = HealthStore(self.dir / "h.db")

    def tearDown(self):
        self.tmp.cleanup()


class StoreTests(Base):
    def test_pain_trend_and_resolve(self):
        self.store.log_pain("Knee", 3, side="left", logged_at=f"{YESTERDAY}T08:00:00")
        pid = self.store.log_pain("knee", 5, side="left")
        self.store.log_pain("lower back", 2)
        pains = self.store.open_pains()
        self.assertEqual(pains[0]["area"], "knee")
        self.assertEqual(pains[0]["severities"], [3, 5])
        self.assertEqual(pains[0]["entries"], 2)
        self.store.resolve_pain(pid)
        # the older knee entry is still open on its own
        self.assertEqual([p["severity"] for p in self.store.open_pains() if p["area"] == "knee"], [3])

    def test_severity_bounds_and_medical_kind(self):
        with self.assertRaises(ValueError):
            self.store.log_pain("knee", 11)
        with self.assertRaises(ValueError):
            self.store.add_medical("vibe", "x")
        mid = self.store.add_medical("Medication", "Lisinopril")
        self.store.set_medical_active(mid, False)
        self.assertEqual(self.store.medical(), [])
        self.assertEqual(len(self.store.medical(include_inactive=True)), 1)

    def test_steps_take_max_across_sources(self):
        self.store.upsert_steps(TODAY, 8000, "iPhone")
        self.store.upsert_steps(TODAY, 8400, "Watch")
        self.store.upsert_steps(TODAY, 9000, "iPhone")  # re-sync overwrites
        self.assertEqual(self.store.daily_steps(1), [{"day": TODAY, "steps": 9000}])


class IngestTests(Base):
    def test_health_auto_export_payload(self):
        payload = {"data": {
            "metrics": [
                {"name": "step_count", "units": "count", "data": [
                    {"date": f"{TODAY} 08:00:00 -0400", "qty": 1000, "source": "iPhone"},
                    {"date": f"{TODAY} 12:00:00 -0400", "qty": 2500, "source": "iPhone"},
                    {"date": f"{YESTERDAY} 00:00:00 -0400", "qty": 7000, "source": "iPhone"},
                ]},
                {"name": "heart_rate", "data": [{"date": TODAY, "qty": 60}]},
            ],
            "workouts": [
                {"id": "w1", "name": "Traditional Strength Training", "source": "Tonal",
                 "start": f"{TODAY} 07:00:00 -0400", "duration": 1800,
                 "activeEnergyBurned": {"qty": 250, "units": "kcal"}},
                {"name": "Outdoor Walk", "start": f"{TODAY} 18:00:00 -0400", "duration": 2400},
            ],
        }}
        self.assertEqual(ingest.ingest_payload(self.store, payload), {"step_days": 2, "workouts": 2})
        steps = {d["day"]: d["steps"] for d in self.store.daily_steps(2)}
        self.assertEqual(steps, {TODAY: 3500, YESTERDAY: 7000})
        ws = {w["title"]: w for w in self.store.workouts(2)}
        self.assertEqual(ws["Traditional Strength Training"]["source"], "tonal")
        self.assertEqual(ws["Traditional Strength Training"]["duration_min"], 30)
        self.assertEqual(ws["Outdoor Walk"]["source"], "apple_health")
        # re-posting the same payload must not duplicate
        ingest.ingest_payload(self.store, payload)
        self.assertEqual(len(self.store.workouts(2)), 2)

    def test_health_connect_webhook_android(self):
        import os
        os.environ["HEALTH_TZ"] = "America/New_York"
        try:
            batch1 = {"timestamp": "2026-10-07T14:00:00.123Z", "app_version": "1.2.3", "steps": [
                {"count": 3000, "start_time": "2026-10-07T13:00:00Z", "end_time": "2026-10-07T14:00:00Z",
                 "metadata": {"data_origin": "com.google.android.apps.fitness"}},
                # 01:30Z on the 8th is 21:30 on the 7th in New York -> counts for the 7th
                {"count": 500, "start_time": "2026-10-08T01:30:00Z", "end_time": "2026-10-08T01:45:00Z",
                 "metadata": {"data_origin": "com.google.android.apps.fitness"}},
            ], "exercise": [
                {"type": "STRENGTH_TRAINING", "title": "Upper Body Power", "duration_seconds": 2400,
                 "start_time": "2026-10-07T11:00:00Z", "end_time": "2026-10-07T11:40:00Z",
                 "metadata": {"data_origin": "com.tonal.trainer"}},
                {"type": "WALKING", "start_time": "2026-10-07T20:00:00Z", "end_time": "2026-10-07T20:30:00Z"},
            ]}
            self.assertEqual(ingest.ingest_payload(self.store, batch1), {"step_days": 1, "workouts": 2})
            # same batch re-sent + an incremental batch with a new chunk: no double count
            ingest.ingest_payload(self.store, batch1)
            ingest.ingest_payload(self.store, {"app_version": "1.2.3", "steps": [
                {"count": 1000, "start_time": "2026-10-07T15:00:00Z", "end_time": "2026-10-07T16:00:00Z",
                 "metadata": {"data_origin": "com.google.android.apps.fitness"}}]})
        finally:
            del os.environ["HEALTH_TZ"]
        with self.store._db() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM steps")]
        self.assertEqual(rows, [{"day": "2026-10-07", "source": "com.google.android.apps.fitness",
                                 "count": 4500}])
        with self.store._db() as db:
            ws = {r["title"]: dict(r) for r in db.execute("SELECT * FROM workouts")}
        self.assertEqual(len(ws), 2)
        self.assertEqual(ws["Upper Body Power"]["source"], "tonal")
        self.assertEqual(ws["Upper Body Power"]["started_at"], "2026-10-07T07:00:00")
        self.assertEqual(ws["Upper Body Power"]["duration_min"], 40)
        self.assertEqual((ws["Walking"]["source"], ws["Walking"]["duration_min"]),
                         ("health_connect", 30))

    def test_simple_shapes(self):
        ingest.ingest_payload(self.store, {"date": TODAY, "steps": 4321, "source": "android"})
        ingest.ingest_payload(self.store, [{"date": YESTERDAY, "steps": "1200"}, {"bad": 1}])
        steps = {d["day"]: d["steps"] for d in self.store.daily_steps(2)}
        self.assertEqual(steps, {TODAY: 4321, YESTERDAY: 1200})

    def test_csv(self):
        p = self.dir / "s.csv"
        p.write_text(f"Date,Steps\n{TODAY},\"10,500\"\n{YESTERDAY},800\nnot-a-date,5\n")
        self.assertEqual(ingest.import_steps_csv(self.store, p)["step_days"], 2)
        self.assertEqual(self.store.daily_steps(1)[0]["steps"], 10500)

    def test_apple_export_xml(self):
        p = self.dir / "export.xml"
        p.write_text(f"""<?xml version="1.0"?>
<HealthData>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone" startDate="{TODAY} 08:00:00 -0400" value="400"/>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone" startDate="{TODAY} 09:00:00 -0400" value="600"/>
 <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Watch" startDate="{TODAY} 09:00:00 -0400" value="70"/>
 <Workout workoutActivityType="HKWorkoutActivityTypeTraditionalStrengthTraining" duration="42" durationUnit="min" sourceName="Tonal" startDate="{TODAY} 06:30:00 -0400"/>
</HealthData>""")
        self.assertEqual(ingest.import_apple_health_xml(self.store, p), {"step_days": 1, "workouts": 1})
        self.assertEqual(self.store.daily_steps(1)[0]["steps"], 1000)
        w = self.store.workouts(1)[0]
        self.assertEqual((w["source"], w["title"], w["duration_min"]),
                         ("tonal", "TraditionalStrengthTraining", 42))


class ServerTests(Base):
    def setUp(self):
        super().setUp()
        self.token = "t" * 20
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store, self.token))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def _post(self, body, token=None, path="/ingest", headers=None):
        hdrs = headers or ({"Authorization": f"Bearer {token}"} if token else {})
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method="POST",
                                     headers=hdrs)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_auth_and_ingest(self):
        self.assertEqual(self._post({"date": TODAY, "steps": 5})[0], 401)
        self.assertEqual(self._post({"date": TODAY, "steps": 5}, token="wrong")[0], 401)
        code, body = self._post({"date": TODAY, "steps": 5000}, token=self.token)
        self.assertEqual((code, body["step_days"]), (200, 1))
        code, _ = self._post({"date": TODAY, "steps": 6000}, path=f"/ingest?token={self.token}")
        self.assertEqual(code, 200)
        self.assertEqual(self.store.daily_steps(1)[0]["steps"], 6000)
        code, _ = self._post({"date": TODAY, "steps": 7000}, headers={"X-Api-Key": self.token})
        self.assertEqual(code, 200)
        self.assertEqual(self._post({"date": TODAY, "steps": 1}, headers={"X-Api-Key": "nope"})[0], 401)


class VaultTests(Base):
    def test_nothing_readable_on_disk(self):
        self.store.add_medical("condition", "Hypertension", "diagnosed 2021")
        self.store.log_pain("knee", 5, notes="sharp on stairs")
        self.store.add_note("visit", "S: knee pain on stairs")
        raw = (self.dir / "h.db").read_bytes()
        self.assertTrue(raw.startswith(vault.MAGIC))
        for secret in (b"Hypertension", b"knee", b"stairs", b"SQLite format"):
            self.assertNotIn(secret, raw)
        # a second store instance (another process) reads it back
        self.assertEqual(HealthStore(self.dir / "h.db").medical()[0]["name"], "Hypertension")

    def test_wrong_key_and_tampering_are_refused(self):
        self.store.log_pain("knee", 2)
        with self.assertRaises(vault.VaultError):
            HealthStore(self.dir / "h.db", key=vault.new_key()).open_pains()
        f = self.dir / "h.db"
        blob = bytearray(f.read_bytes())
        blob[-5] ^= 0xFF
        f.write_bytes(bytes(blob))
        with self.assertRaises(vault.VaultError):
            self.store.open_pains()

    def test_previous_version_kept_as_backup(self):
        self.store.log_pain("knee", 2)
        self.store.log_pain("hip", 3)
        self.assertTrue((self.dir / "h.db.bak").exists())
        self.assertFalse((self.dir / "h.db.lock").exists())

    def test_concurrent_writers_lose_nothing(self):
        def worker(i):
            for j in range(10):
                self.store.upsert_steps(f"2026-01-{i + 1:02d}", j, source=f"s{j}")
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with self.store._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM steps").fetchone()[0], 50)

    def test_recovery_key_round_trip(self):
        k = vault.new_key()
        text = vault.format_recovery_key(k)
        self.assertEqual(vault.parse_recovery_key(text.lower().replace("-", " ")), k)
        with self.assertRaises(vault.VaultError):
            vault.parse_recovery_key("ABCD-EFGH")

    def test_migrate_plaintext(self):
        old = self.dir / "old.db"
        db = sqlite3.connect(old)
        db.execute("CREATE TABLE profile (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO profile VALUES('goals', 'deadlift 405')")
        db.commit()
        db.close()
        target = self.dir / "migrated.enc"
        vault.import_plaintext(vault.EncryptedDB(target), old)
        self.assertEqual(HealthStore(target).profile(), {"goals": "deadlift 405"})
        self.assertNotIn(b"deadlift", target.read_bytes())


# ----------------------------------------------------------------------
# Coach tool loop against a fake client
# ----------------------------------------------------------------------
def _block(**kw):
    return SimpleNamespace(**kw)


class FakeClient:
    """Returns scripted responses; records each request for assertions."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


class CoachTests(Base):
    def test_chat_saves_pain_then_answers(self):
        fake = FakeClient([
            SimpleNamespace(stop_reason="tool_use", content=[
                _block(type="text", text="Saving that."),
                _block(type="tool_use", id="tu1", name="log_pain", input={
                    "area": "knee", "side": "left", "severity": 4, "kind": "ache",
                    "trigger": "lunges", "notes": ""}),
                _block(type="tool_use", id="tu2", name="add_medical_item", input={
                    "kind": "surgery", "name": "ACL repair", "details": "left", "since": "2015"}),
            ]),
            SimpleNamespace(stop_reason="end_turn",
                            content=[_block(type="text", text="Upper-body day today.")]),
            SimpleNamespace(stop_reason="end_turn",
                            content=[_block(type="text", text="Sure.")]),
        ])
        s = coach.CoachSession(self.store, client=fake)
        self.assertEqual(s.ask("left knee is a 4 after lunges, I had ACL repair in 2015"),
                         "Upper-body day today.")
        self.assertEqual(self.store.open_pains()[0]["severity"], 4)
        self.assertEqual(self.store.medical()[0]["name"], "ACL repair")
        # both tool results go back in ONE user message
        results = fake.requests[1]["messages"][-1]["content"]
        self.assertEqual([r["tool_use_id"] for r in results], ["tu1", "tu2"])
        # request shape
        req = fake.requests[0]
        self.assertEqual(req["model"], "claude-opus-5-5")
        self.assertEqual(req["fallbacks"], "default")
        self.assertIn("<health_record>", req["messages"][0]["content"])
        # record changed (pain saved) -> next turn re-sends it; unchanged -> doesn't
        s.ask("thanks")
        self.assertIn("<health_record>", fake.requests[2]["messages"][-1]["content"])
        fake.responses.append(SimpleNamespace(stop_reason="end_turn",
                                              content=[_block(type="text", text="ok")]))
        s.ask("one more")
        self.assertEqual(fake.requests[3]["messages"][-1]["content"], "one more")

    def test_visit_is_saved_encrypted_and_noted(self):
        fake = FakeClient([
            SimpleNamespace(stop_reason="end_turn", content=[_block(type="text", text="Tell me more.")]),
            SimpleNamespace(stop_reason="end_turn", content=[_block(type="text", text="S: hip ache\nA: ...")]),
        ])
        s = coach.CoachSession(self.store, client=fake)
        s.ask("my hip aches")
        self.assertIsNone(coach.CoachSession(self.store, client=fake).close())  # nothing said
        self.assertEqual(s.close(), "S: hip ache\nA: ...")
        conv = self.store.conversations()[0]
        self.assertEqual([t["role"] for t in conv["transcript"]], ["you", "coach"])
        self.assertIn("my hip aches", fake.requests[1]["messages"][0]["content"])
        self.assertNotIn("tools", fake.requests[1])
        # the note shows up in the record the next visit reads
        self.assertEqual(self.store.snapshot()["recent_visit_notes"][0]["text"], "S: hip ache\nA: ...")

    def test_identifiers_never_sent(self):
        self.store.set_profile("name", "Jay Example")
        self.store.set_profile("email", "jay@example.com")
        self.store.set_profile("birth_year", "1980")
        self.store.set_profile("goals", "stronger back")
        block = coach.record_block(self.store)
        for leak in ("Jay Example", "jay@example.com", "1980"):
            self.assertNotIn(leak, block)
        self.assertIn('"age": %d' % (date.today().year - 1980), block)
        self.assertIn("stronger back", block)

    def test_prompt_is_clinical_and_international(self):
        for phrase in ("differentials", "must-not-miss", "No single", "Never invent citations",
                       "traditional and complementary"):
            self.assertIn(phrase, coach.SYSTEM_PROMPT)

    def test_tool_error_is_reported_not_raised(self):
        fake = FakeClient([
            SimpleNamespace(stop_reason="tool_use", content=[
                _block(type="tool_use", id="x", name="log_pain", input={
                    "area": "knee", "side": "", "severity": 42, "kind": "", "trigger": "", "notes": ""})]),
            SimpleNamespace(stop_reason="end_turn", content=[_block(type="text", text="What's the 0-10?")]),
        ])
        coach.CoachSession(self.store, client=fake).ask("knee hurts a ton")
        self.assertTrue(fake.requests[1]["messages"][-1]["content"][0]["is_error"])

    def test_brief_and_push(self):
        fake = FakeClient([SimpleNamespace(stop_reason="end_turn",
                                           content=[_block(type="text", text="Rest day.")])])
        self.assertEqual(coach.daily_brief(self.store, client=fake), "Rest day.")
        self.assertNotIn("tools", fake.requests[0])
        self.assertEqual(self.store.notes("brief")[-1]["text"], "Rest day.")
        os.environ["HEALTH_PUSH_OUTBOX"] = str(self.dir / "push_outbox")
        try:
            self.assertTrue(coach.push_to_phone("t", "knee 6/10, skip squats"))
        finally:
            del os.environ["HEALTH_PUSH_OUTBOX"]
        pushed = list((self.dir / "push_outbox").glob("health-*.json"))
        self.assertEqual(len(pushed), 1)
        self.assertNotIn("knee", pushed[0].read_text())  # no health detail by default


if __name__ == "__main__":
    unittest.main()
