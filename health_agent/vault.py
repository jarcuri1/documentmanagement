"""
Vault — your health record, encrypted at rest
=============================================
The whole SQLite database lives on disk as ONE encrypted file
(health.db.enc, AES-256-GCM). It is decrypted into memory only while a
command runs, and re-encrypted before it is written back. A copied file, a
backup, a Dropbox sync or a stolen drive holds nothing readable without the key.

THE KEY
  A random 256-bit key, created once by `python health.py init-key`.
  - Stored in the OS credential vault via `keyring`. On Windows that is
    Credential Manager, protected by DPAPI and tied to your Windows login,
    so the ingest server and the morning brief can run unattended.
  - Shown to you ONCE as a recovery key. Write it on paper or put it in your
    password manager. Without it, a new PC or a reinstalled Windows means
    the record is gone for good. There is no back door, by design.
  - HEALTH_KEY=<recovery key> in the environment overrides the keyring.
    That's for restoring on a new machine and for tests. Don't put it in a
    shared .env file.

Writes are atomic (temp file + rename). The previous version is kept as
health.db.enc.bak, also encrypted, so a crash mid-write never loses the
record. A lock file stops the ingest server and the CLI from overwriting
each other.
"""

import base64
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

MAGIC = b"HEALTHVAULT1\n"
KEYRING_SERVICE = "health-agent"
KEYRING_USER = "db-key"


class VaultError(RuntimeError):
    pass


# ----------------------------------------------------------------------
# Key handling
# ----------------------------------------------------------------------
def format_recovery_key(key: bytes) -> str:
    s = base64.b32encode(key).decode().rstrip("=")
    return "-".join(s[i:i + 4] for i in range(0, len(s), 4))


def parse_recovery_key(text: str) -> bytes:
    s = "".join(ch for ch in text.upper() if ch.isalnum())
    try:
        key = base64.b32decode(s + "=" * (-len(s) % 8))
    except Exception:
        raise VaultError("that doesn't look like a recovery key") from None
    if len(key) != 32:
        raise VaultError("recovery key has the wrong length")
    return key


def _keyring():
    try:
        import keyring
        return keyring
    except ImportError:
        return None


def load_key() -> bytes:
    env = os.environ.get("HEALTH_KEY")
    if env:
        return parse_recovery_key(env)
    kr = _keyring()
    try:
        stored = kr.get_password(KEYRING_SERVICE, KEYRING_USER) if kr else None
    except Exception as e:  # e.g. NoKeyringError on a headless Linux box
        raise VaultError(f"this machine has no usable credential vault ({type(e).__name__}). "
                         "Set HEALTH_KEY to your recovery key instead.") from None
    if not stored:
        raise VaultError("no encryption key on this machine. Run `python health.py init-key` "
                         "(first time) or `python health.py restore-key` (new machine).")
    return parse_recovery_key(stored)


def save_key(key: bytes):
    kr = _keyring()
    if not kr:
        raise VaultError("pip install keyring  (needed to keep the key in the OS vault)")
    try:
        kr.set_password(KEYRING_SERVICE, KEYRING_USER, format_recovery_key(key))
    except Exception as e:
        raise VaultError(f"could not store the key in this machine's credential vault "
                         f"({type(e).__name__}: {e})") from None


def new_key() -> bytes:
    return secrets.token_bytes(32)


# ----------------------------------------------------------------------
# Encrypt / decrypt
# ----------------------------------------------------------------------
def encrypt(key: bytes, plaintext: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = secrets.token_bytes(12)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, plaintext, MAGIC)


def decrypt(key: bytes, blob: bytes) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not blob.startswith(MAGIC):
        raise VaultError("not a health vault file")
    nonce = blob[len(MAGIC):len(MAGIC) + 12]
    try:
        return AESGCM(key).decrypt(nonce, blob[len(MAGIC) + 12:], MAGIC)
    except InvalidTag:
        raise VaultError("wrong key, or the file was tampered with") from None


# ----------------------------------------------------------------------
# Lock (cross-platform, no extra deps)
# ----------------------------------------------------------------------
@contextmanager
def _locked(path: Path, timeout=60, stale_after=300):
    lock = path.with_name(path.name + ".lock")
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > stale_after:
                    lock.unlink(missing_ok=True)  # a crashed writer left it
                    continue
            except FileNotFoundError:
                continue
            if time.time() > deadline:
                raise VaultError(f"health record is locked by another process ({lock})")
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(fd)
        lock.unlink(missing_ok=True)


# ----------------------------------------------------------------------
# The encrypted database
# ----------------------------------------------------------------------
class EncryptedDB:
    def __init__(self, path: Path, key: bytes = None):
        self.path = Path(path)
        self.key = key or load_key()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self):
        """Decrypt into memory, yield a connection, re-encrypt if anything changed."""
        with _locked(self.path):
            db = sqlite3.connect(":memory:")
            db.row_factory = sqlite3.Row
            if self.path.exists():
                db.deserialize(decrypt(self.key, self.path.read_bytes()))
            try:
                yield db
                if db.total_changes:
                    db.commit()
                    self._write(db.serialize())
            finally:
                db.close()

    def _write(self, data: bytes):
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_bytes(encrypt(self.key, data))
        if self.path.exists():
            os.replace(self.path, self.path.with_name(self.path.name + ".bak"))
        os.replace(tmp, self.path)


def import_plaintext(enc_db: EncryptedDB, plaintext_path: Path):
    """Move an old unencrypted health.db into the vault (one-time migration)."""
    src = sqlite3.connect(plaintext_path)
    try:
        mem = sqlite3.connect(":memory:")
        src.backup(mem)
        data = mem.serialize()
        mem.close()
    finally:
        src.close()
    with _locked(enc_db.path):
        enc_db._write(data)
