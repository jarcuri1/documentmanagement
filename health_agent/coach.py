"""
Coach — Claude, reading your whole health picture, building around YOU
=======================================================================
Every answer is grounded in the store's snapshot: medical history and
restrictions, open aches and pains (with their trend), the last two weeks of
steps, and recent Tonal/other workouts. The coach can also WRITE: tell it
"my left knee is a 4 after yesterday's lunges" or "I had rotator cuff surgery
in 2019" in plain words and it files that into the right table itself.

Two entry points:
  CoachSession(store).ask(t)  one conversational turn (health.py chat)
  daily_brief(store)           today's plan in ~10 lines (health.py brief),
                               optionally pushed to the phone via the fleet
                               push_outbox rail

Model: claude-opus-5-5, adaptive thinking. Server-side refusal fallback is
enabled ("default" routing) so a safety-classifier false positive on medical
wording gets re-run on another model instead of a dead end.

Not a doctor: the system prompt makes the coach work AROUND what you report,
and send you to a clinician for red flags instead of programming through them.
"""

import json
import os
from pathlib import Path

MODEL = os.environ.get("HEALTH_MODEL", "claude-opus-5-5")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOOL_ROUNDS = 8

SYSTEM_PROMPT = """You are a personal health and training coach for one person. \
You have their health record (inside <health_record>): profile, medical \
history, current aches and pains with trends, daily phone step counts, and \
recent workouts — most of them on Tonal, a home strength-training machine \
with digital weights and a library of programs and single movements.

How to coach:
- Build every suggestion around their record. A logged injury, surgery, \
condition, medication or restriction changes what you recommend. Say which \
item shaped the advice ("because your right shoulder is at 5/10, ...").
- For an open ache: steer load away from it, suggest regressions or \
substitutions (e.g. on Tonal: lighter weight, Eccentric/Spotter off, \
single-arm or seated variants, a mobility program), and favor movements \
that don't aggravate it. Use the trend: getting worse over several logs \
means back off further; improving means a cautious step up.
- Recovery: look at what muscle groups they trained in the last 48 hours \
and how many days in a row they've trained before suggesting the next \
session. Use step counts for the daily movement target — build from their \
real average, not a generic 10,000.
- Be concrete: name the session, the movements, sets x reps or minutes, and \
an effort cue. Keep it short unless they ask for detail.
- Use only numbers that appear in the record. If something you need is \
missing (no step data yet, no workouts synced), say so and ask.

Keeping the record current:
- When they tell you about a new ache, an injury, a diagnosis, surgery, \
medication, allergy, or a doctor's restriction, call the matching tool to \
save it, then confirm in one line what you saved. Ask a quick follow-up \
only if severity or body area is unclear. When an ache is gone, resolve it.

Safety — you are not their doctor:
- Red flags mean stop and see a clinician, not a workout tweak: chest pain \
or pressure, shortness of breath at rest, fainting, sudden severe headache, \
numbness/tingling/weakness spreading down a limb, loss of bladder or bowel \
control, a joint that is hot, very swollen or can't bear weight after an \
injury, pain 8/10 or higher, or pain that wakes them at night and keeps \
getting worse. For chest pain, trouble breathing or stroke signs, tell them \
to call emergency services now.
- Don't diagnose or change medications. If a medication or condition \
plausibly matters for exercise (e.g. blood thinners, beta blockers, \
diabetes, blood pressure, pregnancy, recent surgery), mention it and \
suggest they confirm limits with their clinician."""

TOOLS = [
    {
        "name": "log_pain",
        "description": "Save an ache or pain the user reports. Call it whenever they describe a new or "
                       "changed ache, so the trend is tracked.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "area": {"type": "string", "description": "Body area, e.g. 'knee', 'lower back', 'shoulder'."},
                "side": {"type": "string", "enum": ["left", "right", "both", "center", ""]},
                "severity": {"type": "integer", "description": "0-10 as the user rates it."},
                "kind": {"type": "string", "description": "e.g. sharp, dull, ache, stiffness, burning. '' if unknown."},
                "trigger": {"type": "string", "description": "What set it off, '' if unknown."},
                "notes": {"type": "string"},
            },
            "required": ["area", "side", "severity", "kind", "trigger", "notes"],
            "additionalProperties": False,
        },
    },
    {
        "name": "resolve_pain",
        "description": "Mark an open ache as resolved (gone). Use the pain id from the record.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"pain_id": {"type": "integer"}},
            "required": ["pain_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "add_medical_item",
        "description": "Save a medical-history item: condition, surgery, injury, medication, allergy, "
                       "restriction (e.g. a doctor's 'no overhead pressing'), or note.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["condition", "surgery", "injury", "medication",
                                                    "allergy", "restriction", "note"]},
                "name": {"type": "string"},
                "details": {"type": "string"},
                "since": {"type": "string", "description": "When it started / happened, '' if unknown."},
            },
            "required": ["kind", "name", "details", "since"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_medical_item_active",
        "description": "Mark a medical-history item inactive (e.g. stopped a medication, cleared by "
                       "doctor) or active again. Use the item id from the record.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"item_id": {"type": "integer"}, "active": {"type": "boolean"}},
            "required": ["item_id", "active"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_profile",
        "description": "Save a profile fact: name, birth_year, sex, height, weight, goals, "
                       "training_days_per_week, preferred_session_minutes, etc.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
            "required": ["key", "value"],
            "additionalProperties": False,
        },
    },
]


