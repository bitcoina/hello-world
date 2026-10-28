"""Writing an extraction into the stores, undoing it, and describing it in one paragraph.

Every derived row carries ``source_note_ids``. Undoing a note removes that note's
support from each row; a row left with no support is deleted, and status is recomputed
(provisional with one note, corroborated with two or more). That one rule gives undo,
correction and "forget" the same semantics.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from .almanac import Almanac, normalise
from .db import add_note_id, jdump, jload, parse_date, status_for, utcnow
from .schemas import Extraction

KIN_RELATIONS = {
    "mother", "father", "mum", "dad", "parent", "son", "daughter", "child", "sister", "brother", "sibling",
    "wife", "husband", "partner", "spouse", "grandmother", "grandfather", "grandparent", "gran", "grandma",
    "grandad", "grandpa", "grandson", "granddaughter", "grandchild", "aunt", "uncle", "niece", "nephew",
    "cousin", "mother-in-law", "father-in-law", "sister-in-law", "brother-in-law", "son-in-law",
    "daughter-in-law", "stepmother", "stepfather", "stepson", "stepdaughter", "stepsister", "stepbrother",
    "half-sister", "half-brother", "fiancee", "fiance", "fiancée", "fiancé",
}

FOLLOW_UP_RULES = {
    # kind: (days after the date, prompt template)
    "hospital": (2, "Ask {name} how the {label} went"),
    "result": (0, "Ask {name} about the {label}"),
    "exam": (1, "Ask {name} how the {label} went"),
    "interview": (1, "Ask {name} how the {label} went"),
    "house move": (7, "Ask {name} how the {label} is going"),
    "new job": (7, "Ask {name} how the {label} is going"),
    "bereavement": (3, "Check in with {name}"),
    "other": (1, "Ask {name} how the {label} went"),
}

ACT_PHRASES = {
    "praised": "praised",
    "helped": "helped",
    "sided with": "sided with",
    "confided in": "confided in",
    "criticised": "criticised",
    "excluded": "excluded",
    "undermined": "undermined",
    "broke contact": "broke contact with",
    "reconciled": "reconciled with",
}


@dataclass
class Resolution:
    refs: dict[str, int] = field(default_factory=dict)
    created: list[int] = field(default_factory=list)
    question: dict | None = None

    def pid(self, ref: str) -> int:
        return self.refs[ref]

    def as_dict(self) -> dict:
        return {"refs": self.refs, "created": self.created, "question": self.question}

    @classmethod
    def from_dict(cls, d: dict) -> "Resolution":
        return cls(dict(d.get("refs", {})), list(d.get("created", [])), d.get("question"))


@dataclass
class Outcome:
    """What a commit did, for the readback."""

    statuses: dict[str, str] = field(default_factory=dict)
    new_topics: set[str] = field(default_factory=set)
    follow_ups: dict[int, str] = field(default_factory=dict)  # key_date index -> due date
    event_title: str | None = None
    question: dict | None = None


def _display_from_phrase(name: str) -> str:
    bare = re.sub(r"^(my|our|the)\s+", "", name.strip(), flags=re.I)
    return bare[:1].upper() + bare[1:] if bare else name


REF_KEYS = {
    "ref", "candidates", "actor", "target", "relayed_by", "from_ref", "to_ref", "truster", "trustee",
    "source", "about", "relates_to", "person", "attendees", "existing",
}


def humanise(ext: Extraction, res: "Resolution", names: dict[int, str], me: str = "me") -> Extraction:
    """A copy of ``ext`` whose free text uses names instead of refs (refs fields untouched).

    The model saw a pseudonymised note, so its quotes and descriptions say "P3" and "X1".
    Stored text and the readback should say "Jean".
    """

    def name_for(ref: str) -> str:
        if ref.startswith("X"):
            return names.get(res.refs.get(ref, -1), ref)
        pid = int(ref[1:])
        return names.get(pid, ref)

    def dec(text: str) -> str:
        text = re.sub(r"\b([PX]\d+)\b", lambda m: name_for(m.group(1)), text)
        return re.sub(r"\bME\b", me, text)

    def walk(value, key=None):
        if isinstance(value, dict):
            return {k: walk(v, k) for k, v in value.items()}
        if isinstance(value, list):
            if key in REF_KEYS or (key == "people" and all(isinstance(v, str) for v in value)):
                return value
            return [walk(v, key) for v in value]
        if isinstance(value, str) and key not in REF_KEYS:
            return dec(value)
        return value

    return Extraction.model_validate(walk(ext.model_dump()))


# ---------------------------------------------------------------------------
# Almanac part (runs even while Deep is locked)
# ---------------------------------------------------------------------------
def resolve_people(c, almanac: Almanac, ext: Extraction, note_id: str, overrides: dict, now: str) -> Resolution:
    res = Resolution()
    self_id = almanac.self_id()
    res.refs["ME"] = self_id
    people = {r["id"]: dict(r) for r in c.execute("SELECT * FROM people WHERE is_self = 0").fetchall()}

    def kin_of_me(relation: str) -> list[int]:
        rows = c.execute(
            "SELECT from_id FROM family_links WHERE to_id = ? AND lower(relation) = ?", (self_id, relation.lower())
        ).fetchall()
        return [r["from_id"] for r in rows]

    def create(m) -> int:
        display = _display_from_phrase(m.name)
        aliases = [m.name] if m.name.strip() != display else []
        grp = m.group if m.group != "unknown" else ("family" if m.relation_to_author.lower() in KIN_RELATIONS else None)
        cur = c.execute(
            "INSERT INTO people (display_name, aliases, grp, source, source_note_ids, created_at, updated_at)"
            " VALUES (?, ?, ?, 'extraction', ?, ?, ?)",
            (display, jdump(aliases), grp, jdump([note_id]), now, now),
        )
        pid = cur.lastrowid
        people[pid] = {"id": pid, "display_name": display, "aliases": jdump(aliases)}
        res.created.append(pid)
        return pid

    def ask(kind: str, m, options: list[int], guess: int, allow_new: bool = True) -> None:
        if res.question is not None:
            return
        opts = [{"value": str(o), "label": people.get(o, {}).get("display_name", f"#{o}")} for o in options]
        if allow_new:
            opts.append({"value": "new", "label": "Someone new"})
        res.question = {
            "kind": kind,
            "ref": m.ref,
            "prompt": f"Who is “{m.name}” in this note?",
            "options": opts,
            "guess": str(guess),
        }

    for m in ext.people:
        override = overrides.get(m.ref)
        if override == "new":
            pid = create(m)
        elif override is not None:
            pid = int(override)
        elif m.ref.startswith("P"):
            pid = int(m.ref[1:])
            others = [int(x[1:]) for x in m.candidates if x != m.ref and x.startswith("P")]
            if others:
                ask("ambiguous_person", m, [pid, *others], pid, allow_new=False)
        else:
            hits = [h for h in almanac.match_alias(m.name) if h != self_id]
            if not hits and m.relation_to_author:
                hits = kin_of_me(m.relation_to_author)
            if len(hits) == 1:
                pid = hits[0]
            elif len(hits) > 1:
                pid = hits[0]
                ask("ambiguous_person", m, hits, pid)
            else:
                pid = create(m)
                names = {normalise(p["display_name"]): i for i, p in people.items() if i != pid}
                close = difflib.get_close_matches(normalise(m.name), list(names), n=2, cutoff=0.82)
                if close:
                    ask("possible_duplicate", m, [names[n] for n in close], pid)
                    res.question["prompt"] = f"Is “{m.name}” someone you've already logged?"
                    res.question["options"][-1]["label"] = f"No, {_display_from_phrase(m.name)} is new"
                    res.question["guess"] = "new"
        res.refs[m.ref] = pid
        if pid not in res.created:
            row = c.execute("SELECT source_note_ids FROM people WHERE id = ?", (pid,)).fetchone()
            if row is not None:
                c.execute(
                    "UPDATE people SET source_note_ids = ?, updated_at = ? WHERE id = ?",
                    (jdump(add_note_id(row["source_note_ids"], note_id)), now, pid),
                )
        if m.relation_to_author and m.relation_to_author.lower() in KIN_RELATIONS:
            _family_link(c, pid, self_id, m.relation_to_author.lower(), note_id, now)
    return res


def _family_link(c, a: int, b: int, relation: str, note_id: str, now: str) -> None:
    row = c.execute(
        "SELECT id, source_note_ids FROM family_links WHERE from_id = ? AND to_id = ? AND relation = ?",
        (a, b, relation),
    ).fetchone()
    if row:
        ids = add_note_id(row["source_note_ids"], note_id)
        c.execute(
            "UPDATE family_links SET source_note_ids = ?, status = ? WHERE id = ?",
            (jdump(ids), status_for(ids), row["id"]),
        )
    else:
        c.execute(
            "INSERT INTO family_links (from_id, to_id, relation, source_note_ids, status, created_at)"
            " VALUES (?, ?, ?, ?, 'provisional', ?)",
            (a, b, relation, jdump([note_id]), now),
        )


def apply_almanac(
    almanac: Almanac, ext: Extraction, note_id: str, note_day: date, overrides: dict, tagged_event: int | None
) -> tuple[Resolution, Outcome, int | None]:
    """Write the shareable facts. Returns the resolution, outcome and linked event id."""
    now = utcnow()
    out = Outcome()
    with almanac.tx() as c:
        res = resolve_people(c, almanac, ext, note_id, overrides, now)
        names = {r["id"]: r["display_name"] for r in c.execute("SELECT id, display_name FROM people")}
        ext = humanise(ext, res, names)
        P = res.pid

        for e in ext.edges:
            if e.kind == "kin":
                _family_link(c, P(e.from_ref), P(e.to_ref), e.relation.lower(), note_id, now)

        for i, k in enumerate(ext.key_dates):
            pid = P(k.person)
            recurring = 1 if (k.recurring or k.kind in ("birthday", "anniversary", "bereavement")) else 0
            existing = None
            for row in c.execute("SELECT * FROM key_dates WHERE person_id = ? AND kind = ?", (pid, k.kind)):
                same = row["date"] == k.date or (recurring and row["recurring"] and row["date"][5:] == k.date[5:])
                if same:
                    existing = row
                    break
            if existing:
                ids = add_note_id(existing["source_note_ids"], note_id)
                status = "corroborated" if existing["source"] == "form" else status_for(ids)
                c.execute(
                    "UPDATE key_dates SET source_note_ids = ?, status = ? WHERE id = ?",
                    (jdump(ids), status, existing["id"]),
                )
                out.statuses[f"key_date:{i}"] = status
                continue
            cur = c.execute(
                "INSERT INTO key_dates (person_id, kind, label, date, recurring, status, source, source_note_ids, created_at)"
                " VALUES (?, ?, ?, ?, ?, 'provisional', 'extraction', ?, ?)",
                (pid, k.kind, k.label, k.date, recurring, jdump([note_id]), now),
            )
            out.statuses[f"key_date:{i}"] = "provisional"
            rule = FOLLOW_UP_RULES.get(k.kind)
            when = parse_date(k.date)
            if rule and when:
                due = when + timedelta(days=rule[0])
                if due >= note_day - timedelta(days=1):
                    prompt = rule[1].format(name=names.get(pid, "them"), label=k.label)
                    c.execute(
                        "INSERT INTO follow_ups (person_id, key_date_id, due_date, prompt, created_at) VALUES (?, ?, ?, ?, ?)",
                        (pid, cur.lastrowid, due.isoformat(), prompt, now),
                    )
                    out.follow_ups[i] = due.isoformat()

        for g in ext.gestures:
            when = g.date or note_day.isoformat()
            row = c.execute(
                "SELECT id, source_note_ids FROM gestures WHERE person_id = ? AND date = ? AND lower(what) = lower(?)",
                (P(g.person), when, g.what),
            ).fetchone()
            if row:
                c.execute(
                    "UPDATE gestures SET source_note_ids = ? WHERE id = ?",
                    (jdump(add_note_id(row["source_note_ids"], note_id)), row["id"]),
                )
            else:
                c.execute(
                    "INSERT INTO gestures (person_id, date, kind, what, landed, source, source_note_ids, created_at)"
                    " VALUES (?, ?, ?, ?, ?, 'extraction', ?, ?)",
                    (P(g.person), when, g.kind, g.what, g.landed or None, jdump([note_id]), now),
                )

        for ct in ext.contacts:
            if ct.person == "ME":
                continue
            when = ct.date or note_day.isoformat()
            row = c.execute(
                "SELECT id, source_note_ids FROM contacts WHERE person_id = ? AND date = ?", (P(ct.person), when)
            ).fetchone()
            if row:
                c.execute(
                    "UPDATE contacts SET source_note_ids = ? WHERE id = ?",
                    (jdump(add_note_id(row["source_note_ids"], note_id)), row["id"]),
                )
            else:
                c.execute(
                    "INSERT INTO contacts (person_id, date, initiated_by, channel, source, source_note_ids, created_at)"
                    " VALUES (?, ?, ?, ?, 'extraction', ?, ?)",
                    (P(ct.person), when, ct.initiated_by, ct.channel, jdump([note_id]), now),
                )

        event_id = tagged_event
        for ev in ext.events[:1]:
            if ev.ref.startswith("E"):
                event_id = int(ev.ref[1:])
            elif event_id is None:
                row = c.execute(
                    "SELECT id FROM events WHERE lower(title) = lower(?) AND coalesce(date, '') = ?",
                    (ev.title, ev.date),
                ).fetchone()
                if row:
                    event_id = row["id"]
                else:
                    cur = c.execute(
                        "INSERT INTO events (title, date, location, type, source, source_note_ids, created_at)"
                        " VALUES (?, ?, ?, ?, 'extraction', ?, ?)",
                        (ev.title, ev.date or None, ev.location or None, ev.type, jdump([note_id]), now),
                    )
                    event_id = cur.lastrowid
            if event_id is not None:
                row = c.execute("SELECT title, source_note_ids FROM events WHERE id = ?", (event_id,)).fetchone()
                if row is not None:
                    out.event_title = row["title"]
                    c.execute(
                        "UPDATE events SET source_note_ids = ? WHERE id = ?",
                        (jdump(add_note_id(row["source_note_ids"], note_id)), event_id),
                    )
                    for a in ev.attendees:
                        c.execute(
                            "INSERT OR IGNORE INTO event_attendees (event_id, person_id) VALUES (?, ?)",
                            (event_id, P(a)),
                        )
    out.question = res.question
    return res, out, event_id


ALMANAC_PROVENANCE_TABLES = ("people", "family_links", "events", "key_dates", "gestures", "contacts")


def undo_almanac(almanac: Almanac, note_id: str, prune_people: bool = True) -> None:
    with almanac.tx() as c:
        for table in ALMANAC_PROVENANCE_TABLES:
            rows = c.execute(
                f"SELECT id, source, source_note_ids FROM {table} WHERE source_note_ids LIKE ?", (f'%"{note_id}"%',)
            ).fetchall()
            for row in rows:
                ids = [n for n in jload(row["source_note_ids"], []) if n != note_id]
                if not ids and row["source"] == "extraction":
                    if table == "people" and not prune_people:
                        c.execute("UPDATE people SET source_note_ids = '[]' WHERE id = ?", (row["id"],))
                        continue
                    c.execute(f"DELETE FROM {table} WHERE id = ?", (row["id"],))
                    continue
                if table in ("family_links", "key_dates") and row["source"] != "form":
                    c.execute(
                        f"UPDATE {table} SET source_note_ids = ?, status = ? WHERE id = ?",
                        (jdump(ids), status_for(ids), row["id"]),
                    )
                else:
                    c.execute(f"UPDATE {table} SET source_note_ids = ? WHERE id = ?", (jdump(ids), row["id"]))


def prune_unsupported_people(almanac: Almanac) -> None:
    with almanac.tx() as c:
        c.execute(
            "DELETE FROM people WHERE is_self = 0 AND source = 'extraction' AND source_note_ids = '[]'"
        )


# ---------------------------------------------------------------------------
# Deep part (needs the vault open)
# ---------------------------------------------------------------------------
def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def _support(c, table: str, row_id: int, note_id: str, extra_sql: str = "", extra: tuple = ()) -> str:
    row = c.execute(f"SELECT source_note_ids FROM {table} WHERE id = ?", (row_id,)).fetchone()
    ids = add_note_id(row["source_note_ids"], note_id)
    status = status_for(ids)
    c.execute(
        f"UPDATE {table} SET source_note_ids = ?, status = ?, last_seen = ?{extra_sql} WHERE id = ?",
        (jdump(ids), status, utcnow(), *extra, row_id),
    )
    return status


def _topic_id(c, label: str, note_id: str, now: str, out: Outcome, sensitivity: int = 1, frames=()) -> int:
    row = c.execute("SELECT id, frames, sensitivity FROM topics WHERE lower(label) = lower(?)", (label,)).fetchone()
    if row is None:
        for t in c.execute("SELECT id, label FROM topics").fetchall():
            if _similar(t["label"], label) >= 0.85:
                row = c.execute("SELECT id, frames, sensitivity FROM topics WHERE id = ?", (t["id"],)).fetchone()
                break
    if row is None:
        cur = c.execute(
            "INSERT INTO topics (label, sensitivity, frames, status, source_note_ids, last_seen) VALUES (?, ?, ?, 'provisional', ?, ?)",
            (label, sensitivity, jdump(list(frames)), jdump([note_id]), now),
        )
        out.new_topics.add(label)
        return cur.lastrowid
    merged = list(dict.fromkeys(jload(row["frames"], []) + list(frames)))
    _support(
        c, "topics", row["id"], note_id, ", frames = ?, sensitivity = ?", (jdump(merged), max(row["sensitivity"], sensitivity))
    )
    return row["id"]


def apply_deep(c, ext: Extraction, res: Resolution, note_id: str, note_day: date, names: dict[int, str]) -> Outcome:
    """Write the political layer. ``c`` is an open Deep transaction."""
    now = utcnow()
    out = Outcome()
    ext = humanise(ext, res, names)
    P = res.pid
    self_id = res.refs["ME"]

    for pid in set(res.refs.values()) - {self_id}:
        c.execute("INSERT OR IGNORE INTO note_people (note_id, person_id) VALUES (?, ?)", (note_id, pid))

    topic_ids: dict[str, int] = {}
    for t in ext.topics:
        if t.ref.startswith("T") and c.execute("SELECT 1 FROM topics WHERE id = ?", (int(t.ref[1:]),)).fetchone():
            tid = int(t.ref[1:])
            row = c.execute("SELECT frames, sensitivity FROM topics WHERE id = ?", (tid,)).fetchone()
            merged = list(dict.fromkeys(jload(row["frames"], []) + list(t.frames)))
            _support(c, "topics", tid, note_id, ", frames = ?, sensitivity = ?", (jdump(merged), max(row["sensitivity"], t.sensitivity)))
        else:
            tid = _topic_id(c, t.label, note_id, now, out, t.sensitivity, t.frames)
        topic_ids[t.label.lower()] = tid
        for tp in t.people:
            pid = P(tp.person)
            row = c.execute("SELECT id FROM person_topics WHERE person_id = ? AND topic_id = ?", (pid, tid)).fetchone()
            if row:
                extra_sql, extra = ", salience = ?", (tp.salience,)
                if tp.frame:
                    extra_sql, extra = ", salience = ?, frame = ?", (tp.salience, tp.frame)
                _support(c, "person_topics", row["id"], note_id, extra_sql, extra)
            else:
                c.execute(
                    "INSERT INTO person_topics (person_id, topic_id, frame, salience, status, source_note_ids, last_seen)"
                    " VALUES (?, ?, ?, ?, 'provisional', ?, ?)",
                    (pid, tid, tp.frame or None, tp.salience, jdump([note_id]), now),
                )

    for i, x in enumerate(ext.interactions):
        a, b = P(x.actor), P(x.target)
        when = x.date or note_day.isoformat()
        tid = None
        if x.topic:
            tid = topic_ids.get(x.topic.lower()) or _topic_id(c, x.topic, note_id, now, out, 2)
            topic_ids[x.topic.lower()] = tid
        relayed_by = P(x.relayed_by) if (x.witnessed == "relayed" and x.relayed_by) else None
        dup = None
        for row in c.execute(
            "SELECT id, date FROM interactions WHERE actor_id = ? AND target_id = ? AND act = ? AND status != 'retired'",
            (a, b, x.act),
        ):
            d1, d2 = parse_date(row["date"]), parse_date(when)
            if d1 and d2 and abs((d1 - d2).days) <= 7:
                dup = row["id"]
                break
        if dup is not None:
            out.statuses[f"interaction:{i}"] = _support(c, "interactions", dup, note_id)
        else:
            if out.question is None and res.question is None and abs(x.score) >= 2:
                q = _contradiction(c, a, b, x.score, names, note_day)
                if q:
                    out.question = q
            cur = c.execute(
                "INSERT INTO interactions (actor_id, target_id, act, score, witnessed, relayed_by_id, topic_id, date,"
                " quote, interpretation, confidence, status, source_note_ids, last_seen)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'provisional', ?, ?)",
                (a, b, x.act, x.score, x.witnessed, relayed_by, tid, when, x.quote, int(x.interpretation),
                 x.confidence, jdump([note_id]), now),
            )
            out.statuses[f"interaction:{i}"] = "provisional"
            if out.question and out.question.get("interaction_id") is None and out.question["kind"] == "contradiction":
                out.question["interaction_id"] = cur.lastrowid
        if relayed_by is not None:
            _edge(c, relayed_by, self_id, "info-flow", "directed", 1, "", note_id, now)

    for i, e in enumerate(ext.edges):
        if e.kind == "kin":
            continue
        tids = []
        for label in e.topics:
            tid = topic_ids.get(label.lower()) or _topic_id(c, label, note_id, now, out, 2)
            topic_ids[label.lower()] = tid
            tids.append(tid)
        out.statuses[f"edge:{i}"] = _edge(
            c, P(e.from_ref), P(e.to_ref), e.kind, e.direction, e.intensity, e.detail, note_id, now, tids, e.confidence
        )

    for c_ in ext.claims:
        if c_.source and c_.source != "ME":
            _edge(c, P(c_.source), self_id, "info-flow", "directed", 1, "", note_id, now)

    for i, t in enumerate(ext.trust):
        a, b = P(t.truster), P(t.trustee)
        row = c.execute(
            "SELECT id FROM trust WHERE truster_id = ? AND trustee_id = ? AND domain = ?", (a, b, t.domain)
        ).fetchone()
        if row:
            out.statuses[f"trust:{i}"] = _support(
                c, "trust", row["id"], note_id, ", level = ?, basis = ?, confidence = ?", (t.level, t.basis, t.confidence)
            )
        else:
            c.execute(
                "INSERT INTO trust (truster_id, trustee_id, domain, level, basis, confidence, status, source_note_ids, last_seen)"
                " VALUES (?, ?, ?, ?, ?, ?, 'provisional', ?, ?)",
                (a, b, t.domain, t.level, t.basis, t.confidence, jdump([note_id]), now),
            )
            out.statuses[f"trust:{i}"] = "provisional"

    for x in ext.claims:
        cur = c.execute(
            "INSERT INTO claims (note_id, source_person_id, about_person_ids, text, credibility, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (note_id, P(x.source) if x.source != "ME" else None, jdump([P(a) for a in x.about]), x.text,
             x.credibility, now),
        )
        if x.relates_to and out.question is None and res.question is None:
            cid = int(x.relates_to[1:])
            old = c.execute("SELECT text, source_person_id, note_id FROM claims WHERE id = ? AND status = 'open'", (cid,)).fetchone()
            if old:
                src = names.get(old["source_person_id"], "someone")
                verb = "confirm" if x.relation == "confirms" else "contradict"
                out.question = {
                    "kind": "claim_check",
                    "prompt": f"Does this note {verb} what {src} told you ({old['note_id']}): “{_decode(old['text'], names)}”?",
                    "options": [
                        {"value": "confirmed", "label": "Yes, it's confirmed"},
                        {"value": "contradicted", "label": "No, it's contradicted"},
                    ],
                    "claim_id": cid,
                    "new_claim_id": cur.lastrowid,
                }

    for m in ext.motives:
        pid = P(m.person)
        match = None
        for row in c.execute("SELECT id, category, detail FROM motives WHERE person_id = ? AND kind = ?", (pid, m.kind)):
            if (m.category != "other" and row["category"] == m.category) or _similar(row["detail"], m.detail) >= 0.6:
                match = row["id"]
                break
        if match:
            _support(c, "motives", match, note_id, ", detail = ?", (m.detail,))
        else:
            c.execute(
                "INSERT INTO motives (person_id, kind, category, detail, confidence, status, source_note_ids, last_seen)"
                " VALUES (?, ?, ?, ?, ?, 'provisional', ?, ?)",
                (pid, m.kind, m.category, m.detail, m.confidence, jdump([note_id]), now),
            )

    for p in ext.profiles:
        c.execute(
            "INSERT INTO person_profiles (person_id, relativism_tolerance, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(person_id) DO UPDATE SET relativism_tolerance = excluded.relativism_tolerance, updated_at = excluded.updated_at",
            (P(p.person), p.relativism_tolerance, now),
        )

    for s in ext.self_observations:
        c.execute(
            "INSERT INTO self_observations (note_id, kind, description, predicted_outcome, person_ids, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (note_id, s.kind, s.description, s.predicted_outcome or None,
             jdump([P(p) for p in s.people if p != "ME"]), now),
        )

    for a in ext.assessments:
        pid = P(a.person)
        target = None
        if a.existing:
            row = c.execute("SELECT id FROM assessments WHERE id = ? AND person_id = ?", (int(a.existing[1:]), pid)).fetchone()
            target = row["id"] if row else None
        if target is None:
            for row in c.execute("SELECT id, pattern FROM assessments WHERE person_id = ? AND retired = 0", (pid,)):
                if _similar(row["pattern"], a.pattern) >= 0.75:
                    target = row["id"]
                    break
        if target is None:
            if a.stance == "supports":
                c.execute(
                    "INSERT INTO assessments (person_id, pattern, supporting_note_ids, first_seen, last_seen)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (pid, a.pattern, jdump([note_id]), now, now),
                )
            continue
        column = "supporting_note_ids" if a.stance == "supports" else "counter_note_ids"
        row = c.execute(f"SELECT {column} FROM assessments WHERE id = ?", (target,)).fetchone()
        c.execute(
            f"UPDATE assessments SET {column} = ?, last_seen = ? WHERE id = ?",
            (jdump(add_note_id(row[column], note_id)), now, target),
        )
    return out


def _edge(c, a, b, kind, direction, intensity, detail, note_id, now, topic_ids=(), confidence="med") -> str:
    if direction == "mutual":
        row = c.execute(
            "SELECT id, intensity, trigger_topic_ids FROM edges WHERE kind = ? AND ((from_id = ? AND to_id = ?) OR (from_id = ? AND to_id = ?))",
            (kind, a, b, b, a),
        ).fetchone()
    else:
        row = c.execute(
            "SELECT id, intensity, trigger_topic_ids FROM edges WHERE kind = ? AND from_id = ? AND to_id = ?", (kind, a, b)
        ).fetchone()
    if row:
        tids = sorted(set(jload(row["trigger_topic_ids"], [])) | set(topic_ids))
        return _support(
            c, "edges", row["id"], note_id, ", intensity = ?, trigger_topic_ids = ?, last_flared = ?" + (", detail = ?" if detail else ""),
            (max(row["intensity"], intensity), jdump(tids), now, *((detail,) if detail else ())),
        )
    c.execute(
        "INSERT INTO edges (from_id, to_id, kind, direction, intensity, detail, trigger_topic_ids, last_flared, confidence,"
        " status, source_note_ids, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'provisional', ?, ?)",
        (a, b, kind, direction, intensity, detail, jdump(sorted(set(topic_ids))), now, confidence, jdump([note_id]), now),
    )
    return "provisional"


def _contradiction(c, a: int, b: int, score: int, names: dict, today: date) -> dict | None:
    from .graph import interaction_rows_to_readings

    rows = c.execute(
        "SELECT actor_id, target_id, score, date, status, source_note_ids FROM interactions"
        " WHERE ((actor_id = ? AND target_id = ?) OR (actor_id = ? AND target_id = ?)) AND status != 'retired'",
        (a, b, b, a),
    ).fetchall()
    if not rows:
        return None
    reading = next(iter(interaction_rows_to_readings(rows, today).values()))
    if reading.status != "corroborated":
        return None
    opposite = (score <= -2 and reading.value >= 2) or (score >= 2 and reading.value <= -2)
    if not opposite:
        return None
    na, nb = names.get(a, "?"), names.get(b, "?")
    stance = "on good terms" if reading.value > 0 else "at odds"
    return {
        "kind": "contradiction",
        "prompt": f"{na} and {nb} have been {stance} ({len(reading.notes)} notes). Is this a real change?",
        "options": [
            {"value": "real", "label": "Yes, log it as a change"},
            {"value": "one-off", "label": "No, a one-off"},
        ],
        "interaction_id": None,
    }


DEEP_PROVENANCE_TABLES = ("motives", "interactions", "edges", "trust", "topics", "person_topics")


def undo_deep(c, note_id: str) -> None:
    like = f'%"{note_id}"%'
    for table in DEEP_PROVENANCE_TABLES:
        for row in c.execute(f"SELECT id, source_note_ids FROM {table} WHERE source_note_ids LIKE ?", (like,)).fetchall():
            ids = [n for n in jload(row["source_note_ids"], []) if n != note_id]
            if not ids:
                c.execute(f"DELETE FROM {table} WHERE id = ?", (row["id"],))
            else:
                c.execute(
                    f"UPDATE {table} SET source_note_ids = ?,"
                    " status = CASE WHEN status = 'retired' THEN 'retired' ELSE ? END WHERE id = ?",
                    (jdump(ids), status_for(ids), row["id"]),
                )
    for row in c.execute(
        "SELECT id, supporting_note_ids, counter_note_ids FROM assessments WHERE supporting_note_ids LIKE ? OR counter_note_ids LIKE ?",
        (like, like),
    ).fetchall():
        sup = [n for n in jload(row["supporting_note_ids"], []) if n != note_id]
        con = [n for n in jload(row["counter_note_ids"], []) if n != note_id]
        if not sup:
            c.execute("DELETE FROM assessments WHERE id = ?", (row["id"],))
        else:
            c.execute(
                "UPDATE assessments SET supporting_note_ids = ?, counter_note_ids = ? WHERE id = ?",
                (jdump(sup), jdump(con), row["id"]),
            )
    c.execute("UPDATE claims SET status = 'open', resolved_by_note_id = NULL WHERE resolved_by_note_id = ?", (note_id,))
    c.execute("DELETE FROM claims WHERE note_id = ?", (note_id,))
    c.execute("DELETE FROM self_observations WHERE note_id = ?", (note_id,))
    c.execute("DELETE FROM note_people WHERE note_id = ?", (note_id,))
    c.execute("DELETE FROM questions WHERE note_id = ? AND answered_at IS NULL", (note_id,))
    c.execute("DELETE FROM mirror_log WHERE note_id = ?", (note_id,))
    c.execute("UPDATE extractions SET active = 0 WHERE note_id = ?", (note_id,))


# ---------------------------------------------------------------------------
# Readback
# ---------------------------------------------------------------------------
def _decode(text: str, names: dict[int, str]) -> str:
    text = re.sub(r"\bP(\d+)\b", lambda m: names.get(int(m.group(1)), m.group(0)), text)
    return re.sub(r"\bME\b", "you", text)


def _fmt_day(iso: str) -> str:
    d = parse_date(iso)
    return d.strftime("%a %-d %b") if d else iso


def compose_readback(ext: Extraction, res: Resolution, names: dict[int, str], statuses: dict[str, str],
                     follow_ups: dict[int, str], new_topics: set[str], event_title: str | None,
                     mirror_line: str | None, deep_pending: bool) -> str:
    ext = humanise(ext, res, names, me="you")

    def n(ref: str) -> str:
        if ref == "ME":
            return "you"
        return names.get(res.refs.get(ref, -1), ref)

    def tag(key: str) -> str:
        return f" ({statuses.get(key, 'provisional')})"

    logged: list[str] = []
    # Interactions grouped per pair and topic, so one fight reads as one line.
    groups: dict[tuple, list[int]] = {}
    for i, x in enumerate(ext.interactions):
        key = (frozenset((x.actor, x.target)), x.topic.lower())
        groups.setdefault(key, []).append(i)
    for (pair, topic), idxs in groups.items():
        xs = [ext.interactions[i] for i in idxs]
        total = sum(x.score for x in xs)
        over = f" over {xs[0].topic}" if xs[0].topic else ""
        st = "corroborated" if any(statuses.get(f"interaction:{i}") == "corroborated" for i in idxs) else "provisional"
        if total < 0 and len(xs) > 1:
            a, b = sorted(pair)
            logged.append(f"tension between {n(a)} and {n(b)}{over} ({st})")
        else:
            x = xs[0]
            logged.append(f"{n(x.actor)} {ACT_PHRASES[x.act]} {n(x.target)}{over} ({st})")
    for i, e in enumerate(ext.edges):
        if e.kind == "kin":
            logged.append(f"{n(e.from_ref)} is {n(e.to_ref)}'s {e.relation}")
        elif e.kind == "friendship":
            logged.append(f"{n(e.from_ref)} and {n(e.to_ref)} are friends{tag(f'edge:{i}')}")
        elif e.kind == "debt":
            what = f" ({e.detail})" if e.detail else ""
            logged.append(f"{n(e.from_ref)} owes {n(e.to_ref)}{what}{tag(f'edge:{i}')}")
        else:
            logged.append(f"{n(e.from_ref)} passes things on to {n(e.to_ref)}{tag(f'edge:{i}')}")
    for i, t in enumerate(ext.trust):
        verb = "trusts" if t.level > 0 else ("doesn't trust" if t.level < 0 else "is unsure about")
        if t.truster == "ME":
            verb = {"trusts": "trust", "doesn't trust": "don't trust", "is unsure about": "are unsure about"}[verb]
        logged.append(f"{n(t.truster)} {verb} {n(t.trustee)} with {t.domain}{tag(f'trust:{i}')}")
    for m in ext.motives:
        logged.append(f"{n(m.person)} {'wants' if m.kind == 'want' else 'fears'} {m.detail}")
    for label in sorted(new_topics):
        logged.append(f"new topic: {label}")

    sentences: list[str] = []
    new_people = [names[p] for p in res.created if p in names]
    if new_people:
        sentences.append("New: " + ", ".join(new_people) + ".")
    if logged:
        shown = logged[:5]
        more = f"; and {len(logged) - 5} more" if len(logged) > 5 else ""
        sentences.append("Logged: " + "; ".join(shown) + more + ".")
    routes = []
    for x in ext.interactions:
        if x.witnessed == "relayed" and x.relayed_by and x.relayed_by not in routes:
            routes.append(x.relayed_by)
    for cl in ext.claims:
        if cl.source and cl.source != "ME" and cl.source not in routes:
            routes.append(cl.source)
    for r in routes[:2]:
        sentences.append(f"{n(r)} told you, so they're a likely info route.")
    for i, k in enumerate(ext.key_dates):
        s = f"Noted {n(k.person)}'s {k.label} on {_fmt_day(k.date)}"
        if i in follow_ups:
            s += f"; I'll prompt you to follow up on {_fmt_day(follow_ups[i])}"
        sentences.append(s + ".")
    for g in ext.gestures:
        sentences.append(f"Gift log: {g.what} for {n(g.person)}.")
    if ext.contacts:
        who = ", ".join(n(c.person) for c in ext.contacts if c.person != "ME")
        if who:
            sentences.append(f"In touch with {who}.")
    for s in ext.self_observations:
        label = {"misfire": "Misfire noted", "win": "Win noted", "prediction": "Prediction logged"}[s.kind]
        sentences.append(f"{label}: {s.description}.")
    if event_title:
        sentences.append(f"Filed under {event_title}.")
    if not sentences:
        sentences.append("Saved. Nothing to file from this one.")
    if deep_pending:
        sentences.append("Private details are sealed until you next open Private.")
    if mirror_line:
        sentences.append(mirror_line.strip())
    return " ".join(sentences)
