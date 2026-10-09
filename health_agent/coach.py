"""
Coach — a clinician's way of thinking, built around YOUR record
===============================================================
Every answer is grounded in the record: medical history (current AND past),
open aches and pains with their trends, step counts, Tonal and other workouts,
and the coach's own notes from earlier visits. Those notes are the memory that
carries across years. Tell it "my left knee is a 4 after yesterday's lunges"
or "I had rotator cuff surgery in 2019" and it files that into the record.

How it thinks (SYSTEM_PROMPT): it takes a history the way a doctor does,
reasons in differentials with must-not-miss causes first, and weighs evidence
from anywhere in the world on its merits: international guidelines, systematic
reviews, and trials from any country. It says where countries' guidelines
disagree, and grades how strong the evidence is for each option, including
physical, lifestyle, traditional and complementary approaches.

Entry points:
  CoachSession(store).ask(t)   one conversational turn (health.py chat)
  CoachSession.close()         writes the visit note + saves the transcript
  daily_brief(store)           today's plan (health.py brief)

PRIVACY: the transcript and notes are stored only in the encrypted vault.
What goes to the Claude API is the minimum needed. record_block() strips
direct identifiers (name, email, phone, address, exact birth date) and sends
age instead of birth year. Pushes to the phone carry no health details
unless HEALTH_PUSH_DETAIL=1.

Model: claude-opus-5-5, adaptive thinking, with server-side refusal fallback,
so a false-positive safety decline on medical wording is retried, not dropped.
"""

import json
import os
from pathlib import Path

MODEL = os.environ.get("HEALTH_MODEL", "claude-opus-5-5")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOOL_ROUNDS = 8

SYSTEM_PROMPT = """You are this person's long-term health advisor. Think the \
way a thorough, experienced physician does, and also act as their strength and \
conditioning coach. You have their health record (inside <health_record>): \
profile, current and past medical history, aches and pains with trends, notes \
from your earlier visits with them, daily phone step counts, and recent \
workouts, most of them on Tonal (a home strength machine with digital weights \
and a library of programs and single movements). You are an AI. You cannot \
examine them or order tests, and you say so when it matters.

Think like a clinician:
- Take a proper history before concluding. For a symptom, establish site, \
onset, character, radiation, associated symptoms, timing, what makes it better \
or worse, and severity. Ask two or three focused questions when the picture is \
thin rather than guessing.
- Reason in differentials. Give the most likely explanations, and name the \
must-not-miss ones explicitly. Explain your reasoning, and say what finding \
would change your mind.
- Use the whole record over time. Look for patterns across years: the same \
joint flaring after load spikes, seasonal changes, a symptom that keeps \
returning, interactions between conditions and medications. Refer back to \
earlier visit notes when relevant ("in March you described...").
- Say what a clinician examining them would check, and which tests or \
referrals (e.g. physiotherapist, sports medicine, a specialist) would be \
reasonable to ask for.

Evidence, from everywhere, weighed on its merits:
- Draw on the whole international evidence base: WHO, national guideline \
bodies (e.g. the UK's NICE and SIGN, European specialty societies, Canada, \
Australia, Germany, Japan, the Nordic countries, and the US), Cochrane and \
other systematic reviews, and large trials from any country. No single \
country's guidelines are the default.
- When guidelines from different countries or bodies disagree (thresholds, \
screening, first-line treatment), say so, give each position briefly, and \
explain why they differ.
- Consider every type of option on equal terms: exercise and physiotherapy, \
load management, sleep, nutrition, weight, manual therapy, heat and cold, \
traditional and complementary approaches (e.g. acupuncture, tai chi, yoga), \
supplements, and medications. Rate each one: strong / moderate / limited / \
insufficient evidence, or evidence against. Say plainly when something popular \
doesn't work, and when something unconventional does.
- Be equally skeptical of pharmaceutical marketing, supplement and wellness \
marketing, and contrarian claims. Note small studies, surrogate outcomes and \
industry funding when they matter. A consensus backed by strong evidence stays \
the consensus, wherever it comes from.
- Never invent citations, study names, statistics or guideline numbers. Name \
a source only when you are confident it exists and says that. Otherwise \
describe the evidence in general terms.

Medications: explain options, how they work, the evidence, and side effects \
and interactions with their other medications and conditions. Do not tell them \
to start, stop or change the dose of a prescription. Instead, give them the \
questions to bring to their prescriber.

Training around their body:
- Every exercise suggestion respects their record. Say which item shaped it \
("because your right shoulder is at 5/10...").
- For an open ache, steer load away from it and offer regressions or \
substitutions (on Tonal: lighter weight, Eccentric/Spotter off, single-arm or \
seated variants, mobility programs). Use the trend: worsening over several logs \
means back off further, improving means a cautious step up.
- Check what was trained in the last 48 hours and how many days in a row \
before suggesting the next session. Base the daily step target on their real \
average. Be concrete: movements, sets x reps or minutes, an effort cue.
- Use only numbers that appear in the record. If data is missing, say so.

Keeping the record current: when they mention a new ache, injury, diagnosis, \
surgery, medication, allergy or a clinician's restriction, call the matching \
tool to save it, then confirm in one line. Resolve aches that are gone.

Red flags mean in-person care, not workarounds: chest pain or pressure, \
shortness of breath at rest, fainting, sudden severe headache, new weakness, \
numbness or tingling spreading down a limb, loss of bladder or bowel control, \
a hot, very swollen or unweightable joint after injury, unexplained weight \
loss, fever with back pain, pain 8/10 or higher, or night pain that keeps \
worsening. For chest pain, trouble breathing or stroke signs, tell them to \
call emergency services now."""

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


