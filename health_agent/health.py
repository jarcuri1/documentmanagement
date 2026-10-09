"""
health.py — the health agent's front door
=========================================
  python health.py init-key                      # FIRST: create the encryption key
                                                 #   (shows your recovery key once)
  python health.py restore-key                   # new PC: type the recovery key
  python health.py recovery-key                  # show it again (needs this login)
  python health.py migrate-plaintext data/health.db   # old unencrypted DB -> vault

  python health.py setup                         # quick profile questions
  python health.py chat                          # talk to the coach (it can also
                                                 #   save pains / history you mention)
  python health.py brief [--push] [--quiet]      # today's plan; --push notifies
                                                 #   your phone; --quiet prints nothing
                                                 #   (for the scheduler, keeps logs clean)
  python health.py brief --last                  # re-read the latest brief
  python health.py visits [-n 5]                 # the coach's notes from past chats

  python health.py pain knee 4 --side left --kind ache --trigger "lunges"
  python health.py pains                         # open aches + trend
  python health.py resolve-pain 3

  python health.py add-history surgery "Rotator cuff repair" --details "right" --since 2019
  python health.py add-history medication "Lisinopril 10mg"
  python health.py add-history restriction "No overhead pressing" --details "per Dr. K"
  python health.py history [--all]
  python health.py retire-history 5              # e.g. stopped a medication

  python health.py import-apple export.xml [--since 2026-01-01]
  python health.py import-steps steps.csv        # date,steps[,source]
  python health.py import-json payload.json      # a saved Health Auto Export file
  python health.py sync-tonal [--days 30]        # experimental direct Tonal pull
  python health.py status                        # what the agent currently sees
"""

import argparse
import getpass
import sys
from pathlib import Path

import vault
from health_store import DEFAULT_DATA_DIR, MEDICAL_KINDS, HealthStore


def _print_pains(store):
    pains = store.open_pains()
    if not pains:
        print("No open aches or pains.")
    for p in pains:
        trend = " -> ".join(str(s) for s in p["severities"][-5:])
        side = f"{p['side']} " if p["side"] else ""
        print(f"#{p['id']:<4} {side}{p['area']}: {p['severity']}/10 {p['kind']}"
              f"  (trend {trend}; since {p['first_logged'][:10]})"
              + (f"  trigger: {p['trigger']}" if p["trigger"] else ""))


def cmd_setup(store, _):
    questions = [
        ("name", "First name"), ("birth_year", "Birth year"), ("sex", "Sex"),
        ("height", "Height"), ("weight", "Weight"),
        ("goals", "Main goals (e.g. build strength, lose 15 lb, back pain-free)"),
        ("training_days_per_week", "Days per week you want to train"),
        ("preferred_session_minutes", "Typical session length in minutes"),
    ]
    current = store.profile()
    print("Press Enter to keep the current value.")
    for key, label in questions:
        cur = current.get(key, "")
        ans = input(f"{label}{f' [{cur}]' if cur else ''}: ").strip()
        if ans:
            store.set_profile(key, ans)
    print("Saved. Add medical history with `add-history`, or just tell the coach in `chat`.")


def cmd_chat(store, _):
    from coach import CoachSession
    session = CoachSession(store)
    print("Health coach: tell it how you feel, ask for today's workout, or share history. "
          "'quit' to end the visit. The conversation is saved encrypted.\n")
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if text.lower() in ("quit", "exit", "q"):
            break
        if text:
            print(f"\ncoach> {session.ask(text)}\n")
    print("Writing the visit note...")
    note = session.close()
    if note:
        print(f"\n{note}\n")


def cmd_brief(store, args):
    if args.last:
        notes = store.notes("brief", limit=1)
        print(f"{notes[-1]['created_at'][:16]}\n{notes[-1]['text']}" if notes else "No brief yet.")
        return
    from coach import daily_brief, push_to_phone
    brief = daily_brief(store)
    if not brief:
        sys.exit("Could not generate a brief.")
    if not args.quiet:
        print(brief)
    if args.push:
        ok = push_to_phone("Today's training brief", brief)
        if not args.quiet:
            print("\n(pushed to phone)" if ok else "\n(no push_outbox here; set HEALTH_PUSH_OUTBOX)")


def cmd_init_key(args):
    try:
        vault.load_key()
        sys.exit("A key already exists on this machine. Use `recovery-key` to see it.")
    except vault.VaultError:
        pass
    key = vault.new_key()
    vault.save_key(key)
    print("Encryption key created and stored in this computer's credential vault.\n")
    print("RECOVERY KEY (write it on paper or put it in your password manager):\n")
    print(f"    {vault.format_recovery_key(key)}\n")
    print("Without it, a new computer or a reinstalled Windows means the record cannot "
          "be opened by anyone, including you.")


def cmd_restore_key(args):
    key = vault.parse_recovery_key(getpass.getpass("Recovery key: "))
    enc = DEFAULT_DATA_DIR / "health.db.enc"
    if enc.exists():
        vault.decrypt(key, enc.read_bytes())  # raises on a wrong key, before we store it
    vault.save_key(key)
    print("Key restored." + (" It opens your existing record." if enc.exists() else ""))


def cmd_migrate(args):
    src = Path(args.path)
    if not src.exists():
        sys.exit(f"{src} not found")
    store_path = DEFAULT_DATA_DIR / "health.db.enc"
    if store_path.exists():
        sys.exit(f"{store_path} already exists. Refusing to overwrite an encrypted record.")
    vault.import_plaintext(vault.EncryptedDB(store_path), src)
    HealthStore().snapshot()  # proves it opens
    print(f"Encrypted into {store_path}.\nNow delete the old file: {src}\n"
          "(Also empty the Recycle Bin. On an SSD a deleted file can't be reliably wiped, "
          "so turning on BitLocker is the real protection for anything that was ever "
          "written in plain text.)")


