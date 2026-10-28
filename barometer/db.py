"""SQLite plumbing shared by the Almanac and the Deep store.

Each store is one connection guarded by a re-entrant lock. Barometer has a single
owner and very little concurrency, so this is simpler and safer than a pool.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

ALMANAC_SCHEMA = [
    # v1
    """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        pw_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('owner', 'almanac')),
        created_at TEXT NOT NULL
    );
    CREATE TABLE people (
        id INTEGER PRIMARY KEY,
        display_name TEXT NOT NULL,
        aliases TEXT NOT NULL DEFAULT '[]',
        grp TEXT CHECK (grp IN ('family', 'friends', 'work-adjacent')),
        layer INTEGER CHECK (layer IN (5, 15, 50, 150)),
        last_contact_at TEXT,
        is_self INTEGER NOT NULL DEFAULT 0,
        source TEXT NOT NULL DEFAULT 'extraction',
        source_note_ids TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE family_links (
        id INTEGER PRIMARY KEY,
        from_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        to_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        relation TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'extraction',
        source_note_ids TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'provisional',
        created_at TEXT NOT NULL,
        UNIQUE (from_id, to_id, relation)
    );
    CREATE TABLE events (
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        date TEXT,
        location TEXT,
        type TEXT NOT NULL DEFAULT 'gathering' CHECK (type IN ('gathering', '1:1', 'message thread')),
        source TEXT NOT NULL DEFAULT 'extraction',
        source_note_ids TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL
    );
    CREATE TABLE event_attendees (
        event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        PRIMARY KEY (event_id, person_id)
    );
    CREATE TABLE key_dates (
        id INTEGER PRIMARY KEY,
        person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        label TEXT NOT NULL,
        date TEXT NOT NULL,
        recurring INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'provisional',
        source TEXT NOT NULL DEFAULT 'extraction',
        source_note_ids TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL
    );
    CREATE TABLE follow_ups (
        id INTEGER PRIMARY KEY,
        person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        key_date_id INTEGER REFERENCES key_dates(id) ON DELETE CASCADE,
        due_date TEXT NOT NULL,
        prompt TEXT NOT NULL,
        done_at TEXT,
        dismissed_at TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE gestures (
        id INTEGER PRIMARY KEY,
        person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        date TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'other',
        what TEXT NOT NULL,
        landed TEXT,
        source TEXT NOT NULL DEFAULT 'extraction',
        source_note_ids TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL
    );
    CREATE TABLE nudges (
        id INTEGER PRIMARY KEY,
        week_start TEXT NOT NULL,
        person_id INTEGER REFERENCES people(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        text TEXT NOT NULL,
        done_at TEXT,
        dismissed_at TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE digests (
        week_start TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        pushed_at TEXT
    );
    CREATE TABLE deep_inbox (
        id INTEGER PRIMARY KEY,
        note_id TEXT NOT NULL UNIQUE,
        sealed BLOB NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE contacts (
        id INTEGER PRIMARY KEY,
        person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        date TEXT NOT NULL,
        initiated_by TEXT NOT NULL CHECK (initiated_by IN ('me', 'them', 'mutual')),
        channel TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'extraction',
        source_note_ids TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL
    );
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE INDEX idx_key_dates_person ON key_dates(person_id);
    CREATE INDEX idx_follow_ups_due ON follow_ups(due_date);
    CREATE INDEX idx_gestures_person ON gestures(person_id);
    CREATE INDEX idx_contacts_person ON contacts(person_id, date);
    """,
]

DEEP_SCHEMA = [
    # v1
    """
    CREATE TABLE notes (
        id TEXT PRIMARY KEY,
        seq INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        raw_text TEXT NOT NULL,
        input_type TEXT NOT NULL CHECK (input_type IN ('text', 'voice')),
        event_id INTEGER,
        extraction_status TEXT NOT NULL DEFAULT 'pending',
        summary TEXT,
        readback TEXT,
        last_error TEXT
    );
    CREATE TABLE note_people (
        note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
        person_id INTEGER NOT NULL,
        PRIMARY KEY (note_id, person_id)
    );
    CREATE TABLE extractions (
        id INTEGER PRIMARY KEY,
        note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        raw_json TEXT NOT NULL,
        correction TEXT,
        overrides TEXT NOT NULL DEFAULT '{}',
        resolution TEXT NOT NULL DEFAULT '{}',
        active INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE motives (
        id INTEGER PRIMARY KEY,
        person_id INTEGER NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('want', 'fear')),
        category TEXT NOT NULL,
        detail TEXT NOT NULL,
        confidence TEXT NOT NULL DEFAULT 'med',
        status TEXT NOT NULL DEFAULT 'provisional',
        source_note_ids TEXT NOT NULL DEFAULT '[]',
        last_seen TEXT NOT NULL
    );
    CREATE TABLE person_profiles (
        person_id INTEGER PRIMARY KEY,
        relativism_tolerance TEXT CHECK (relativism_tolerance IN ('low', 'normal', 'high')),
        notes TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL
    );
    CREATE TABLE interactions (
        id INTEGER PRIMARY KEY,
        actor_id INTEGER NOT NULL,
        target_id INTEGER NOT NULL,
        act TEXT NOT NULL,
        score INTEGER NOT NULL CHECK (score BETWEEN -3 AND 3),
        witnessed TEXT NOT NULL CHECK (witnessed IN ('me', 'relayed')),
        relayed_by_id INTEGER,
        topic_id INTEGER,
        date TEXT NOT NULL,
        quote TEXT NOT NULL DEFAULT '',
        interpretation INTEGER NOT NULL DEFAULT 0,
        confidence TEXT NOT NULL DEFAULT 'med',
        status TEXT NOT NULL DEFAULT 'provisional',
        source_note_ids TEXT NOT NULL DEFAULT '[]',
        last_seen TEXT NOT NULL
    );
    CREATE TABLE edges (
        id INTEGER PRIMARY KEY,
        from_id INTEGER NOT NULL,
        to_id INTEGER NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('friendship', 'debt', 'info-flow')),
        direction TEXT NOT NULL DEFAULT 'directed' CHECK (direction IN ('directed', 'mutual')),
        intensity INTEGER NOT NULL DEFAULT 1 CHECK (intensity BETWEEN 1 AND 3),
        detail TEXT NOT NULL DEFAULT '',
        trigger_topic_ids TEXT NOT NULL DEFAULT '[]',
        last_flared TEXT,
        confidence TEXT NOT NULL DEFAULT 'med',
        status TEXT NOT NULL DEFAULT 'provisional',
        source_note_ids TEXT NOT NULL DEFAULT '[]',
        last_seen TEXT NOT NULL
    );
    CREATE TABLE trust (
        id INTEGER PRIMARY KEY,
        truster_id INTEGER NOT NULL,
        trustee_id INTEGER NOT NULL,
        domain TEXT NOT NULL CHECK (domain IN ('secrets', 'money', 'practical help', 'emotional support', 'judgement')),
        level INTEGER NOT NULL CHECK (level BETWEEN -2 AND 2),
        basis TEXT NOT NULL CHECK (basis IN ('stated', 'observed', 'inferred')),
        confidence TEXT NOT NULL DEFAULT 'med',
        status TEXT NOT NULL DEFAULT 'provisional',
        source_note_ids TEXT NOT NULL DEFAULT '[]',
        last_seen TEXT NOT NULL,
        UNIQUE (truster_id, trustee_id, domain)
    );
    CREATE TABLE claims (
        id INTEGER PRIMARY KEY,
        note_id TEXT NOT NULL,
        source_person_id INTEGER,
        about_person_ids TEXT NOT NULL DEFAULT '[]',
        text TEXT NOT NULL,
        credibility INTEGER NOT NULL DEFAULT 6 CHECK (credibility BETWEEN 1 AND 6),
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'confirmed', 'contradicted')),
        resolved_by_note_id TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE topics (
        id INTEGER PRIMARY KEY,
        label TEXT NOT NULL,
        sensitivity INTEGER NOT NULL DEFAULT 1 CHECK (sensitivity BETWEEN 1 AND 3),
        frames TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'provisional',
        source_note_ids TEXT NOT NULL DEFAULT '[]',
        last_seen TEXT NOT NULL
    );
    CREATE TABLE person_topics (
        id INTEGER PRIMARY KEY,
        person_id INTEGER NOT NULL,
        topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
        frame TEXT,
        salience INTEGER NOT NULL DEFAULT 1 CHECK (salience BETWEEN 1 AND 3),
        confidence TEXT NOT NULL DEFAULT 'med',
        status TEXT NOT NULL DEFAULT 'provisional',
        source_note_ids TEXT NOT NULL DEFAULT '[]',
        last_seen TEXT NOT NULL,
        UNIQUE (person_id, topic_id)
    );
    CREATE TABLE self_observations (
        id INTEGER PRIMARY KEY,
        note_id TEXT,
        kind TEXT NOT NULL CHECK (kind IN ('misfire', 'win', 'prediction')),
        description TEXT NOT NULL,
        predicted_outcome TEXT,
        actual_outcome TEXT,
        score REAL,
        event_id INTEGER,
        brief_id INTEGER,
        person_ids TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        scored_at TEXT
    );
    CREATE TABLE assessments (
        id INTEGER PRIMARY KEY,
        person_id INTEGER NOT NULL,
        pattern TEXT NOT NULL,
        supporting_note_ids TEXT NOT NULL DEFAULT '[]',
        counter_note_ids TEXT NOT NULL DEFAULT '[]',
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        your_take TEXT NOT NULL DEFAULT '',
        retired INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE questions (
        id INTEGER PRIMARY KEY,
        note_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        prompt TEXT NOT NULL,
        options TEXT NOT NULL,
        payload TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        answered_at TEXT,
        answer TEXT
    );
    CREATE TABLE briefs (
        id INTEGER PRIMARY KEY,
        mode TEXT NOT NULL,
        created_at TEXT NOT NULL,
        event_id INTEGER,
        person_ids TEXT NOT NULL DEFAULT '[]',
        query TEXT NOT NULL DEFAULT '',
        output TEXT NOT NULL,
        used_assessments INTEGER NOT NULL DEFAULT 0,
        saved INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE mirror_log (
        id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL,
        context TEXT NOT NULL,
        pattern TEXT NOT NULL,
        text TEXT NOT NULL,
        note_id TEXT,
        brief_id INTEGER,
        dismissed_at TEXT,
        rating TEXT CHECK (rating IN ('useful', 'not_useful'))
    );
    CREATE TABLE golden_cases (
        id INTEGER PRIMARY KEY,
        note_id TEXT NOT NULL,
        note_text TEXT NOT NULL,
        correction TEXT NOT NULL,
        expected TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_interactions_pair ON interactions(actor_id, target_id);
    CREATE INDEX idx_note_people_person ON note_people(person_id);
    CREATE INDEX idx_self_obs_kind ON self_observations(kind);
    """,
]


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def today_iso(tz) -> str:
    return datetime.now(tz).date().isoformat()


def jload(text: str | None, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    return json.loads(text)


def jdump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def add_note_id(existing: str | None, note_id: str) -> list[str]:
    ids = jload(existing, [])
    if note_id not in ids:
        ids.append(note_id)
    return ids


def status_for(note_ids: Iterable[str]) -> str:
    return "corroborated" if len(set(note_ids)) >= 2 else "provisional"


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


class Store:
    """A single sqlite connection plus a lock."""

    def __init__(self, conn: sqlite3.Connection):
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # Deleted content is overwritten, so "forget" and undo leave no residue in free pages.
        conn.execute("PRAGMA secure_delete = ON")
        self.conn = conn
        self.lock = threading.RLock()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            try:
                self.conn.execute("BEGIN")
                yield self.conn
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def query(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, tuple(params)).fetchone()

    def scalar(self, sql: str, params: Iterable = (), default: Any = None) -> Any:
        row = self.one(sql, params)
        return default if row is None else row[0]

    def migrate(self, scripts: list[str]) -> None:
        with self.lock:
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            for index, script in enumerate(scripts[version:], start=version + 1):
                self.conn.executescript("BEGIN;" + script + f"; PRAGMA user_version = {index}; COMMIT;")


def connect_file(path: Path, key: str | None = None) -> sqlite3.Connection:
    """Open the Almanac DB. Uses SQLCipher when a key is configured and the module is present."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if key:
        try:
            from sqlcipher3 import dbapi2 as sqlcipher  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on the Pi build
            raise RuntimeError(
                "BAROMETER_ALMANAC_KEY_FILE is set but sqlcipher3 is not installed. "
                "Install it with: pip install 'barometer[sqlcipher]'"
            ) from exc
        if not re.fullmatch(r"[0-9a-fA-F]{64}", key):
            raise RuntimeError("Almanac key file must hold 64 hex characters (a 256-bit key)")
        conn = sqlcipher.connect(str(path), check_same_thread=False, isolation_level=None)
        conn.execute(f"PRAGMA key = \"x'{key}'\"")
        conn.row_factory = sqlcipher.Row
        return conn  # type: ignore[return-value]
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def connect_memory() -> sqlite3.Connection:
    return sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
