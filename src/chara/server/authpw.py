"""Optional password login for a PUBLIC (non-loopback) bind.

This is an ALTERNATIVE auth path that sits ON TOP of the existing token gate
(``netsec`` cookie/query). It exists for one ergonomic case: an operator who
bookmarks ``https://host/`` behind a reverse proxy and would rather type a
password than carry the long ``#token=…`` URL. It is INERT for a loopback /
Electron / SSH-tunnel bind — the local app never sees a login screen.

Ported in shape from AstrBot (``core/utils/auth_password.py`` for the PBKDF2
store and ``dashboard/server.py`` for the per-IP token-bucket rate limit), kept
stdlib-only (``hashlib.pbkdf2_hmac``) and confined here so it can be unit
tested without the HTTP server.

Storage: ``~/.chara/auth.json`` holds ONLY ``{algo, iters, salt, hash}`` —
never the plaintext. The verify is constant-time (``hmac.compare_digest``).
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import string
import threading
import time
from pathlib import Path

from ..session import sessions as S

PBKDF2_ITERATIONS = 600_000
_PBKDF2_SALT_BYTES = 16
_GENERATED_PASSWORD_LENGTH = 24
_ALGO = "pbkdf2_sha256"

# Passwords this short / empty are refused outright — a "default" password is
# never accepted (the plan forbids it). The generated password is always 24.
_MIN_LENGTH = 8


def auth_store_path() -> Path:
    """``~/.chara/auth.json`` — the PBKDF2 password record (hash+salt only)."""
    return S.chara_home() / "auth.json"


def generate_password() -> str:
    """A strong random 24-char password (letters + digits), like the token."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(_GENERATED_PASSWORD_LENGTH))


def hash_password(raw: str, *, iterations: int = PBKDF2_ITERATIONS) -> dict[str, object]:
    """PBKDF2-HMAC-SHA256 record for *raw*. Refuses an empty/too-short password."""
    if not isinstance(raw, str) or len(raw) < _MIN_LENGTH:
        raise ValueError("password must be at least 8 characters")
    salt = secrets.token_hex(_PBKDF2_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", raw.encode("utf-8"), bytes.fromhex(salt), iterations
    ).hex()
    return {"algo": _ALGO, "iters": int(iterations), "salt": salt, "hash": digest}


def verify_password(record: dict[str, object] | None, candidate: str) -> bool:
    """Constant-time check that *candidate* matches a stored PBKDF2 record."""
    if not isinstance(record, dict) or not isinstance(candidate, str) or candidate == "":
        return False
    if record.get("algo") != _ALGO:
        return False
    try:
        iterations = int(record["iters"])  # type: ignore[arg-type]
        salt = bytes.fromhex(str(record["salt"]))
        stored = bytes.fromhex(str(record["hash"]))
    except (KeyError, TypeError, ValueError):
        return False
    candidate_key = hashlib.pbkdf2_hmac("sha256", candidate.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(stored, candidate_key)


def load_record(path: Path | None = None) -> dict[str, object] | None:
    """Read the stored password record, or None if absent/corrupt."""
    p = path or auth_store_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("algo") == _ALGO else None


def save_record(record: dict[str, object], path: Path | None = None) -> Path:
    """Persist a PBKDF2 record (hash+salt+iters only, never plaintext).

    The tmp file is created 0o600 BEFORE any bytes are written, so the record is
    never group/world-readable for even a moment (no write-then-chmod race)."""
    p = path or auth_store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    data = json.dumps(record, ensure_ascii=False).encode("utf-8")
    # os.open with 0o600 means the file is private from creation (umask can only
    # remove bits, and O_CREAT honors the mode for a new file).
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)  # in case the tmp pre-existed with looser perms
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, p)
    with contextlib.suppress(OSError):
        p.chmod(0o600)  # belt-and-suspenders if the tmp pre-existed with looser perms
    return p


def ensure_password(
    *, env_password: str | None, path: Path | None = None
) -> tuple[bool, str | None]:
    """Resolve the password for a PUBLIC bind, persisting its hash.

    Precedence:
      1. ``env_password`` (``CHARA_PASSWORD``) — re-hashed + stored every run
         (lets an operator rotate it; refused if too short/empty).
      2. An existing ``auth.json`` record — kept as-is.
      3. Otherwise generate a fresh 24-char password, store its hash, and return
         it so the caller can print it ONCE.

    Returns ``(login_enabled, generated_plaintext_or_None)``. ``generated`` is
    non-None ONLY when we just created a password (so the caller logs it once);
    an env-supplied or pre-existing password returns ``(True, None)``.
    """
    p = path or auth_store_path()
    env = (env_password or "").strip()
    if env:
        if len(env) < _MIN_LENGTH:
            raise ValueError(
                "CHARA_PASSWORD must be at least 8 characters (refusing an empty/weak password)"
            )
        save_record(hash_password(env), p)
        return True, None
    existing = load_record(p)
    if existing is not None:
        return True, None
    fresh = generate_password()
    save_record(hash_password(fresh), p)
    return True, fresh


# ---- per-client-IP rate limit (token bucket, AstrBot server.py shape) --------

class _Bucket:
    __slots__ = ("tokens", "last")

    def __init__(self, capacity: float) -> None:
        self.tokens = capacity
        self.last = time.monotonic()


class LoginRateLimiter:
    """Per-IP token-bucket throttle for ``POST /login``.

    ``capacity`` failed attempts allowed in a burst; refilled at
    ``capacity / window`` tokens/sec. Idle buckets are evicted after an hour so
    the map can't grow without bound. Thread-safe (the HTTP handler runs on a
    pool of threads)."""

    _ENTRY_TTL = 3600.0

    def __init__(self, capacity: int = 5, window: float = 60.0) -> None:
        self.capacity = float(capacity)
        self.refill = self.capacity / float(window)
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()
        self._last_evict = time.monotonic()

    def allow(self, key: str) -> bool:
        """Consume one token for *key*; False when the bucket is empty (throttled)."""
        now = time.monotonic()
        with self._lock:
            self._evict(now)
            b = self._buckets.get(key)
            if b is None:
                b = _Bucket(self.capacity)
                self._buckets[key] = b
            b.tokens = min(self.capacity, b.tokens + (now - b.last) * self.refill)
            b.last = now
            if b.tokens >= 1.0:
                b.tokens -= 1.0
                return True
            return False

    def _evict(self, now: float) -> None:
        if now - self._last_evict < self._ENTRY_TTL:
            return
        self._last_evict = now
        cutoff = now - self._ENTRY_TTL
        for k in [k for k, v in self._buckets.items() if v.last < cutoff]:
            del self._buckets[k]