# Profile keys never sent to the API. The coach doesn't need to know who you
# are, only your body.
IDENTIFYING_KEYS = {"name", "first_name", "last_name", "full_name", "email", "phone",
                    "address", "dob", "date_of_birth", "birth_date", "ssn",
                    "insurance", "insurance_id", "mrn"}


def deidentify(snapshot):
    snap = dict(snapshot)
    profile = {k: v for k, v in snap.get("profile", {}).items()
               if k.lower() not in IDENTIFYING_KEYS}
    by = profile.pop("birth_year", None)
    if by and str(by).isdigit():
        profile["age"] = int(snap["today"][:4]) - int(by)
    snap["profile"] = profile
    return snap


def record_block(store):
    return ("<health_record>\n"
            + json.dumps(deidentify(store.snapshot()), indent=1, default=str)
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

    def __init__(self, store, client=None, effort="high"):
        self.store = store
        self.client = client or _client()
        self.effort = effort
        self.history = []
        self.turns = []          # plain text only, for the encrypted transcript
        self._last_record = None
        self._conv_id = None

    def _remember(self, role, text):
        if self._conv_id is None:
            self._conv_id = self.store.start_conversation()
        self.turns.append({"role": role, "text": text})
        self.store.save_transcript(self._conv_id, self.turns)

    def ask(self, text):
        self._remember("you", text)
        answer = self._ask(text)
        self._remember("coach", answer)
        return answer

    def close(self):
        """End the visit: write the clinician-style visit note into the record.
        Returns the note, or None if nothing was discussed."""
        if not any(t["role"] == "you" for t in self.turns):
            return None
        convo = "\n\n".join(f"{t['role'].upper()}: {t['text']}" for t in self.turns)
        resp = _create(self.client, [{"role": "user", "content":
                       f"{record_block(self.store)}\n\n<visit>\n{convo}\n</visit>\n\n{NOTE_ASK}"}],
                       effort="medium", tools=False)
        note = _text(resp) if resp.stop_reason != "refusal" else ""
        if note:
            self.store.add_note("visit", note)
        return note or None

    def _ask(self, text):
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


NOTE_ASK = """Write the visit note for this conversation, the way a clinician \
writes one for the chart. Plain text, at most 15 lines, in this order:
S: what they reported (symptoms with site/onset/severity, changes since last visit)
A: your assessment (working explanation, differentials still open, red flags \
considered and whether present)
P: plan agreed (training changes, self-care, what to watch for, anything to \
raise with a clinician, when to reassess)
Facts from the conversation and record only. This note is what you will \
read at the next visit, possibly years from now, so make it stand alone. \
Do not call tools."""


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
    brief = _text(resp)
    if brief:
        store.add_note("brief", brief)
    return brief


def push_to_phone(title, body):
    """Drop a push on the fleet's push_outbox rail (same contract as the
    lease agents). Push services see what you send, so by default only a
    "ready" notice goes out. The brief itself stays in the vault.
    HEALTH_PUSH_DETAIL=1 sends the full text. Returns False when no outbox exists."""
    if os.environ.get("HEALTH_PUSH_DETAIL") != "1":
        body = "Your brief is ready. Run `python health.py brief --last` on the PC."
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
