"""Swap people's names for refs (P12) before text leaves the Pi, and back again on return."""

from __future__ import annotations

import re
from collections import defaultdict

from .almanac import normalise

# Relational words are matched case-insensitively ("mum" / "Mum"). Plain names are
# matched as written, so "Will" the person doesn't swallow "will" the verb.
RELATIONAL = {
    "mum", "mom", "mother", "dad", "father", "sister", "brother", "gran", "granny", "grandma",
    "grandmother", "grandad", "granddad", "grandpa", "grandfather", "nan", "nana", "nanna", "aunt",
    "auntie", "uncle", "wife", "husband", "partner", "son", "daughter", "boss", "stepmum", "stepdad",
    "mother-in-law", "father-in-law", "sister-in-law", "brother-in-law", "cousin", "niece", "nephew",
}

REF_RE = re.compile(r"\bP(\d+)\b")


def name_variants(person: dict) -> list[str]:
    out: list[str] = []
    for raw in [person["display_name"], *person.get("aliases", [])]:
        raw = raw.strip()
        if not raw:
            continue
        out.append(raw)
        bare = re.sub(r"^(my|our)\s+", "", raw, flags=re.I)
        if bare != raw:
            out.append(bare)
    return sorted(set(out), key=len, reverse=True)


def variant_regex(variant: str) -> re.Pattern:
    insensitive = " " in variant or normalise(variant) in RELATIONAL
    body = re.escape(variant)
    if insensitive:
        # also catch "my mum" / "our mum" so the possessive goes too
        return re.compile(rf"\b(?:(?:my|our)\s+)?{body}\b", re.I)
    return re.compile(rf"\b{body}\b")


class Pseudonymiser:
    def __init__(self, people: list[dict], enabled: bool = True):
        self.enabled = enabled
        self.names = {p["id"]: p["display_name"] for p in people}
        owners: dict[str, set[int]] = defaultdict(set)
        variants: dict[str, set[int]] = defaultdict(set)
        for p in people:
            if p.get("is_self"):
                continue
            for v in name_variants(p):
                owners[normalise(v)].add(p["id"])
                variants[v].add(p["id"])
        self.ambiguous = {n: sorted(ids) for n, ids in owners.items() if len(ids) > 1}
        self._rules: list[tuple[re.Pattern, int]] = []
        for v, ids in sorted(variants.items(), key=lambda kv: len(kv[0]), reverse=True):
            if len(owners[normalise(v)]) == 1:
                self._rules.append((variant_regex(v), next(iter(ids))))

    def encode(self, text: str) -> str:
        if not self.enabled or not text:
            return text
        # Protect existing refs and placeholders, then replace longest names first.
        for pattern, pid in self._rules:
            text = pattern.sub(f"P{pid}", text)
        return text

    def decode(self, text: str) -> str:
        if not text:
            return text
        return REF_RE.sub(lambda m: self.names.get(int(m.group(1)), m.group(0)), text)

    def label(self, pid: int) -> str:
        """How a person is referred to inside a prompt."""
        if self.enabled:
            return f"P{pid}"
        return f"P{pid} ({self.names.get(pid, '?')})"
