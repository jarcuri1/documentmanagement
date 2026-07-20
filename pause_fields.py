"""
pause_fields.py — assemble a test signing, then PAUSE so Jay can demonstrate
============================================================================
Runs the exact sender assembly (login, upload, overlay, RTS, templates,
participants), clicks Send Signing once so validation flags the sender
fill-in boxes, then STOPS and hands the mouse to Jay.

While paused it records everything: every mousedown/dblclick (with
coordinates), keypresses, and focus changes, plus a screenshot whenever the
'missing parameters' count changes. Jay fills the boxes by hand (and may click
Send Signing at the end) — the log shows the automation exactly what to do.

Run:  python pause_fields.py
Logs: prints events live; screenshots into Dropbox\Leases\Audit\pause-demo-*
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
from lease_sender import (CONFIG, SELECTORS, login_if_needed, _upload_document,
                          _apply_overlay_to_lease, _add_uploaded_document,
                          _add_template_by_name, _dismiss_stray_dialog,
                          _reconcile_participants, _missing_param_count)

JOB = Path(r"C:\Users\realt\AppData\Local\Temp\claude\C--AIAgents"
           r"\8cefbb03-fcc6-49db-82d4-ad07dfd15c00\scratchpad\dryrun\dryrun-job.json")
S = SELECTORS
SHOTS = Path(CONFIG["audit_dir"]) / f"pause-demo-{datetime.now():%Y%m%d-%H%M%S}"
SHOTS.mkdir(parents=True, exist_ok=True)


def main():
    job = json.loads(JOB.read_text(encoding="utf-8"))
    events = []

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            CONFIG["browser_profile_dir"], channel="chrome", headless=False,
            viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        def on_event(data):
            line = f"[{datetime.now():%H:%M:%S}] {data}"
            print("EVENT", line, flush=True)
            events.append(line)
        page.expose_function("jayEvent", on_event)

        page.goto(CONFIG["sign_url"], timeout=45_000)
        login_if_needed(page)
        page.wait_for_selector(S["logged_in_marker"], timeout=15_000)
        page.wait_for_timeout(1000)

        print("assembling the signing (same steps as the sender)...")
        page.click(S["new_signing_btn"], timeout=30_000); page.wait_for_timeout(2500)
        page.fill(S["signing_name_input"], "ZZ PAUSE DEMO - delete me", timeout=15_000)
        _upload_document(page, job["pdf_path"])
        page.click(S["continue_btn"], timeout=30_000)
        page.wait_for_selector(S["add_documents_btn"], timeout=30_000); page.wait_for_timeout(1500)
        _apply_overlay_to_lease(page, "Agent automated single_family_lease")
        for doc in job["documents"][1:]:
            _add_uploaded_document(page, doc)
        for tpl in CONFIG["packet_templates"]:
            _add_template_by_name(page, tpl)
        _reconcile_participants(page, job)
        _dismiss_stray_dialog(page)

        # Trigger validation: Send -> Proceed past the checkbox warning.
        page.click(S["send_btn"], timeout=30_000)
        page.wait_for_timeout(2000)
        try:
            if page.get_by_text("Do you still wish to proceed", exact=False).count():
                page.get_by_role("button", name="Proceed", exact=True).first.click(timeout=8_000)
                page.wait_for_timeout(2000)
        except Exception:
            pass

        # Arm the recorder AFTER assembly so only Jay's actions are logged.
        page.evaluate(
            """()=>{
              ['mousedown','dblclick','keydown','focusin'].forEach(t=>{
                document.addEventListener(t,(e)=>{
                  const tgt=e.target||{};
                  window.jayEvent(JSON.stringify({
                    type:t, x:e.clientX, y:e.clientY, key:e.key||null,
                    tag:tgt.tagName||null,
                    tid:(tgt.getAttribute&&tgt.getAttribute('data-testid'))||null,
                    cls:((tgt.className||'')+'').slice(0,50)
                  }));
                }, true);
              });
            }""")

        n = _missing_param_count(page)
        page.screenshot(path=str(SHOTS / "00-paused.png"), full_page=True)
        print()
        print("=" * 62)
        print(f"PAUSED — missing fields: {n}.  JAY: the window is yours.")
        print("Fill the flagged boxes exactly as you normally would (and feel")
        print("free to click Send Signing at the end). Everything is being")
        print("recorded. Close the browser window when you're done.")
        print("=" * 62, flush=True)

        last_n = n
        shot_i = 1
        deadline = time.time() + 20 * 60
        try:
            while time.time() < deadline:
                page.wait_for_timeout(1000)
                try:
                    now_n = _missing_param_count(page)
                except Exception:
                    break   # window closed
                if now_n != last_n:
                    page.screenshot(path=str(SHOTS / f"{shot_i:02d}-missing-{now_n}.png"),
                                    full_page=True)
                    print(f"*** missing count {last_n} -> {now_n} (screenshot saved)", flush=True)
                    last_n = now_n
                    shot_i += 1
                try:
                    if page.locator(S["sent_confirmation"]).count():
                        page.screenshot(path=str(SHOTS / f"{shot_i:02d}-SENT.png"), full_page=True)
                        print("*** SENT confirmation observed (screenshot saved)", flush=True)
                        page.wait_for_timeout(3000)
                        break
                except Exception:
                    break
        except KeyboardInterrupt:
            pass
        finally:
            (SHOTS / "events.log").write_text("\n".join(events), encoding="utf-8")
            print(f"\nevent log + screenshots: {SHOTS}")
            try:
                ctx.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
