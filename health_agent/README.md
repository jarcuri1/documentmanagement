# Health Agent

A personal health and training coach that knows your Tonal workouts, your
phone's step count, your medical history, and your current aches and pains,
and builds its suggestions around all of them.

```
 Android: Health Connect ──(HC Webhook app)──────────► ingest_server.py ─┐
 Tonal ──► Health Connect ──► (same phone feed) ─────────────────────────┤
 Tonal (experimental direct API) ──► tonal_client.py ────────────────────┤
 You: `health.py pain / add-history` or just tell the coach in chat ─────┤
                                                                         ▼
                                                    health.db (SQLite, local)
                                                                         │
                                              coach.py (Claude) ◄────────┘
                                   chat · daily brief · push to phone
```

| File | What it does |
|---|---|
| `health.py` | CLI: setup, chat, brief, pain log, medical history, imports, status |
| `health_store.py` | SQLite store: profile, medical, pain_log, steps, workouts |
| `ingest.py` | Parsers: HC Webhook (Android Health Connect) JSON, Health Auto Export (iPhone) JSON, simple `{date, steps}`, Apple `export.xml`, CSV |
| `ingest_server.py` | Token-protected `POST /ingest` your phone posts to |
| `tonal_client.py` | Experimental direct pull of Tonal history (movements, volume, muscle groups) |
| `coach.py` | Claude coach: reads the full record, saves pains/history you mention, writes the daily brief |

## 1. Install

```
cd health_agent
pip install anthropic
set ANTHROPIC_API_KEY=...            # or put it in C:\AIAgents\shared\.env like the other agents
set HEALTH_DATA_DIR=C:\AIAgents\HealthAgent\data   # where health.db lives (default: ./data)
python health.py setup
```

`health.db` contains medical information. Keep it off shared drives, and never
commit it. `data/` is git-ignored.

## 2. Medical history, aches, and pains

Use either the CLI or plain English in `python health.py chat`. The coach saves
what you tell it on its own:

```
python health.py add-history surgery "Rotator cuff repair" --details "right shoulder" --since 2019
python health.py add-history medication "Lisinopril 10mg" --details "blood pressure"
python health.py add-history restriction "No running" --details "PT until Nov"
python health.py pain knee 4 --side left --kind ache --trigger "walking lunges"
python health.py pains            # open aches with their severity trend
python health.py resolve-pain 3   # when it's gone
```

> **you>** my left knee is around a 4 after yesterday's lunges, and I should mention I had ACL surgery on it in 2015
> **coach>** Saved: left knee 4/10 (ache, lunges) and ACL repair (2015). Today, skip lunges and split squats...

Log the same spot again as it changes. The coach reads the trend (3 → 5 → 6
means back off, 5 → 3 means step back up carefully).

## 3. Phone steps and Tonal workouts (Android)

Android keeps your steps and workouts in **Health Connect** (built into
Android 14+, a Play Store app on older phones). Health Connect has no cloud
API, so a small app on the phone reads it and posts it to the agent.

**a. Start the receiver** on the PC:
```
set HEALTH_INGEST_TOKEN=<random 32+ chars>
set HEALTH_TZ=America/New_York        # your time zone; steps are bucketed by YOUR day
python ingest_server.py               # listens on :8765
```
The phone has to reach the PC. Install **Tailscale** on both (free) and use the
PC's Tailscale IP. Don't port-forward this to the internet.

**b. Tonal → Health Connect:** in the Tonal app, turn on the **Health Connect**
integration and allow it to write exercise. Every Tonal session then lands in
Health Connect.

**c. Steps → Health Connect:** make sure whatever counts your steps (Google Fit,
Samsung Health, Fitbit, your watch app) is allowed to write **Steps** to Health
Connect. Check under *Settings → Health Connect → App permissions*.

**d. Health Connect → the agent:** install **HC Webhook**
([github.com/mcnaveen/health-connect-webhook](https://github.com/mcnaveen/health-connect-webhook),
on Google Play). It's open source, which matters because it handles health data.
- Grant it read access to **Steps** and **Exercise**. That's all the agent uses.
- Webhook URL: `http://<pc-tailscale-ip>:8765/ingest`
- Custom header: `X-Api-Key: <your HEALTH_INGEST_TOKEN>`
- Sync interval: 15–60 minutes.
- Use its manual **Sync** button once, then check with `python health.py status`.

Re-sent and overlapping batches are de-duplicated chunk by chunk, so syncing
often never inflates your steps. If both your phone and watch report steps, the
agent uses the higher number for the day, not the sum. Workouts written by
Tonal are tagged `tonal`. Other exercise (walks, rides) is kept too.

**No app?** Any automation (Tasker, etc.) can post
`{"date": "2026-10-07", "steps": 8123, "source": "android"}` to the same URL.

**Backfill:** `python health.py import-steps steps.csv` (columns `date,steps`).

<details><summary>iPhone instead</summary>

Use *Health Auto Export – JSON+CSV* → Automation → REST API to the same URL with
header `Authorization: Bearer <token>`, metrics Step Count + Workouts, daily
aggregation. Turn on Apple Health in the Tonal app. Backfill with
`python health.py import-apple export.xml`.
</details>

## 4. Tonal detail (optional, experimental)

The Health Connect route gives the date, duration, and Tonal's workout title. For per-movement weights,
volume, and muscle groups, `tonal_client.py` can log in to Tonal's own backend.
**Tonal has no public API.** This route uses undocumented endpoints that can
change without notice, and it may be against Tonal's terms. Every endpoint is
an env var, so a change is a config fix. See the header of `tonal_client.py`.
```
set TONAL_EMAIL=... & set TONAL_PASSWORD=... & set TONAL_AUTH_CLIENT_ID=...
python health.py sync-tonal --days 30
```

## 5. Use it

```
python health.py status          # what the agent sees right now
python health.py chat            # "what should I do on Tonal today?", "my back is tight"
python health.py brief --push    # today's plan, pushed to your phone
```

The coach runs on `claude-opus-5-5` with adaptive thinking. Server-side refusal
fallback is on, so a false-positive safety decline on medical wording gets
retried on another model instead of failing.

**Not a doctor.** For red flags (chest pain, shortness of breath, numbness
spreading down a limb, a hot or swollen joint after an injury, pain 8/10 or
higher) the coach tells you to see a clinician instead of planning a workout
around it. It won't diagnose or change medications.

## 6. Fleet supervisor (optional)

Add these to `DEFAULT_FLEET.agents` in `supervisor.js`, next to the lease agents:

```js
    health_ingest: {
      label: 'Health — Phone Ingest',
      enabled: true, dir: 'LeaseAgent\\health_agent',
      cmd: 'python', args: ['ingest_server.py'],
      schedule: { type: 'always' },          // long-running server
      env: {}, envControls: [], modes: { 'normal': [] },
    },
    health_brief: {
      label: 'Health — Morning Brief',
      enabled: true, dir: 'LeaseAgent\\health_agent',
      cmd: 'python', args: ['health.py', 'brief', '--push'],
      schedule: { type: 'daily', at: '06:15' },
      env: {}, envControls: [], modes: { 'normal': [] },
    },
```
(Match `schedule` to whatever types your supervisor supports.) The brief goes
out on the same `shared\push_outbox` rail as the lease pushes.

## Tests

```
python -m unittest test_health_agent -v
```
These run fully offline. A fake Claude client exercises the coach's tool loop.
