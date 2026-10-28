"""Strict output schemas for every LLM call.

All fields are required and optional values use "" or empty lists rather than null:
that keeps the JSON schema inside the structured-output limits (no unions) and gives
the validator one shape to check.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Conf = Literal["low", "med", "high"]

ACTS = (
    "praised",
    "helped",
    "sided with",
    "confided in",
    "criticised",
    "excluded",
    "undermined",
    "broke contact",
    "reconciled",
)
Act = Literal[
    "praised",
    "helped",
    "sided with",
    "confided in",
    "criticised",
    "excluded",
    "undermined",
    "broke contact",
    "reconciled",
]
TRUST_DOMAINS = ("secrets", "money", "practical help", "emotional support", "judgement")
TrustDomain = Literal["secrets", "money", "practical help", "emotional support", "judgement"]
WANTS = ("status", "security", "belonging", "fairness", "being right", "autonomy")
KEY_DATE_KINDS = (
    "birthday",
    "anniversary",
    "bereavement",
    "hospital",
    "exam",
    "interview",
    "house move",
    "new job",
    "result",
    "other",
)
MIRROR_PATTERNS = (
    "edge_case_argument",
    "ends_justify_means",
    "devils_advocate_on_values",
    "moral_relativism",
)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
class PersonMention(Strict):
    ref: str = Field(description='"P<id>" for someone in the supplied index, or "X1", "X2"... for someone new')
    name: str = Field(description="The words the note uses for them, e.g. 'Sarah' or 'my sister'")
    match_confidence: Conf
    candidates: list[str] = Field(description="Other P-refs this could plausibly be; empty when the match is clear")
    group: Literal["family", "friends", "work-adjacent", "unknown"]
    relation_to_author: str = Field(description="e.g. 'mother', 'sister', 'colleague'; empty if unknown")
    quote: str


class InteractionOut(Strict):
    actor: str
    target: str
    act: Act
    score: Literal[-3, -2, -1, 0, 1, 2, 3]
    witnessed: Literal["me", "relayed"]
    relayed_by: str = Field(description="Person ref who told the author, when relayed; else empty")
    topic: str = Field(description="Topic label the interaction was about; else empty")
    date: str = Field(description="YYYY-MM-DD if known or inferable; else empty")
    interpretation: bool = Field(description="True if this reads intent into behaviour rather than reporting it")
    confidence: Conf
    quote: str


class EdgeOut(Strict):
    from_ref: str
    to_ref: str
    kind: Literal["kin", "friendship", "debt", "info-flow"]
    relation: str = Field(description="For kin: from_ref is the <relation> of to_ref, e.g. 'mother'. Else empty")
    direction: Literal["directed", "mutual"]
    intensity: Literal[1, 2, 3]
    detail: str
    topics: list[str]
    confidence: Conf
    quote: str


class TrustOut(Strict):
    truster: str
    trustee: str
    domain: TrustDomain
    level: Literal[-2, -1, 0, 1, 2]
    basis: Literal["stated", "observed", "inferred"]
    confidence: Conf
    quote: str


class ClaimOut(Strict):
    source: str = Field(description="Person ref who told the author this")
    about: list[str]
    text: str = Field(description="The relayed claim, stated neutrally")
    credibility: Literal[1, 2, 3, 4, 5, 6]
    relates_to: str = Field(description='An open claim ref "C<id>" this note confirms or contradicts; else empty')
    relation: Literal["none", "confirms", "contradicts"]
    quote: str


class TopicPerson(Strict):
    person: str
    frame: str = Field(description="The frame they hold on this topic; empty if unknown")
    salience: Literal[1, 2, 3]


class TopicOut(Strict):
    ref: str = Field(description='"T<id>" for a supplied topic, else "new"')
    label: str
    sensitivity: Literal[1, 2, 3]
    frames: list[str] = Field(description="Competing frames, e.g. ['fairness', 'reward for effort']")
    people: list[TopicPerson]
    quote: str


class MotiveOut(Strict):
    person: str
    kind: Literal["want", "fear"]
    category: Literal["status", "security", "belonging", "fairness", "being right", "autonomy", "other"]
    detail: str
    confidence: Conf
    quote: str


class KeyDateOut(Strict):
    person: str
    kind: Literal[
        "birthday",
        "anniversary",
        "bereavement",
        "hospital",
        "exam",
        "interview",
        "house move",
        "new job",
        "result",
        "other",
    ]
    label: str = Field(description="Short label, e.g. 'hip operation'")
    date: str = Field(description="YYYY-MM-DD")
    recurring: bool
    quote: str


class GestureOut(Strict):
    person: str
    kind: Literal["text", "gift", "photo", "article", "coffee", "call", "visit", "other"]
    what: str
    date: str
    landed: str = Field(description="How it landed, if the note says; else empty")
    quote: str


class ContactOut(Strict):
    person: str
    initiated_by: Literal["me", "them", "mutual"]
    channel: str
    date: str
    quote: str


class SelfObservationOut(Strict):
    kind: Literal["misfire", "win", "prediction"]
    description: str
    predicted_outcome: str
    people: list[str]
    quote: str


class AssessmentOut(Strict):
    person: str
    pattern: str = Field(description="One testable behavioural sentence; never a label or diagnosis")
    stance: Literal["supports", "counters"]
    existing: str = Field(description='"A<id>" if this bears on a supplied pattern; else empty')
    quote: str


class ProfileOut(Strict):
    person: str
    relativism_tolerance: Literal["low", "normal", "high"]
    quote: str


class EventOut(Strict):
    ref: str = Field(description='"E<id>" for a supplied open event, else "new"')
    title: str
    date: str
    location: str
    type: Literal["gathering", "1:1", "message thread"]
    attendees: list[str]


class MirrorOut(Strict):
    pattern: Literal[
        "none", "edge_case_argument", "ends_justify_means", "devils_advocate_on_values", "moral_relativism"
    ]
    strength: Literal[0, 1, 2, 3]
    line: str = Field(description="One advisory sentence with its reason; empty when pattern is none")


class Extraction(Strict):
    summary: str = Field(description="One line, under 20 words, using refs not names")
    people: list[PersonMention]
    interactions: list[InteractionOut]
    edges: list[EdgeOut]
    trust: list[TrustOut]
    claims: list[ClaimOut]
    topics: list[TopicOut]
    motives: list[MotiveOut]
    key_dates: list[KeyDateOut]
    gestures: list[GestureOut]
    contacts: list[ContactOut]
    self_observations: list[SelfObservationOut]
    assessments: list[AssessmentOut]
    profiles: list[ProfileOut]
    events: list[EventOut] = Field(description="At most one: the event this note is about, if any")
    mirror: MirrorOut


# ---------------------------------------------------------------------------
# Retrieval modes
# ---------------------------------------------------------------------------
class CitedItem(Strict):
    text: str
    note_ids: list[str] = Field(description='Note IDs such as "N12" that support this')


class PersonWant(Strict):
    person: str
    wants: str
    frame: str
    note_ids: list[str]


class BriefOut(Strict):
    fault_lines: list[CitedItem]
    wants: list[PersonWant]
    trust_cautions: list[CitedItem]
    avoid: list[CitedItem]
    seating: list[CitedItem]
    prediction: CitedItem
    mirror_line: str = Field(description="Empty unless mirror is allowed and warranted")
    uses_assessments: list[str] = Field(description='Assessment refs "A<id>" drawn on, if any')


class Hypothesis(Strict):
    explanation: str
    supporting: list[str]
    contradicting: list[str]
    confidence: Conf


class ExplainOut(Strict):
    what_happened: list[CitedItem]
    explanations: list[Hypothesis]
    would_tell_apart: list[str]
    mirror_line: str


class RecordIssue(Strict):
    text: str
    note_ids: list[str]
    target: str = Field(description='Ref of an existing stored item ("I<id>" interaction, "D<id>" edge) or empty')


class RecordCheckOut(Strict):
    missing: list[RecordIssue]
    contradictions: list[RecordIssue]
    stale: list[RecordIssue]


class ReflectOut(Strict):
    patterns: list[CitedItem]
    misfire_themes: list[CitedItem]
    wins: list[CitedItem]
    mirror_line: str
