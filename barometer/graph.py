"""The political graph: relationships derived from logged interactions.

Nothing here stores "A and B are allies". Alliance and tension are computed on read
from the interaction log, each interaction decaying with a six-month half-life, so
old feuds fade without a decay job.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from itertools import combinations

from .db import jload, parse_date

HALF_LIFE_DAYS = 182.6
TREND_WINDOW_DAYS = 90
STATUS_WEIGHT = {"corroborated": 1.0, "provisional": 0.6, "retired": 0.0}


def decay(age_days: float) -> float:
    return 0.5 ** (max(age_days, 0.0) / HALF_LIFE_DAYS)


@dataclass
class PairReading:
    a: int
    b: int
    value: float = 0.0  # decayed, status-weighted sum of scores
    pos: float = 0.0
    neg: float = 0.0
    recent: float = 0.0  # raw sum over the trend window
    count: int = 0
    notes: set = field(default_factory=set)
    topics: set = field(default_factory=set)
    last: date | None = None

    @property
    def trend(self) -> str:
        if self.recent >= 1.5:
            return "warming"
        if self.recent <= -1.5:
            return "cooling"
        return "stable"

    @property
    def weather(self) -> str:
        return weather(self.value, self.pos, self.neg)

    @property
    def status(self) -> str:
        return "corroborated" if len(self.notes) >= 2 else "provisional"

    @property
    def sign(self) -> int:
        if self.value >= 0.5:
            return 1
        if self.value <= -0.5:
            return -1
        return 0

    @property
    def needle(self) -> float:
        """-1 (stormy) .. +1 (fair), for the dial."""
        return math.tanh(self.value / 3.0)

    def as_dict(self) -> dict:
        return {
            "a": self.a,
            "b": self.b,
            "value": round(self.value, 2),
            "trend": self.trend,
            "weather": self.weather,
            "status": self.status,
            "needle": round(self.needle, 3),
            "notes": sorted(self.notes, key=_note_key),
            "topics": sorted(self.topics),
            "count": self.count,
            "last": self.last.isoformat() if self.last else None,
        }


def _note_key(note_id: str) -> int:
    try:
        return int(note_id.lstrip("N"))
    except ValueError:
        return 0


def weather(value: float, pos: float, neg: float) -> str:
    if value <= -2:
        return "Stormy"
    if pos >= 1 and neg >= 1:
        return "Changeable"
    if value >= 2:
        return "Fair"
    if value <= -0.5:
        return "Changeable"
    return "Settled"


def interaction_rows_to_readings(rows, today: date, directed: bool = False) -> dict[tuple[int, int], PairReading]:
    """Fold interaction rows into pair readings. Undirected unless ``directed``."""
    readings: dict[tuple[int, int], PairReading] = {}
    for r in rows:
        w = STATUS_WEIGHT.get(r["status"], 0.6)
        if w == 0:
            continue
        a, b = r["actor_id"], r["target_id"]
        key = (a, b) if directed else (min(a, b), max(a, b))
        reading = readings.get(key)
        if reading is None:
            reading = readings[key] = PairReading(*key)
        when = parse_date(r["date"]) or today
        s = r["score"] * w * decay((today - when).days)
        reading.value += s
        if s > 0:
            reading.pos += s
        elif s < 0:
            reading.neg -= s
        if (today - when).days <= TREND_WINDOW_DAYS:
            reading.recent += r["score"] * w
        reading.count += 1
        reading.notes.update(jload(r["source_note_ids"], []))
        if "topic_label" in r.keys() and r["topic_label"]:
            reading.topics.add(r["topic_label"])
        if reading.last is None or when > reading.last:
            reading.last = when
    return readings


def signed_graph(readings: dict[tuple[int, int], PairReading], friendships=()) -> dict[tuple[int, int], int]:
    signs = {k: r.sign for k, r in readings.items() if r.sign != 0}
    for a, b in friendships:
        key = (min(a, b), max(a, b))
        signs.setdefault(key, 1)
    return signs


def unbalanced_triads(signs: dict[tuple[int, int], int]) -> list[tuple[int, int, int]]:
    """Triangles with an odd number of negative edges (balance theory)."""
    adj: dict[int, set[int]] = defaultdict(set)
    for a, b in signs:
        adj[a].add(b)
        adj[b].add(a)
    out = []
    for a in sorted(adj):
        for b, c in combinations(sorted(n for n in adj[a] if n > a), 2):
            if c in adj[b]:
                product = signs[(a, b)] * signs[(a, c)] * signs[(min(b, c), max(b, c))]
                if product < 0:
                    out.append((a, b, c))
    return out


def source_grade(confirmed: int, contradicted: int) -> str:
    """Admiralty-style reliability of a person's reports: A (reliable) .. F (cannot judge)."""
    n = confirmed + contradicted
    if n < 2:
        return "F"
    ratio = confirmed / n
    if ratio >= 0.9 and n >= 5:
        return "A"
    if ratio >= 0.75:
        return "B"
    if ratio >= 0.6:
        return "C"
    if ratio >= 0.4:
        return "D"
    return "E"


SOURCE_GRADE_TEXT = {
    "A": "reliable",
    "B": "usually reliable",
    "C": "fairly reliable",
    "D": "not usually reliable",
    "E": "unreliable",
    "F": "cannot judge yet",
}
CREDIBILITY_TEXT = {
    1: "confirmed",
    2: "probably true",
    3: "possibly true",
    4: "doubtful",
    5: "improbable",
    6: "cannot judge",
}


def assessment_confidence(supporting: int, counter: int, last_seen: date | None, today: date) -> float:
    if supporting == 0:
        return 0.0
    base = min(1.0, supporting / 3.0) * (supporting / (supporting + counter))
    age = (today - last_seen).days if last_seen else 0
    return round(base * decay(age), 2)


def assessment_status(supporting: int, counter: int, retired: bool) -> str:
    if retired:
        return "retired"
    if counter >= 2 and counter * 2 >= supporting:
        return "flag for retirement"
    return "established" if supporting >= 3 else "hunch"
