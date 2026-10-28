"""Turning one note into structure: the prompt, the index it sees, and validation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .almanac import Almanac
from .db import jload, parse_date
from .deep import DeepVault
from .llm import LLM
from .pseudo import Pseudonymiser
from .schemas import Extraction

EXTRACTION_SYSTEM = """\
You turn one private note about the author's family and friends into structure, for the author's own use only. The author is the only reader; the people described have not consented to being recorded, so record only what the note supports and nothing more.

Return one JSON object matching the schema. Use [] when there is nothing and "" when a value is unknown.

People
- The author is "ME". People in the supplied index are "P<id>". Names in the note may already have been replaced by these refs.
- Match people to the index before proposing anyone new. Someone not in the index gets a new ref "X1", "X2"... in order of appearance.
- List every ref you use (except ME) once in `people`, with the words the note used for them, match_confidence, and `candidates` (other P-refs it could be) when a name could fit more than one indexed person. Unnamed relatives ("my sister") are fine: name = "my sister", relation_to_author = "sister".

Evidence
- Every item quotes, verbatim, the sentence from the note that supports it (`quote`).
- Record observed behaviour and stated feelings. When an item reads intent into behaviour, set interpretation = true and lower confidence.
- Character judgements go only in `assessments`, rewritten as one testable behavioural pattern ("withdraws for days after being contradicted in public"). Never store labels ("passive-aggressive") or armchair diagnoses ("narcissist"); rewrite them or leave them out. Never put character judgements in people, edges, interactions or motives.
- The author's own behaviour goes in `self_observations`: misfire (something the author said or did that landed badly), win (landed well), prediction (an explicit forecast, with predicted_outcome).

Items
- interactions: one per act between two people: actor did `act` to target. score runs from -3 (open conflict) to +3 (strong support). witnessed = "me" if the author saw it; "relayed" if someone told the author (set relayed_by). Never write an interaction for a relationship in general; only for something that happened. "B sided with A" is actor B, target A.
- claims: anything someone told the author about other people is also a claim (source = who told them). credibility: 1 confirmed by another source, 2 probably true, 3 possibly true, 4 doubtful, 5 improbable, 6 cannot judge. If the note confirms or contradicts an open claim listed in the index, set relates_to and relation.
- edges: kin (from_ref is the <relation> of to_ref, e.g. P3 is the "mother" of ME), friendship, debt (from_ref owes to_ref), info-flow (from_ref tells to_ref things). Do not use edges for alliance or tension; those come from interactions.
- trust: who trusts whom, in which domain, from -2 (actively distrusts) to +2. The author's own trust has truster ME.
- key_dates: birthdays, anniversaries and bereavements (recurring = true) and one-off events: hospital, exam, interview, house move, new job, result. Resolve relative dates ("the 14th", "next Tuesday") against today's date. Only include a date you can pin to a day.
- gestures: what the author sent or gave someone, and how it landed if the note says.
- contacts: each time the author was in touch with someone (in person, call, message) and who initiated.
- motives: what someone wants (status, security, belonging, fairness, being right, autonomy, or other) or fears.
- profiles: only when the note shows how someone reacts to hypothetical or relativist arguments about ethics.
- topics: subjects that carry feeling. Reuse an indexed topic by ref. Give competing frames and, for each person, the frame they hold and how much they care (1-3).
- events: if the note is about one specific gathering, 1:1 or message thread, include it (reuse an open event by ref, otherwise "new").

Mirror
- Set `mirror` only when the AUTHOR is planning or rehearsing an argument that treats moral commitments as negotiable: trolley-problem edge cases, ends-justify-means, devil's advocate on values, "it's all relative". strength 1-3 is how clearly the note shows it. `line` is one sentence that names the pattern and its cost: most listeners don't hear it as analysis, they hear "under the right conditions, he'd do it to me". Suggest leading with the commitments the author actually holds, and keeping the exploration for people who enjoy it. Never comment on other people's morals. Otherwise pattern "none", strength 0, line "".

Summary
- `summary` is one line under 20 words, using refs rather than names.

