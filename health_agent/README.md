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
                                                    health.db.enc (encrypted)
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
| `vault.py` | AES-256-GCM encryption of the whole record; key kept in Windows Credential Manager |
| `coach.py` | Claude coach: clinician-style reasoning over the full record, visit notes, daily brief |

## 1. Install

Python 3.11 or newer.
```
cd health_agent
pip install -r requirements.txt
set ANTHROPIC_API_KEY=...            # or put it in C:\AIAgents\shared\.env like the other agents
set HEALTH_DATA_DIR=C:\AIAgents\HealthAgent\data   # where the encrypted record lives
python health.py init-key            # FIRST. Write down the recovery key it shows you.
python health.py setup
```
Already ran an earlier version? Move the old unencrypted file into the vault,
then delete it: `python health.py migrate-plaintext data\health.db`.

See **Privacy and security** below for what's protected and how.

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
python health.py brief --push    # today's plan; your phone gets a "brief ready" notice
python health.py brief --last    # read the latest brief
python health.py visits          # the coach's notes from your past conversations
```

### How the coach thinks

It's instructed to reason like a thorough physician, and to coach your training too:
- **Takes a history** (site, onset, character, what makes it better or worse)
  and asks follow-up questions before concluding.
- **Thinks in differentials:** the most likely explanations plus the
  must-not-miss ones, what would change its mind, and what an in-person exam
  or test would check.
- **Remembers you over years.** Each chat ends with a clinician-style visit
  note (S/A/P), stored encrypted. The next visit reads the recent notes plus
  your current and past history, so it can spot patterns: the same knee flaring
  every time volume jumps, a symptom that keeps coming back.
- **Weighs evidence from everywhere.** WHO, national guideline bodies
  (UK NICE/SIGN, European societies, Canada, Australia, Germany, Japan, the
  Nordic countries and the US), Cochrane reviews, and trials from any country.
  No country's guidelines are the default. When they disagree (for example on
  blood-pressure thresholds or screening), it shows each position and why.
- **Considers every kind of option**: physio, load management, sleep, diet,
  acupuncture, tai chi, supplements, medication. It grades the evidence for
  each (strong / moderate / limited / insufficient / against). It is equally
  skeptical of drug marketing, supplement marketing and contrarian claims, and
  is told never to invent a citation.

It runs on `claude-opus-5-5` with adaptive thinking at high effort for chats.

**It still isn't your doctor.** It can't examine you or order tests. It won't
tell you to start, stop or change a prescription; it gives you the questions to
ask your prescriber instead. For red flags (chest pain, breathlessness at rest,
spreading numbness or weakness, a hot or swollen joint after injury, pain 8/10
or higher) it sends you to in-person care instead of working around them.

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
      cmd: 'python', args: ['health.py', 'brief', '--push', '--quiet'],  // --quiet: no health text in supervisor logs
      schedule: { type: 'daily', at: '06:15' },
      env: {}, envControls: [], modes: { 'normal': [] },
    },
```
(Match `schedule` to whatever types your supervisor supports.) The brief goes
out on the same `shared\push_outbox` rail as the lease pushes.

## Privacy and security

**What's protected, and how**

| Where your data could leak | What the agent does |
|---|---|
| The file on disk (backups, Dropbox, a stolen or recycled PC) | The whole record (history, pains, steps, workouts, **every conversation**, visit notes) is one AES-256-GCM encrypted file. It is only decrypted in memory while a command runs. Tampering is detected and refused. |
| The encryption key | Kept in Windows Credential Manager, protected by your Windows login (DPAPI), so the unattended pieces still work. Copying the data folder to another PC gets nothing. |
| Phone → PC | Token required on every post. Over Tailscale the traffic is encrypted end to end. Don't expose port 8765 to the internet, and don't use plain Wi-Fi without Tailscale. |
| The Claude API | Only what the coach needs is sent. Name, email, phone, address and birth date are stripped, and your age is sent instead of your birth year. Data sent through the API isn't used to train models under Anthropic's commercial terms, and is kept for a limited period (check Anthropic's current retention policy). Organizations can arrange Zero Data Retention with Anthropic. |
| Phone notifications | Push services (Google/Apple) see notification text, so the push only says "your brief is ready". The brief stays in the vault. Set `HEALTH_PUSH_DETAIL=1` if you want the text on your lock screen. |
| Logs | The scheduled brief runs with `--quiet`, so no health text ends up in supervisor logs. The ingest server logs only counts. |
| Git | `data/` and `*.enc` are git-ignored. Nothing personal is ever committed. |

**Do these too:**
1. **Write down the recovery key** from `init-key` (paper, or your password
   manager). Lose it along with this Windows login and the record is gone for
   good. That's the price of real encryption: there is no back door.
2. **Turn on BitLocker** (Windows → Settings → Device encryption / BitLocker).
   It covers anything Windows itself caches, and the old unencrypted file if
   you used an earlier version.
3. **Back up** `health.db.enc` anywhere you like (it's unreadable without the
   key). On a new PC: copy it over, run `python health.py restore-key`, and
   type the recovery key.
4. Keep `HEALTH_KEY` out of shared `.env` files. It's only for restoring.

**What this does not do:** it's not HIPAA-certified and isn't a medical
records system. It protects the record from the file being copied, synced or
stolen. Someone logged in to your Windows account can still open it, the same
way they could open your email. Lock the PC when you step away.

## Tests

```
python -m unittest test_health_agent -v
```
These run fully offline. A fake Claude client exercises the coach's tool loop.
