"""
set_login.py — store your SmartMLS credentials in Windows Credential Manager
============================================================================
Run this ONCE, on the fleet PC, in your own terminal:

    python set_login.py

It asks for your SmartMLS username and password. The password prompt is
hidden (getpass) and the values go straight into the Windows Credential
Manager, encrypted and tied to your Windows login. They are NEVER written to
a file and never printed. lease_sender.py reads them back from the vault only
when it needs to log itself in (see get_credentials there).

To update later, just run it again. To wipe them:
    python set_login.py --clear
"""

import sys
import getpass

import keyring

# --service tenanttracks stores TenantTracks credentials instead (used by
# tenanttracks_agent.py); default remains SmartMLS for lease_sender.py.
_SERVICES = {
    "smartmls": ("LeaseAgent-SmartMLS", "SmartMLS"),
    "tenanttracks": ("LeaseAgent-TenantTracks", "TenantTracks"),
}
_pick = "tenanttracks" if "--service" in sys.argv and \
    sys.argv[sys.argv.index("--service") + 1].lower() == "tenanttracks" else "smartmls"
SERVICE, LABEL = _SERVICES[_pick]
USER_KEY = "__username__"


def clear():
    user = keyring.get_password(SERVICE, USER_KEY)
    if user:
        try:
            keyring.delete_password(SERVICE, user)
        except keyring.errors.PasswordDeleteError:
            pass
    try:
        keyring.delete_password(SERVICE, USER_KEY)
    except keyring.errors.PasswordDeleteError:
        pass
    print(f"Cleared stored {LABEL} credentials.")


def main():
    if "--clear" in sys.argv:
        clear()
        return

    print(f"Storing {LABEL} credentials in Windows Credential Manager.")
    print("(These are used only for unattended re-login. Password input is hidden.)\n")

    # --show echoes the password as you type (skips the confirm step too) —
    # for when no one is around and you want to see what you're entering.
    show = "--show" in sys.argv
    ask = input if show else getpass.getpass

    username = input(f"{LABEL} username / email: ").strip()
    if not username:
        print("No username entered — aborting, nothing saved.")
        return
    password = ask(f"{LABEL} password{'' if show else ' (hidden)'}: ")
    if not password:
        print("No password entered — aborting, nothing saved.")
        return
    if not show:
        confirm = getpass.getpass("Re-enter password to confirm: ")
        if password != confirm:
            print("Passwords did not match — aborting, nothing saved.")
            return

    keyring.set_password(SERVICE, USER_KEY, username)
    keyring.set_password(SERVICE, username, password)
    print(f"\nSaved. Username '{username}' + password stored in the vault.")
    print("lease_sender.py will now be able to log itself in unattended.")


if __name__ == "__main__":
    main()