Don't invent. If the note doesn't support an item, leave it out.
"""

REF_PERSON = re.compile(r"^(ME|P\d+|X\d+)$")


@dataclass
class ExtractionContext:
    today: date
    pseudo: Pseudonymiser
    index_text: str
    person_refs: set[str]
    topic_refs: set[str] = field(default_factory=set)
    claim_refs: set[str] = field(default_factory=set)
    assessment_refs: set[str] = field(default_factory=set)
    event_refs: set[str] = field(default_factory=set)
    deep_available: bool = False


def build_context(almanac: Almanac, vault: DeepVault, today: date, pseudonymise: bool) -> ExtractionContext:
    people = almanac.people(include_self=True)
    pseudo = Pseudonymiser(people, enabled=pseudonymise)
    self_id = almanac.self_id()
    relations = {}
    for r in almanac.query(
        "SELECT from_id, to_id, relation FROM family_links WHERE from_id = ? OR to_id = ?", (self_id, self_id)
    ):
        if r["to_id"] == self_id:
            relations[r["from_id"]] = f"author's {r['relation']}"
        else:
            relations[r["to_id"]] = f"the author is their {r['relation']}"
    lines = ["People:"]
    refs = {"ME"}
    for p in people:
        if p["is_self"]:
            continue
        refs.add(f"P{p['id']}")
        bits = [p["grp"] or "group unknown"]
        if p["id"] in relations:
            bits.append(relations[p["id"]])
        lines.append(f"- {pseudo.label(p['id'])}: " + "; ".join(bits))
    if len(lines) == 1:
        lines.append("- (none yet)")
    if pseudo.ambiguous:
        lines.append("Names shared by more than one indexed person (left as written in the note):")
        for name, ids in sorted(pseudo.ambiguous.items()):
            lines.append(f'- "{name}": ' + ", ".join(f"P{i}" for i in ids))

    event_refs = set()
    window_start = (today - timedelta(days=14)).isoformat()
    events = almanac.query(
        "SELECT * FROM events WHERE date IS NULL OR date >= ? ORDER BY date LIMIT 20", (window_start,)
    )
    if events:
        lines.append("Open events:")
        for e in events:
            event_refs.add(f"E{e['id']}")
            attendees = [
                f"P{r['person_id']}"
                for r in almanac.query("SELECT person_id FROM event_attendees WHERE event_id = ?", (e["id"],))
            ]
            title = pseudo.encode(e["title"])
            lines.append(f"- E{e['id']}: {title} ({e['date'] or 'date unknown'}; {e['type']}) attendees: {', '.join(attendees) or 'unknown'}")

    ctx = ExtractionContext(today=today, pseudo=pseudo, index_text="", person_refs=refs, event_refs=event_refs)
    if vault.is_open:
        ctx.deep_available = True
        store = vault.store
        topics = store.query("SELECT id, label, sensitivity, frames FROM topics ORDER BY last_seen DESC LIMIT 40")
        if topics:
            lines.append("Topics:")
            for t in topics:
                ctx.topic_refs.add(f"T{t['id']}")
                frames = " | ".join(jload(t["frames"], [])) or "no frames yet"
                lines.append(f"- T{t['id']}: {pseudo.encode(t['label'])} (sensitivity {t['sensitivity']}; frames: {frames})")
        claims = store.query(
            "SELECT id, source_person_id, text FROM claims WHERE status = 'open' ORDER BY id DESC LIMIT 30"
        )
        if claims:
            lines.append("Open claims (relayed, not yet confirmed or contradicted):")
            for c in claims:
                ctx.claim_refs.add(f"C{c['id']}")
                src = f"P{c['source_person_id']}" if c["source_person_id"] else "unknown"
                lines.append(f"- C{c['id']} (from {src}): {pseudo.encode(c['text'])}")
        assessments = store.query(
            "SELECT id, person_id, pattern FROM assessments WHERE retired = 0 ORDER BY last_seen DESC LIMIT 40"
        )
        if assessments:
            lines.append("Behavioural patterns on file:")
            for a in assessments:
                ctx.assessment_refs.add(f"A{a['id']}")
                lines.append(f"- A{a['id']} (P{a['person_id']}): {pseudo.encode(a['pattern'])}")
    ctx.index_text = "\n".join(lines)
    return ctx


def build_user_message(
    ctx: ExtractionContext,
    note_id: str,
    created_at: str,
    text: str,
    input_type: str,
    event_id: int | None,
    correction: str | None = None,
    previous_summary: str | None = None,
) -> str:
    when = datetime.fromisoformat(created_at)
    parts = [
        f"Today is {ctx.today.isoformat()} ({ctx.today.strftime('%A')}). Note {note_id} was written {when.strftime('%Y-%m-%d %H:%M')} UTC as {input_type}.",
    ]
    if event_id:
        parts.append(f"The author tagged this note to event E{event_id}.")
    parts.append("<index>\n" + ctx.index_text + "\n</index>")
    parts.append("<note>\n" + ctx.pseudo.encode(text) + "\n</note>")
    if correction:
        parts.append(
            "The author read the previous extraction of this note and says it was wrong. Re-extract the whole note, "
            "applying their correction. Their correction outranks your reading of the note.\n"
            + (f"<previous_reading>\n{ctx.pseudo.encode(previous_summary or '')}\n</previous_reading>\n" if previous_summary else "")
            + f"<correction>\n{ctx.pseudo.encode(correction)}\n</correction>"
        )
    return "\n\n".join(parts)


def validator(ctx: ExtractionContext):
    def check(ext: Extraction) -> None:
        problems: list[str] = []
        declared = {"ME"}
        for p in ext.people:
            if not REF_PERSON.match(p.ref) or p.ref == "ME":
                problems.append(f"people: bad ref {p.ref!r}")
            elif p.ref.startswith("P") and p.ref not in ctx.person_refs:
                problems.append(f"people: {p.ref} is not in the index; use an X ref for someone new")
            if p.ref in declared and p.ref != "ME":
                problems.append(f"people: {p.ref} listed twice")
            declared.add(p.ref)
            for cand in p.candidates:
                if cand not in ctx.person_refs:
                    problems.append(f"people: candidate {cand} is not in the index")

        def need(ref: str, where: str) -> None:
            if ref not in declared:
                problems.append(f"{where}: ref {ref!r} is not ME or listed in people")

        def need_date(value: str, where: str, required: bool = False) -> None:
            if not value:
                if required:
                    problems.append(f"{where}: date is required")
                return
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) or parse_date(value) is None:
                problems.append(f"{where}: date {value!r} is not YYYY-MM-DD")

        for i, x in enumerate(ext.interactions):
            need(x.actor, f"interactions[{i}].actor")
            need(x.target, f"interactions[{i}].target")
            if x.actor == x.target:
                problems.append(f"interactions[{i}]: actor and target are the same")
            if x.witnessed == "relayed" and x.relayed_by:
                need(x.relayed_by, f"interactions[{i}].relayed_by")
            need_date(x.date, f"interactions[{i}]")
        for i, x in enumerate(ext.edges):
            need(x.from_ref, f"edges[{i}].from_ref")
            need(x.to_ref, f"edges[{i}].to_ref")
            if x.from_ref == x.to_ref:
                problems.append(f"edges[{i}]: from_ref and to_ref are the same")
            if x.kind == "kin" and not x.relation:
                problems.append(f"edges[{i}]: kin edges need a relation")
        for i, x in enumerate(ext.trust):
            need(x.truster, f"trust[{i}].truster")
            need(x.trustee, f"trust[{i}].trustee")
        for i, x in enumerate(ext.claims):
            need(x.source, f"claims[{i}].source")
            for a in x.about:
                need(a, f"claims[{i}].about")
            if x.relates_to and x.relates_to not in ctx.claim_refs:
                problems.append(f"claims[{i}]: relates_to {x.relates_to} is not an open claim")
            if x.relates_to and x.relation == "none":
                problems.append(f"claims[{i}]: relates_to set but relation is none")
        for i, x in enumerate(ext.topics):
            if x.ref != "new" and x.ref not in ctx.topic_refs:
                problems.append(f"topics[{i}]: ref {x.ref} is not a supplied topic")
            for tp in x.people:
                need(tp.person, f"topics[{i}].people")
        for i, x in enumerate(ext.motives):
            need(x.person, f"motives[{i}].person")
        for i, x in enumerate(ext.key_dates):
            need(x.person, f"key_dates[{i}].person")
            need_date(x.date, f"key_dates[{i}]", required=True)
        for i, x in enumerate(ext.gestures):
            need(x.person, f"gestures[{i}].person")
            need_date(x.date, f"gestures[{i}]")
        for i, x in enumerate(ext.contacts):
            need(x.person, f"contacts[{i}].person")
            need_date(x.date, f"contacts[{i}]")
        for i, x in enumerate(ext.self_observations):
            for p in x.people:
                need(p, f"self_observations[{i}].people")
        for i, x in enumerate(ext.assessments):
            need(x.person, f"assessments[{i}].person")
            if x.person == "ME":
                problems.append(f"assessments[{i}]: the author's own behaviour belongs in self_observations")
            if x.existing and x.existing not in ctx.assessment_refs:
                problems.append(f"assessments[{i}]: existing {x.existing} is not a supplied pattern")
        for i, x in enumerate(ext.profiles):
            need(x.person, f"profiles[{i}].person")
        if len(ext.events) > 1:
            problems.append("events: at most one event per note")
        for i, x in enumerate(ext.events):
            if x.ref != "new" and x.ref not in ctx.event_refs:
                problems.append(f"events[{i}]: ref {x.ref} is not an open event")
            need_date(x.date, f"events[{i}]")
            for a in x.attendees:
                need(a, f"events[{i}].attendees")
        if ext.mirror.pattern == "none" and ext.mirror.strength:
            problems.append("mirror: strength must be 0 when pattern is none")
        if problems:
            raise ValueError("; ".join(problems[:25]))

    return check


def run_extraction(
    llm: LLM,
    ctx: ExtractionContext,
    *,
    note_id: str,
    created_at: str,
    text: str,
    input_type: str,
    event_id: int | None,
    effort: str,
    correction: str | None = None,
    previous_summary: str | None = None,
) -> Extraction:
    user = build_user_message(ctx, note_id, created_at, text, input_type, event_id, correction, previous_summary)
    return llm.structured(
        task=f"extract {note_id}",
        system=EXTRACTION_SYSTEM,
        user=user,
        schema=Extraction,
        effort=effort,
        validate=validator(ctx),
    )
