"""Forget a person in one action: rows deleted, their name redacted wherever it was written."""

from __future__ import annotations

import re

from .almanac import Almanac
from .commit import Resolution
from .db import jdump, jload
from .deep import DeepVault
from .pseudo import name_variants, variant_regex

REDACTED = "[forgotten]"


def _patterns(person: dict) -> list[re.Pattern]:
    pats = [variant_regex(v) for v in name_variants(person)]
    # Also catch the name in any case ("SARAH", "sarah") as long as it's a real name, not a short word.
    for v in name_variants(person):
        if len(v) >= 4:
            pats.append(re.compile(rf"\b{re.escape(v)}\b", re.I))
    return pats


def _redact(text: str, pats: list[re.Pattern], ref_re: re.Pattern) -> str:
    for p in pats:
        text = p.sub(REDACTED, text)
    return ref_re.sub("P0", text)


def _redact_db(conn, pats: list[re.Pattern], ref_re: re.Pattern, skip: set[str] = frozenset()) -> int:
    changed = 0
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    for table in tables:
        if table in skip:
            continue
        cols = [c[1] for c in conn.execute(f"PRAGMA table_info({table})") if (c[2] or "").upper() == "TEXT"]
        if not cols:
            continue
        for row in conn.execute(f"SELECT rowid AS _rid, {', '.join(cols)} FROM {table}").fetchall():
            updates = {}
            for col in cols:
                val = row[col]
                if isinstance(val, str) and val:
                    new = _redact(val, pats, ref_re)
                    if new != val:
                        updates[col] = new
            if updates:
                sets = ", ".join(f"{c} = ?" for c in updates)
                conn.execute(f"UPDATE {table} SET {sets} WHERE rowid = ?", (*updates.values(), row["_rid"]))
                changed += 1
    return changed


def forget_person(almanac: Almanac, vault: DeepVault, pid: int) -> dict:
    """Needs the Deep store open (and its inbox drained) so note text can be redacted."""
    person = almanac.person(pid)
    if person is None or person["is_self"]:
        raise ValueError("No such person")
    vault.store  # raises DeepLocked if Private is closed
    if almanac.scalar("SELECT count(*) FROM deep_inbox", (), 0):
        raise RuntimeError("Sealed notes are still waiting to be filed; try again in a moment")
    pats = _patterns(person)
    ref_re = re.compile(rf"\bP{pid}\b")

    with vault.tx() as c:
        # Keep re-commits of older notes from resurrecting the person.
        for e in c.execute("SELECT id, raw_json, resolution FROM extractions").fetchall():
            res = Resolution.from_dict(jload(e["resolution"], {}))
            if pid not in res.refs.values():
                continue
            from .pipeline import _prune_refs
            from .schemas import Extraction

            drop = {k for k, v in res.refs.items() if v == pid}
            res.refs = {k: v for k, v in res.refs.items() if k not in drop}
            res.created = [x for x in res.created if x != pid]
            ext = _prune_refs(Extraction.model_validate_json(e["raw_json"]), set(res.refs))
            c.execute("UPDATE extractions SET raw_json = ?, resolution = ? WHERE id = ?",
                      (ext.model_dump_json(), jdump(res.as_dict()), e["id"]))
        c.execute("DELETE FROM motives WHERE person_id = ?", (pid,))
        c.execute("DELETE FROM person_profiles WHERE person_id = ?", (pid,))
        c.execute("DELETE FROM interactions WHERE actor_id = ? OR target_id = ?", (pid, pid))
        c.execute("UPDATE interactions SET relayed_by_id = NULL WHERE relayed_by_id = ?", (pid,))
        c.execute("DELETE FROM edges WHERE from_id = ? OR to_id = ?", (pid, pid))
        c.execute("DELETE FROM trust WHERE truster_id = ? OR trustee_id = ?", (pid, pid))
        c.execute("DELETE FROM claims WHERE source_person_id = ?", (pid,))
        for r in c.execute("SELECT id, about_person_ids FROM claims").fetchall():
            about = jload(r["about_person_ids"], [])
            if pid in about:
                c.execute("UPDATE claims SET about_person_ids = ? WHERE id = ?", (jdump([a for a in about if a != pid]), r["id"]))
        c.execute("DELETE FROM person_topics WHERE person_id = ?", (pid,))
        c.execute("DELETE FROM assessments WHERE person_id = ?", (pid,))
        c.execute("DELETE FROM note_people WHERE person_id = ?", (pid,))
        for table in ("self_observations", "briefs"):
            for r in c.execute(f"SELECT id, person_ids FROM {table}").fetchall():
                ids = jload(r["person_ids"], [])
                if pid in ids:
                    c.execute(f"UPDATE {table} SET person_ids = ? WHERE id = ?", (jdump([x for x in ids if x != pid]), r["id"]))
        c.execute("DELETE FROM questions WHERE answered_at IS NULL AND payload LIKE ?", (f"%{person['display_name']}%",))
        deep_rows = _redact_db(c, pats, ref_re)

    with almanac.tx() as c:
        c.execute("DELETE FROM people WHERE id = ?", (pid,))  # cascades dates, follow-ups, gifts, contacts, links
        c.execute("DELETE FROM nudges WHERE person_id = ?", (pid,))
        for r in c.execute("SELECT id, aliases FROM people").fetchall():
            aliases = jload(r["aliases"], [])
            kept = [a for a in aliases if not any(p.search(a) for p in pats)]
            if kept != aliases:
                c.execute("UPDATE people SET aliases = ? WHERE id = ?", (jdump(kept), r["id"]))
        almanac_rows = _redact_db(c, pats, ref_re, skip={"users", "settings", "deep_inbox"})
    with almanac.lock:
        almanac.conn.execute("VACUUM")
        almanac.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    vault.vacuum()
    return {"deep_rows_redacted": deep_rows, "almanac_rows_redacted": almanac_rows}