def cmd_visits(store, args):
    notes = store.notes("visit", limit=args.n)
    if not notes:
        print("No visit notes yet. They're written when a `chat` ends.")
    for n in notes:
        print(f"--- {n['created_at'][:16]} ---\n{n['text']}\n")


def cmd_pain(store, args):
    pid = store.log_pain(args.area, args.severity, side=args.side, kind=args.kind,
                         trigger=args.trigger, notes=args.notes)
    print(f"Logged pain #{pid}.")
    if args.severity >= 8:
        print("8/10 or higher is a see-a-clinician level — get it checked before training it.")


def cmd_history(store, args):
    items = store.medical(include_inactive=args.all)
    if not items:
        print("No medical history yet.")
    for m in items:
        flag = "" if m["active"] else "  (inactive)"
        extra = " — ".join(x for x in (m["details"], m["since"]) if x)
        print(f"#{m['id']:<4} {m['kind']:<12} {m['name']}{f'  ({extra})' if extra else ''}{flag}")


def cmd_status(store, _):
    snap = store.snapshot()
    p = snap["profile"]
    print(f"Profile: {', '.join(f'{k}={v}' for k, v in p.items()) or 'not set (run setup)'}")
    print(f"Medical items: {len(snap['medical'])} active")
    s = snap["steps"]
    print(f"Steps: {s['days_reported']} days reported in last 14, avg {s['avg'] or '—'}")
    for d in s["daily"][-7:]:
        print(f"   {d['day']}  {d['steps']:>6}")
    print(f"Workouts (14d): {len(snap['workouts'])}")
    for w in snap["workouts"][-7:]:
        mg = f" [{', '.join(w['muscle_groups'])}]" if w["muscle_groups"] else ""
        print(f"   {w['started_at'][:16]}  {w['source']:<12} {w['title']}  "
              f"{w['duration_min']:.0f} min{mg}")
    _print_pains(store)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Personal health agent")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-key")
    sub.add_parser("restore-key")
    sub.add_parser("recovery-key")
    mp = sub.add_parser("migrate-plaintext"); mp.add_argument("path")
    sub.add_parser("setup")
    sub.add_parser("chat")
    b = sub.add_parser("brief"); b.add_argument("--push", action="store_true")
    b.add_argument("--quiet", action="store_true"); b.add_argument("--last", action="store_true")
    v = sub.add_parser("visits"); v.add_argument("-n", type=int, default=5)
    sub.add_parser("status")

    p = sub.add_parser("pain")
    p.add_argument("area"); p.add_argument("severity", type=int)
    p.add_argument("--side", default=""); p.add_argument("--kind", default="")
    p.add_argument("--trigger", default=""); p.add_argument("--notes", default="")
    sub.add_parser("pains")
    r = sub.add_parser("resolve-pain"); r.add_argument("id", type=int)

    h = sub.add_parser("add-history")
    h.add_argument("kind", choices=MEDICAL_KINDS); h.add_argument("name")
    h.add_argument("--details", default=""); h.add_argument("--since", default="")
    hl = sub.add_parser("history"); hl.add_argument("--all", action="store_true")
    rh = sub.add_parser("retire-history"); rh.add_argument("id", type=int)

    ia = sub.add_parser("import-apple"); ia.add_argument("path"); ia.add_argument("--since")
    isc = sub.add_parser("import-steps"); isc.add_argument("path")
    ij = sub.add_parser("import-json"); ij.add_argument("path")
    t = sub.add_parser("sync-tonal"); t.add_argument("--days", type=int, default=30)

    args = ap.parse_args(argv)
    try:
        run(args)
    except vault.VaultError as e:
        sys.exit(f"Health record: {e}")


def run(args):
    if args.cmd == "init-key":
        return cmd_init_key(args)
    if args.cmd == "restore-key":
        return cmd_restore_key(args)
    if args.cmd == "recovery-key":
        return print(vault.format_recovery_key(vault.load_key()))
    if args.cmd == "migrate-plaintext":
        return cmd_migrate(args)

    store = HealthStore()
    if args.cmd == "visits":
        cmd_visits(store, args)
    elif args.cmd == "setup":
        cmd_setup(store, args)
    elif args.cmd == "chat":
        cmd_chat(store, args)
    elif args.cmd == "brief":
        cmd_brief(store, args)
    elif args.cmd == "status":
        cmd_status(store, args)
    elif args.cmd == "pain":
        cmd_pain(store, args)
    elif args.cmd == "pains":
        _print_pains(store)
    elif args.cmd == "resolve-pain":
        print("Resolved." if store.resolve_pain(args.id) else "No open pain with that id.")
    elif args.cmd == "add-history":
        print(f"Saved #{store.add_medical(args.kind, args.name, args.details, args.since)}.")
    elif args.cmd == "history":
        cmd_history(store, args)
    elif args.cmd == "retire-history":
        print("Marked inactive." if store.set_medical_active(args.id, False) else "No such item.")
    elif args.cmd in ("import-apple", "import-steps", "import-json"):
        import ingest
        fn = {"import-apple": lambda: ingest.import_apple_health_xml(store, args.path, args.since),
              "import-steps": lambda: ingest.import_steps_csv(store, args.path),
              "import-json": lambda: ingest.import_json_file(store, args.path)}[args.cmd]
        print(fn())
    elif args.cmd == "sync-tonal":
        import tonal_client
        try:
            print(f"Synced {tonal_client.sync(store, args.days)} Tonal workouts.")
        except tonal_client.TonalError as e:
            sys.exit(f"Tonal sync failed: {e}")


if __name__ == "__main__":
    main()
