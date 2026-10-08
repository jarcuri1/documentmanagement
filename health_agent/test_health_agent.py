"""Offline tests: store, every ingest path, the HTTP server, and the coach's
tool loop against a fake Claude client. Run:  python -m pytest -q  (or
python test_health_agent.py)."""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import coach
import ingest
from health_store import HealthStore
from ingest_server import ThreadingHTTPServer, make_handler

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

    def _post(self, body, token=None, path="/ingest"):
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {token}"} if token else {})
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
        import os
        os.environ["HEALTH_PUSH_OUTBOX"] = str(self.dir / "push_outbox")
        try:
            self.assertTrue(coach.push_to_phone("t", "b"))
        finally:
            del os.environ["HEALTH_PUSH_OUTBOX"]
        self.assertEqual(len(list((self.dir / "push_outbox").glob("health-*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
