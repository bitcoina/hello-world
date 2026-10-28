"""Dates, follow-ups, cadence nudges and the Sunday digest.

Designed for lapses: the digest only ever looks at this week. Follow-ups older than
two weeks quietly drop away, nudges are regenerated each week rather than piling
up, and nothing is ever shown as overdue.
"""

from __future__ import annotations

import logging
import random
from datetime import date, datetime, timedelta

from .almanac import Almanac
from .db import jload, parse_date, utcnow

log = logging.getLogger(__name__)

LAYER_CADENCE_DAYS = {15: 21, 50: 75, 150: 365}
MAX_NUDGES = 3
FOLLOW_UP_GRACE_DAYS = 14
DORMANT_DAYS = 180

GESTURE_IDEAS = [
    "a quick text, no reason needed",
    "a photo that reminded you of them",
    "an article they'd enjoy",
    "a coffee",
    "a short call",
]


def week_start(d: date) -> date:
    """Weeks start on Sunday, the digest day."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


def next_occurrence(iso: str, recurring: bool, today: date) -> date | None:
    d = parse_date(iso)
    if d is None:
        return None
    if not recurring:
        return d
    for year in (today.year, today.year + 1):
        try:
            candidate = d.replace(year=year)
        except ValueError:  # 29 Feb
            candidate = date(year, 2, 28)
        if candidate >= today:
            return candidate
    return None


def upcoming_dates(alm: Almanac, today: date, days: int = 14, person_ids: set[int] | None = None) -> list[dict]:
    out = []
    for r in alm.query(
        "SELECT k.*, p.display_name FROM key_dates k JOIN people p ON p.id = k.person_id"
    ):
        if person_ids is not None and r["person_id"] not in person_ids:
            continue
        when = next_occurrence(r["date"], bool(r["recurring"]), today)
        if when is None or not (today <= when <= today + timedelta(days=days)):
            continue
        original = parse_date(r["date"])
        years = when.year - original.year if (r["recurring"] and original and original.year > 1900) else None
        out.append({
            "id": r["id"],
            "person_id": r["person_id"],
            "person": r["display_name"],
            "kind": r["kind"],
            "label": r["label"],
            "date": when.isoformat(),
            "years": years if years and years > 0 else None,
            "status": r["status"],
            "note_ids": jload(r["source_note_ids"], []),
        })
    return sorted(out, key=lambda x: x["date"])


def follow_ups_due(alm: Almanac, today: date, person_ids: set[int] | None = None, horizon: int = 7) -> list[dict]:
    rows = alm.query(
        "SELECT f.*, p.display_name FROM follow_ups f JOIN people p ON p.id = f.person_id"
        " WHERE f.done_at IS NULL AND f.dismissed_at IS NULL AND f.due_date BETWEEN ? AND ? ORDER BY f.due_date",
        ((today - timedelta(days=FOLLOW_UP_GRACE_DAYS)).isoformat(), (today + timedelta(days=horizon)).isoformat()),
    )
    return [
        {"id": r["id"], "person_id": r["person_id"], "person": r["display_name"], "due": r["due_date"], "prompt": r["prompt"]}
        for r in rows
        if person_ids is None or r["person_id"] in person_ids
    ]


def last_contacts(alm: Almanac) -> dict[int, date]:
    out: dict[int, date] = {}
    for table in ("contacts", "gestures"):
        for r in alm.query(f"SELECT person_id, max(date) AS d FROM {table} GROUP BY person_id"):
            d = parse_date(r["d"])
            if d and (r["person_id"] not in out or d > out[r["person_id"]]):
                out[r["person_id"]] = d
    return out


def _nudge_text(name: str, days: int, idea: str) -> str:
    if days >= 60:
        gap = f"about {round(days / 30)} months"
    else:
        gap = f"about {max(1, round(days / 7))} weeks"
    return f"It's been {gap} since you were in touch with {name}. Try {idea}: people value unprompted contact more than we expect."


def plan_nudges(alm: Almanac, today: date, rng: random.Random | None = None) -> list[dict]:
    rng = rng or random.Random(today.toordinal())
    contacts = last_contacts(alm)
    candidates = []
    dormant = []
    for p in alm.people():
        created = parse_date(p["created_at"]) or today
        last = contacts.get(p["id"])
        since = (today - (last or created)).days
        cadence = LAYER_CADENCE_DAYS.get(p["layer"] or 0)
        if cadence and since > cadence:
            candidates.append((since / cadence, p, since))
        history = len(p["source_note_ids"]) + (3 if last else 0)
        if since >= DORMANT_DAYS and history >= 2 and p["layer"] in (None, 50, 150):
            dormant.append((history, p, since))
    last_kind = {r["person_id"]: r["kind"] for r in alm.query(
        "SELECT person_id, kind FROM gestures g WHERE date = (SELECT max(date) FROM gestures WHERE person_id = g.person_id)"
    )}
    nudges = []
    if today.day <= 7 and dormant:
        # Once a month, the app picks a dormant tie, so anxiety doesn't pick the wrong one.
        top = sorted(dormant, key=lambda t: (-t[0], t[1]["id"]))[:3]
        _, p, since = rng.choice(top)
        nudges.append({
            "person_id": p["id"],
            "kind": "dormant",
            "text": f"Reconnect: {p['display_name']}. You last spoke about {max(6, round(since / 30))} months ago; "
                    "dormant ties often give more than current ones.",
        })
    for _, p, since in sorted(candidates, key=lambda t: -t[0]):
        if len(nudges) >= MAX_NUDGES:
            break
        if any(n["person_id"] == p["id"] for n in nudges):
            continue
        previous = last_kind.get(p["id"])
        ideas = [i for i in GESTURE_IDEAS if not previous or previous not in i] or GESTURE_IDEAS
        nudges.append({"person_id": p["id"], "kind": "cadence", "text": _nudge_text(p["display_name"], since, rng.choice(ideas))})
    return nudges[:MAX_NUDGES]


def ensure_week(alm: Almanac, today: date) -> str:
    ws = week_start(today).isoformat()
    if alm.one("SELECT 1 FROM digests WHERE week_start = ?", (ws,)):
        return ws
    nudges = plan_nudges(alm, today)
    now = utcnow()
    with alm.tx() as c:
        c.execute("INSERT OR IGNORE INTO digests (week_start, created_at) VALUES (?, ?)", (ws, now))
        for n in nudges:
            c.execute(
                "INSERT INTO nudges (week_start, person_id, kind, text, created_at) VALUES (?, ?, ?, ?, ?)",
                (ws, n["person_id"], n["kind"], n["text"], now),
            )
    return ws


def today_view(alm: Almanac, today: date) -> dict:
    ws = ensure_week(alm, today)
    nudges = [
        {"id": r["id"], "person_id": r["person_id"], "kind": r["kind"], "text": r["text"]}
        for r in alm.query(
            "SELECT * FROM nudges WHERE week_start = ? AND done_at IS NULL AND dismissed_at IS NULL ORDER BY id", (ws,)
        )
    ]
    return {
        "week_start": ws,
        "dates": upcoming_dates(alm, today),
        "follow_ups": follow_ups_due(alm, today),
        "nudges": nudges,
    }


def send_weekly_push(alm: Almanac, ntfy_url: str | None, today: date) -> bool:
    """One neutral push a week. No names, ever."""
    ws = ensure_week(alm, today)
    row = alm.one("SELECT pushed_at FROM digests WHERE week_start = ?", (ws,))
    if not ntfy_url or (row and row["pushed_at"]):
        return False
    import httpx

    try:
        httpx.post(ntfy_url, content="Your weekly reading is ready.", headers={"Title": "Barometer: weekly reading", "Tags": "partly_sunny"}, timeout=10)
    except httpx.HTTPError:
        log.warning("weekly push failed", exc_info=True)
        return False
    with alm.tx() as c:
        c.execute("UPDATE digests SET pushed_at = ? WHERE week_start = ?", (utcnow(), ws))
    return True


def initiation_ledger(alm: Almanac, start: date, end: date) -> list[dict]:
    rows = alm.query(
        "SELECT c.person_id, p.display_name, c.initiated_by, count(*) AS n FROM contacts c JOIN people p ON p.id = c.person_id"
        " WHERE c.date BETWEEN ? AND ? GROUP BY c.person_id, c.initiated_by",
        (start.isoformat(), end.isoformat()),
    )
    ledger: dict[int, dict] = {}
    for r in rows:
        entry = ledger.setdefault(r["person_id"], {"person_id": r["person_id"], "person": r["display_name"], "me": 0, "them": 0, "mutual": 0})
        entry[r["initiated_by"]] += r["n"]
    out = []
    for e in ledger.values():
        total = e["me"] + e["them"]
        e["risk"] = bool(total >= 4 and e["them"] / total >= 0.75)
        out.append(e)
    return sorted(out, key=lambda e: -(e["me"] + e["them"] + e["mutual"]))


def is_due(now_local: datetime, weekday: int = 6, hour: int = 8) -> bool:
    """Sunday from 08:00 local by default."""
    return now_local.weekday() == weekday and now_local.hour >= hour
