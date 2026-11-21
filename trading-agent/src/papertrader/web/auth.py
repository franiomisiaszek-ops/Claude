"""The web app's login: one long password, kept only as a slow hash, and signed sessions.

* The password is set on the PC with `papertrader web set-password`. Only its scrypt hash
  (with a random salt) is stored, in `<data>/web_auth.json`, readable by the app alone.
* A login gives the browser a session cookie: when it was issued, when it expires, and a
  signature (HMAC-SHA256 with a random secret from the same file). Nothing else is in it.
  Setting a new password makes a new secret, which logs out every device at once.
* Every form that changes something carries a CSRF token tied to the session.
* Wrong passwords are counted: after `lockout_after` in a row, logins are locked for 1
  minute, then 2, 4, ... up to `lockout_max_minutes`. A right password resets the count.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

AUTH_FILE = "web_auth.json"
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1  # about 32 MB and a tenth of a second per guess
COOKIE = "pt_session"


class PasswordError(ValueError):
    pass


@dataclass(frozen=True)
class AuthRecord:
    salt: str
    hash: str
    n: int
    r: int
    p: int
    secret: str
    set_at: str


def auth_path(data_dir: Path) -> Path:
    return data_dir / AUTH_FILE


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=128 * 1024 * 1024, dklen=32
    )


def check_strength(password: str, min_length: int) -> None:
    if len(password) < min_length:
        raise PasswordError(f"The password must be at least {min_length} characters long.")
    if len(set(password)) < 6:
        raise PasswordError("The password repeats too few different characters.")


def set_password(path: Path, password: str, min_length: int, now: datetime) -> AuthRecord:
    """Store a new password's hash and a new session secret (every device is logged out)."""
    check_strength(password, min_length)
    salt = secrets.token_bytes(16)
    record = AuthRecord(
        salt=salt.hex(),
        hash=_scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P).hex(),
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        secret=secrets.token_hex(32),
        set_at=now.isoformat(),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(asdict(record), handle)
    os.replace(temp, path)
    return record


def logout_everywhere(path: Path) -> AuthRecord | None:
    """A new session secret, the same password: every device must log in again."""
    record = load(path)
    if record is None:
        return None
    fresh = AuthRecord(**{**asdict(record), "secret": secrets.token_hex(32)})
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(asdict(fresh), handle)
    os.replace(temp, path)
    return fresh


def load(path: Path) -> AuthRecord | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return AuthRecord(**raw)
    except (OSError, ValueError, TypeError):
        return None


def check_password(record: AuthRecord, password: str) -> bool:
    candidate = _scrypt(password, bytes.fromhex(record.salt), record.n, record.r, record.p)
    return hmac.compare_digest(candidate.hex(), record.hash)


# --------------------------------------------------------------------------- sessions


def _sign(record: AuthRecord, purpose: str, payload: str) -> str:
    key = bytes.fromhex(record.secret)
    return hmac.new(key, f"{purpose}:{payload}".encode(), hashlib.sha256).hexdigest()


def make_session(record: AuthRecord, now: datetime, days: int) -> str:
    issued = int(now.timestamp())
    expires = int((now + timedelta(days=days)).timestamp())
    payload = f"{issued}.{expires}"
    return f"{payload}.{_sign(record, 'session', payload)}"


def valid_session(record: AuthRecord | None, token: str | None, now: datetime) -> bool:
    if record is None or not token:
        return False
    parts = token.split(".")
    if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
        return False
    payload = f"{parts[0]}.{parts[1]}"
    if not hmac.compare_digest(_sign(record, "session", payload), parts[2]):
        return False
    stamp = now.timestamp()
    return int(parts[0]) <= stamp + 60 and stamp < int(parts[1])


def csrf_token(record: AuthRecord, session: str) -> str:
    return _sign(record, "csrf", session)[:40]


def valid_csrf(record: AuthRecord | None, session: str | None, value: str | None) -> bool:
    if record is None or not session or not value:
        return False
    return hmac.compare_digest(csrf_token(record, session), value)


# --------------------------------------------------------------------------- lockout


class Lockout:
    """Counts wrong passwords in a row and locks logins for a growing time."""

    def __init__(self, after: int, max_minutes: int) -> None:
        self.after = after
        self.max_minutes = max_minutes
        self.failures = 0
        self.locked_until: datetime | None = None

    def seconds_left(self, now: datetime) -> int:
        if self.locked_until is None or now >= self.locked_until:
            return 0
        return int((self.locked_until - now).total_seconds()) + 1

    def failed(self, now: datetime) -> int:
        """Count a wrong password; returns the lock in seconds (0 if not locked)."""
        self.failures += 1
        if self.failures < self.after:
            return 0
        minutes = min(self.max_minutes, int(2 ** (self.failures - self.after)))
        self.locked_until = now + timedelta(minutes=minutes)
        return minutes * 60

    def succeeded(self) -> None:
        self.failures = 0
        self.locked_until = None
