"""Logins and sessions. Server-side sessions, idle logout, no "remember me"."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .almanac import Almanac
from .crypto import hash_password, random_token, verify_password
from .db import utcnow


@dataclass
class Session:
    token: str
    user_id: int
    username: str
    role: str
    last_seen: float = field(default_factory=time.monotonic)
    deep: bool = False  # this session entered the Deep passphrase
    captured: set = field(default_factory=set)  # notes this session wrote; it may see their readbacks

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"


class Throttle:
    """Exponential back-off after repeated failures, per key (username or 'deep')."""

    def __init__(self, free_attempts: int = 5, max_wait: float = 900.0):
        self.free = free_attempts
        self.max_wait = max_wait
        self._fails: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def wait_for(self, key: str) -> float:
        with self._lock:
            count, last = self._fails.get(key, (0, 0.0))
        if count < self.free:
            return 0.0
        wait = min(self.max_wait, 2 ** (count - self.free + 1))
        return max(0.0, last + wait - time.monotonic())

    def failed(self, key: str) -> None:
        with self._lock:
            count, _ = self._fails.get(key, (0, 0.0))
            self._fails[key] = (count + 1, time.monotonic())

    def succeeded(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)


class Sessions:
    def __init__(self, idle_seconds: int):
        self.idle = idle_seconds
        self._by_token: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, user) -> Session:
        s = Session(random_token(), user["id"], user["username"], user["role"])
        with self._lock:
            self._by_token[s.token] = s
        return s

    def get(self, token: str | None, touch: bool = True) -> Session | None:
        if not token:
            return None
        with self._lock:
            s = self._by_token.get(token)
            if s is None:
                return None
            if time.monotonic() - s.last_seen > self.idle:
                del self._by_token[token]
                return None
            if touch:
                s.last_seen = time.monotonic()
            return s

    def destroy(self, token: str | None) -> None:
        with self._lock:
            self._by_token.pop(token or "", None)

    def expire(self) -> list[Session]:
        now = time.monotonic()
        with self._lock:
            dead = [s for s in self._by_token.values() if now - s.last_seen > self.idle]
            for s in dead:
                del self._by_token[s.token]
        return dead

    def clear_deep(self) -> None:
        with self._lock:
            for s in self._by_token.values():
                s.deep = False

    def any_deep(self) -> bool:
        with self._lock:
            return any(s.deep for s in self._by_token.values())


def add_user(almanac: Almanac, username: str, password: str, role: str) -> int:
    if role not in ("owner", "almanac"):
        raise ValueError("role must be owner or almanac")
    if len(password) < 10:
        raise ValueError("Use a login passphrase of at least 10 characters")
    with almanac.tx() as c:
        cur = c.execute(
            "INSERT INTO users (username, pw_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (username.strip(), hash_password(password), role, utcnow()),
        )
        return cur.lastrowid


def set_password(almanac: Almanac, username: str, password: str) -> None:
    if len(password) < 10:
        raise ValueError("Use a login passphrase of at least 10 characters")
    with almanac.tx() as c:
        c.execute("UPDATE users SET pw_hash = ? WHERE username = ?", (hash_password(password), username))


def check_login(almanac: Almanac, username: str, password: str):
    user = almanac.one("SELECT * FROM users WHERE username = ?", (username.strip(),))
    if user is None:
        # Spend the same time as a real check so usernames can't be probed by timing.
        verify_password(_DUMMY_HASH, password)
        return None
    return user if verify_password(user["pw_hash"], password) else None


_DUMMY_HASH = hash_password("not-a-real-password")
