"""The Almanac: the shareable tier. People, family links, dates, events, gifts, digest."""

from __future__ import annotations

import re
from typing import Any

from .db import ALMANAC_SCHEMA, Store, connect_file, jdump, jload, utcnow
from .config import Settings


class Almanac(Store):
    def __init__(self, settings: Settings):
        key = None
        if settings.almanac_key_file:
            key = settings.almanac_key_file.read_text().strip()
        super().__init__(connect_file(settings.almanac_path, key))
        self.migrate(ALMANAC_SCHEMA)

    # -- settings ---------------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        return default if row is None else jload(row["value"], default)

    def set_setting(self, key: str, value: Any, conn=None) -> None:
        sql = "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        if conn is not None:
            conn.execute(sql, (key, jdump(value)))
        else:
            with self.tx() as c:
                c.execute(sql, (key, jdump(value)))

    def next_note_id(self) -> tuple[str, int]:
        """Readable, monotonically increasing note IDs (N1, N2...) so citations stay short."""
        with self.tx() as c:
            row = c.execute("SELECT value FROM settings WHERE key = 'note_seq'").fetchone()
            seq = (jload(row["value"], 0) if row else 0) + 1
            self.set_setting("note_seq", seq, conn=c)
        return f"N{seq}", seq

    # -- people -----------------------------------------------------------------
    def self_id(self) -> int:
        row = self.one("SELECT id FROM people WHERE is_self = 1")
        if row:
            return row["id"]
        now = utcnow()
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO people (display_name, aliases, is_self, source, created_at, updated_at)"
                " VALUES ('Me', '[\"me\", \"I\"]', 1, 'system', ?, ?)",
                (now, now),
            )
            return cur.lastrowid

    def people(self, include_self: bool = False) -> list[dict]:
        rows = self.query(
            "SELECT * FROM people" + ("" if include_self else " WHERE is_self = 0") + " ORDER BY display_name"
        )
        return [person_dict(r) for r in rows]

    def person(self, person_id: int) -> dict | None:
        row = self.one("SELECT * FROM people WHERE id = ?", (person_id,))
        return person_dict(row) if row else None

    def names(self) -> dict[int, str]:
        return {r["id"]: r["display_name"] for r in self.query("SELECT id, display_name FROM people")}

    def match_alias(self, text: str) -> list[int]:
        """People whose display name or alias equals ``text`` (case-insensitive)."""
        needle = normalise(text)
        hits = []
        for p in self.people(include_self=True):
            if normalise(p["display_name"]) == needle or needle in {normalise(a) for a in p["aliases"]}:
                hits.append(p["id"])
        return hits


def normalise(name: str) -> str:
    name = name.strip().lower()
    name = re.sub(r"^(my|our)\s+", "", name)
    return re.sub(r"\s+", " ", name)


def person_dict(row) -> dict:
    d = dict(row)
    d["aliases"] = jload(d.get("aliases"), [])
    d["source_note_ids"] = jload(d.get("source_note_ids"), [])
    d["is_self"] = bool(d.get("is_self"))
    return d