def run_tool(store, name, args):
    """Execute one coach tool against the store; returns a short result string."""
    if name == "log_pain":
        pid = store.log_pain(args["area"], args["severity"], side=args.get("side", ""),
                             kind=args.get("kind", ""), trigger=args.get("trigger", ""),
                             notes=args.get("notes", ""))
        return f"saved pain #{pid}"
    if name == "resolve_pain":
        return "resolved" if store.resolve_pain(args["pain_id"]) else "no open pain with that id"
    if name == "add_medical_item":
        mid = store.add_medical(args["kind"], args["name"], args.get("details", ""),
                                args.get("since", ""))
        return f"saved medical item #{mid}"
    if name == "set_medical_item_active":
        ok = store.set_medical_active(args["item_id"], args["active"])
        return "updated" if ok else "no medical item with that id"
    if name == "set_profile":
        store.set_profile(args["key"], args["value"])
        return "saved"
    raise ValueError(f"unknown tool {name}")


def record_block(store):
    return ("<health_record>\n" + json.dumps(store.snapshot(), indent=1, default=str)
            + "\n</health_record>")


def _client():
    import anthropic
    return anthropic.Anthropic()


def _text(response):
    return "\n".join(b.text for b in response.content if b.type == "text").strip()


def _create(client, messages, effort, tools=True):
    kwargs = dict(
        model=MODEL,
        max_tokens=16000,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        thinking={"type": "adaptive"},
        output_config={"effort": effort},
        messages=messages,
        betas=[FALLBACK_BETA],
        fallbacks="default",
    )
    if tools:
        kwargs["tools"] = TOOLS
    return client.beta.messages.create(**kwargs)


class CoachSession:
    """A running conversation. History is append-only (never edited), so the
    prompt cache and thinking blocks stay valid turn to turn. The record is
    re-sent only when it changed since the last time Claude saw it."""

    def __init__(self, store, client=None, effort="medium"):
        self.store = store
        self.client = client or _client()
        self.effort = effort
        self.history = []
        self._last_record = None

    def ask(self, text):
        record = record_block(self.store)
        content = text if record == self._last_record else f"{record}\n\n{text}"
        self._last_record = record
        self.history.append({"role": "user", "content": content})

        for _ in range(MAX_TOOL_ROUNDS):
            resp = _create(self.client, self.history, self.effort)
            self.history.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "refusal":
                return "I couldn't answer that one. Try rephrasing, or ask your clinician."
            if resp.stop_reason != "tool_use":
                return _text(resp)
            results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                try:
                    out, err = run_tool(self.store, block.name, block.input), False
                except Exception as e:
                    out, err = f"error: {e}", True
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": out, "is_error": err})
            self.history.append({"role": "user", "content": results})
        return "(stopped after too many record updates in one turn)"


BRIEF_ASK = """Write today's brief, plain text, max ~12 short lines:
1. One line on recovery status (what was trained lately, open aches and their trend).
2. Today's Tonal suggestion: session type + 3-6 movements with sets x reps or a \
named program/mobility session — or a rest/recovery day if that's smarter. Note \
any substitutions made for an ache or restriction.
3. Step target for today based on their recent average, with one idea to hit it.
4. If anything in the record is a red flag, lead with that instead and keep the rest minimal.
Do not call tools."""


def daily_brief(store, client=None):
    client = client or _client()
    resp = _create(client, [{"role": "user", "content": f"{record_block(store)}\n\n{BRIEF_ASK}"}],
                   effort="medium", tools=False)
    if resp.stop_reason == "refusal":
        return None
    return _text(resp)


def push_to_phone(title, body):
    """Drop the brief on the fleet's push_outbox rail (same contract as the
    lease agents). Returns False when no outbox is configured/present."""
    outbox = Path(os.environ.get("HEALTH_PUSH_OUTBOX", r"C:\AIAgents\shared\push_outbox"))
    if not outbox.parent.exists():
        return False
    import time
    outbox.mkdir(parents=True, exist_ok=True)
    name = f"health-{os.getpid()}-{int(time.time() * 1000)}.json"
    tmp = outbox / (name + ".tmp")
    tmp.write_text(json.dumps({"title": title, "body": body, "data": {"kind": "health"}}),
                   encoding="utf-8")
    tmp.replace(outbox / name)
    return True
