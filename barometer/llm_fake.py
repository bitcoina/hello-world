"""A stand-in LLM for tests and offline demos (BAROMETER_LLM=fake).

Queue exact responses with ``push``; otherwise it makes a crude but valid guess so
the app can be clicked through without an API key.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from pydantic import BaseModel

from .llm import LLMError
from .schemas import BriefOut, ExplainOut, Extraction, RecordCheckOut, ReflectOut

STOP = {
    "I", "The", "A", "An", "And", "But", "So", "Then", "We", "She", "He", "They", "It", "My", "Our", "At", "On", "In",
    "Today", "Yesterday", "Tomorrow", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    "This", "That", "When", "After", "Before", "Mum", "Dad", "Just", "Also", "If", "Not", "No", "Yes", "Had", "Was",
    "Christmas", "Easter", "New", "Year", "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
}


class FakeLLM:
    name = "fake"

    def __init__(self) -> None:
        self.queue: list[Any] = []
        self.calls: list[dict] = []

    def push(self, response: Any) -> None:
        """A dict, a model instance, an exception, or ``fn(user) -> dict``."""
        self.queue.append(response)

    def structured(self, *, task: str, system: str, user: str, schema: type[BaseModel], effort: str,
                   validate: Callable | None = None):
        self.calls.append({"task": task, "system": system, "user": user, "schema": schema.__name__, "effort": effort})
        if self.queue:
            resp = self.queue.pop(0)
            if isinstance(resp, BaseException):
                raise resp
            if callable(resp):
                resp = resp(user)
        else:
            resp = self._guess(schema, user)
        result = resp if isinstance(resp, schema) else schema.model_validate(resp)
        if validate:
            try:
                validate(result)
            except ValueError as exc:
                raise LLMError(f"{task}: output failed validation: {exc}") from exc
        return result

    # -- guesses ---------------------------------------------------------------
    def _guess(self, schema, user: str) -> dict:
        if schema is Extraction:
            return self._extraction(user)
        notes = re.findall(r'<note id="(N\d+)"', user) or re.findall(r"\b(N\d+)\b", user)
        cite = notes[:2]
        refs = re.findall(r"\bP\d+\b", user.split("In focus:")[-1].split("\n")[0]) if "In focus:" in user else []
        if schema is BriefOut:
            return {
                "fault_lines": [{"text": "Old tensions may surface; keep conversation light.", "note_ids": cite}] if cite else [],
                "wants": [{"person": r, "wants": "to be listened to", "frame": "", "note_ids": cite} for r in refs[:3]] if cite else [],
                "trust_cautions": [],
                "avoid": [],
                "seating": [],
                "prediction": {"text": "The evening stays civil.", "note_ids": cite},
                "mirror_line": "",
                "uses_assessments": [],
            }
        if schema is ExplainOut:
            return {
                "what_happened": [{"text": "Something changed recently.", "note_ids": cite}] if cite else [],
                "explanations": [
                    {"explanation": "Pressure from outside the family.", "supporting": cite, "contradicting": [], "confidence": "low"},
                    {"explanation": "A misunderstanding that hasn't been aired.", "supporting": [], "contradicting": [], "confidence": "low"},
                ],
                "would_tell_apart": ["Whether they mention it unprompted."],
                "mirror_line": "",
            }
        if schema is RecordCheckOut:
            return {"missing": [], "contradictions": [], "stale": []}
        if schema is ReflectOut:
            return {"patterns": [], "misfire_themes": [], "wins": [], "mirror_line": ""}
        raise LLMError(f"FakeLLM has no guess for {schema.__name__}")

    def _extraction(self, user: str) -> dict:
        note = user.split("<note>", 1)[-1].split("</note>", 1)[0]
        people, seen = [], set()
        for ref in re.findall(r"\bP\d+\b", note):
            if ref not in seen and re.search(rf"- {ref}\b", user):
                seen.add(ref)
                people.append(_mention(ref, ref))
        n = 0
        for match in re.finditer(r"\b[A-Z][a-z]{2,}\b", note):
            word = match.group(0)
            before = note[: match.start()].rstrip()
            sentence_start = not before or before[-1] in ".!?\n"
            if word in STOP or word in seen or sentence_start:
                continue
            seen.add(word)
            n += 1
            people.append(_mention(f"X{n}", word))
        words = note.split()
        return {
            "summary": " ".join(words[:15]),
            "people": people,
            "interactions": [], "edges": [], "trust": [], "claims": [], "topics": [], "motives": [],
            "key_dates": [], "gestures": [], "contacts": [], "self_observations": [], "assessments": [],
            "profiles": [], "events": [],
            "mirror": {"pattern": "none", "strength": 0, "line": ""},
        }


def _mention(ref: str, name: str) -> dict:
    return {"ref": ref, "name": name, "match_confidence": "high", "candidates": [], "group": "unknown",
            "relation_to_author": "", "quote": name}


def empty_extraction(**overrides) -> dict:
    base = {
        "summary": "", "people": [], "interactions": [], "edges": [], "trust": [], "claims": [], "topics": [],
        "motives": [], "key_dates": [], "gestures": [], "contacts": [], "self_observations": [], "assessments": [],
        "profiles": [], "events": [], "mirror": {"pattern": "none", "strength": 0, "line": ""},
    }
    base.update(overrides)
    return base


def person(ref: str, name: str, **kw) -> dict:
    return {**_mention(ref, name), **kw}
