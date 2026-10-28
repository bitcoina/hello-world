"""Retrieval and analysis: context assembly, the four modes, the map and person readings."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

from .almanac import Almanac
from .config import Settings
from .db import jdump, jload, parse_date, utcnow
from .deep import DeepVault
from .graph import (
    CREDIBILITY_TEXT,
    SOURCE_GRADE_TEXT,
    assessment_confidence,
    assessment_status,
    interaction_rows_to_readings,
    signed_graph,
    source_grade,
    unbalanced_triads,
)
from .llm import LLM
from .mirror import MirrorGate
from .pseudo import Pseudonymiser
from .relationships import follow_ups_due, initiation_ledger, upcoming_dates
from .schemas import BriefOut, ExplainOut, RecordCheckOut, ReflectOut

BRIEF_WORD_LIMIT = 250
NOTE_HALF_LIFE_DAYS = 90

COMMON_RULES = """\
Use only the supplied context. It comes from the author's private record and may be incomplete or wrong.
People appear as refs ("P12"); the author is ME. Refer to people only by these refs, exactly as given.
Every claim cites the note IDs ("N12") it rests on, taken from the context. Never invent a note ID. If something isn't supported by a note, leave it out.
Items marked provisional rest on a single note: weight them lower and say "(provisional)" when you lean on one.
Keep facts and interpretations apart: say what was said and done, and label any reading of motive as a reading.
Never suggest manipulating anyone. The aim is understanding, and being a better guest, relative and friend.
"""

BRIEF_SYSTEM = COMMON_RULES + """
You are writing a short brief before an event or meeting. Order: fault lines, what each person probably wants (and the frame they hold on live topics), trust cautions, topics to avoid, seating or pairing cautions, one prediction the author can check afterwards, and an optional Mirror line.
- The whole brief must stay under 250 words: it is read on a phone in the car park. Fewer, sharper items beat coverage.
- Pressure points (unbalanced triads) are computed and shown separately; only mention one in fault lines if you add something.
- Trust cautions are concrete: "Don't share X in front of P7; P7 has passed on confidences before (N3, N9)."
- The prediction is one specific, checkable sentence about how the event will go.
- mirror_line: only when the context says mirror_allowed is yes AND it gives a reason (someone present with low tolerance for moral relativism, or a high-sensitivity topic that invites hypotheticals). One sentence that gives its reason, e.g. "Hold the trolley-problem reasoning for another audience tonight: P3 hears hypotheticals about loyalty as statements of intent." Never comment on other people's morals. Otherwise "".
- If assessments are supplied, you may use them; list each one you use in uses_assessments and add "(assessment A3)" to the sentence.
"""

EXPLAIN_SYSTEM = COMMON_RULES + """
You are explaining something that happened, in the spirit of Analysis of Competing Hypotheses: try to reject explanations rather than confirm one.
- what_happened: the facts, cited.
- explanations: at least two genuinely different explanations. For each, the notes that support it and the notes that cut against it, and a confidence.
- would_tell_apart: the new evidence that would separate the explanations (things the author could notice or ask, not tests to set anyone).
- mirror_line: only if the context says mirror_allowed is yes and a misfire in the context follows the author arguing an edge case; name the pattern and its cost in one sentence. Otherwise "".
"""

RECORD_SYSTEM = COMMON_RULES + """
You are checking the record against the notes supplied in focus.
- missing: conflicts, alliances, trust, dates or info routes these notes show that the stored record (listed with refs I<id> for interactions, D<id> for edges) doesn't hold.
- contradictions: stored items the notes contradict; set target to the stored item's ref.
- stale: stored items that look out of date and could be retired; set target.
Be conservative. An empty list is a fine answer.
"""

REFLECT_SYSTEM = COMMON_RULES + """
You are helping the author reflect on their own conduct over a period, using their misfires, wins, predictions and notes.
- patterns: recurring patterns in how the author's choices land, cited.
- misfire_themes: what the misfires have in common, cited.
- wins: what the wins have in common, cited.
- mirror_line: only if the context says mirror_allowed is yes and misfires follow the author arguing edge cases or treating values as negotiable; name the pattern and the trust cost in one sentence. Otherwise "".
Speak to the author plainly, as a thoughtful friend would. No scores, no gamification.
"""


@dataclass
class Context:
    text: str
    note_ids: set[str]
    pseudo: Pseudonymiser
    focus: set[int]
    scope: set[int]
    assessment_refs: set[str] = field(default_factory=set)
    stored_refs: set[str] = field(default_factory=set)
    mirror_reason: str | None = None
    summarised: int = 0


class Analyst:
    def __init__(self, settings: Settings, almanac: Almanac, vault: DeepVault, llm: LLM, mirror: MirrorGate):
        self.settings = settings
        self.almanac = almanac
        self.vault = vault
        self.llm = llm
        self.mirror = mirror

    def today(self) -> date:
        return datetime.now(self.settings.tz).date()

    # ------------------------------------------------------------- shared reads
    def interactions(self, directed: bool = False) -> dict:
        rows = self.vault.store.query(
            "SELECT i.*, t.label AS topic_label FROM interactions i LEFT JOIN topics t ON t.id = i.topic_id"
            " WHERE i.status != 'retired'"
        )
        return interaction_rows_to_readings(rows, self.today(), directed=directed)

    def friendships(self) -> list[tuple[int, int]]:
        return [(r["from_id"], r["to_id"]) for r in self.vault.store.query(
            "SELECT from_id, to_id FROM edges WHERE kind = 'friendship' AND status != 'retired'")]

    def triads(self, readings=None) -> list[tuple[int, int, int]]:
        readings = readings if readings is not None else self.interactions()
        return unbalanced_triads(signed_graph(readings, self.friendships()))

    def grades(self) -> dict[int, dict]:
        out = {}
        for r in self.vault.store.query(
            "SELECT source_person_id AS pid, sum(status = 'confirmed') AS ok, sum(status = 'contradicted') AS bad,"
            " count(*) AS n FROM claims WHERE source_person_id IS NOT NULL GROUP BY source_person_id"
        ):
            g = source_grade(r["ok"] or 0, r["bad"] or 0)
            out[r["pid"]] = {"grade": g, "text": SOURCE_GRADE_TEXT[g], "confirmed": r["ok"] or 0,
                             "contradicted": r["bad"] or 0, "claims": r["n"]}
        return out

    def activity(self) -> dict[int, str]:
        """Most recent note mentioning each person (ISO timestamp)."""
        out = {}
        if self.vault.is_open:
            for r in self.vault.store.query(
                "SELECT np.person_id, max(n.created_at) AS last FROM note_people np JOIN notes n ON n.id = np.note_id GROUP BY np.person_id"
            ):
                out[r["person_id"]] = r["last"]
        return out

    def tray(self) -> dict:
        act = self.activity()
        people = self.almanac.people()
        people.sort(key=lambda p: (act.get(p["id"]) or p["updated_at"] or ""), reverse=True)
        events = self.almanac.query("SELECT * FROM events ORDER BY coalesce(date, created_at) DESC LIMIT 30")
        return {
            "people": [{"id": p["id"], "name": p["display_name"], "group": p["grp"], "layer": p["layer"]} for p in people],
            "events": [{"id": e["id"], "title": e["title"], "date": e["date"], "type": e["type"]} for e in events],
        }

    # ------------------------------------------------------------ context build
    def assemble(self, person_ids: list[int], event_id: int | None, include_assessments: bool,
                 focus_note_ids: list[str] = (), budget_tokens: int = 24000, mode: str = "brief") -> Context:
        alm, store = self.almanac, self.vault.store
        today = self.today()
        pseudo = Pseudonymiser(alm.people(include_self=True), self.settings.pseudonymise)
        enc = pseudo.encode
        self_id = alm.self_id()
        focus = set(person_ids)
        lines: list[str] = [f"Today: {today.isoformat()} ({today.strftime('%A')})."]
        event = None
        if event_id:
            event = alm.one("SELECT * FROM events WHERE id = ?", (event_id,))
            if event:
                focus |= {r["person_id"] for r in alm.query("SELECT person_id FROM event_attendees WHERE event_id = ?", (event_id,))}
                where = f" at {enc(event['location'])}" if event["location"] else ""
                lines.append(f"Event E{event['id']}: {enc(event['title'])} on {event['date'] or 'date unknown'}{where} ({event['type']}).")
        for nid in focus_note_ids:
            focus |= {r["person_id"] for r in store.query("SELECT person_id FROM note_people WHERE note_id = ?", (nid,))}
        focus.discard(self_id)

        readings = self.interactions()
        directed = self.interactions(directed=True)
        adj: dict[int, set[int]] = defaultdict(set)
        for (a, b), r in readings.items():
            if abs(r.value) >= 0.5:
                adj[a].add(b)
                adj[b].add(a)
        brokers = {x for x, ns in adj.items() if x not in focus and x != self_id and len(ns & focus) >= 2}
        scope = focus | brokers | {self_id}
        L = pseudo.label
        lines.append("In focus: " + (", ".join(L(p) for p in sorted(focus)) or "nobody specific") + ".")
        if brokers:
            lines.append("Not selected but connected to two or more of them: " + ", ".join(L(p) for p in sorted(brokers)) + ".")

        grades = self.grades()
        profiles = {r["person_id"]: r for r in store.query("SELECT * FROM person_profiles")}
        mirror_reasons = []
        lines.append("\n## People")
        for pid in sorted(focus):
            p = alm.person(pid)
            if not p:
                continue
            bits = [p["grp"] or "group unknown"]
            if p["layer"]:
                bits.append(f"closeness layer {p['layer']}")
            if pid in grades:
                g = grades[pid]
                bits.append(f"source grade {g['grade']} ({g['text']}; {g['confirmed']} confirmed, {g['contradicted']} contradicted)")
            prof = profiles.get(pid)
            if prof and prof["relativism_tolerance"]:
                bits.append(f"tolerance for moral relativism: {prof['relativism_tolerance']}")
                if prof["relativism_tolerance"] == "low":
                    mirror_reasons.append(f"{L(pid)} has low tolerance for moral relativism")
            lines.append(f"- {L(pid)}: " + "; ".join(bits))
            for m in store.query("SELECT * FROM motives WHERE person_id = ? ORDER BY last_seen DESC LIMIT 6", (pid,)):
                lines.append(f"  - {m['kind']}s {m['category']}: {enc(m['detail'])} ({m['status']}; {', '.join(jload(m['source_note_ids'], []))})")
            for f in store.query(
                "SELECT pt.*, t.label, t.sensitivity FROM person_topics pt JOIN topics t ON t.id = pt.topic_id WHERE pt.person_id = ?",
                (pid,),
            ):
                lines.append(f"  - on T{f['topic_id']} ({enc(f['label'])}): frame {enc(f['frame'] or 'unknown')}, cares {f['salience']}/3 ({', '.join(jload(f['source_note_ids'], []))})")
            for t in store.query("SELECT * FROM trust WHERE truster_id = ? AND trustee_id = ?", (self_id, pid)):
                lines.append(f"  - ME trusts them with {t['domain']}: {t['level']:+d} ({t['basis']}; {', '.join(jload(t['source_note_ids'], []))})")
            for d in upcoming_dates(alm, today, 21, {pid}):
                lines.append(f"  - upcoming: {d['label']} on {d['date']}" + (f" ({', '.join(d['note_ids'])})" if d["note_ids"] else ""))
            for fu in follow_ups_due(alm, today, {pid}):
                lines.append(f"  - follow-up due {fu['due']}: {enc(fu['prompt'])}")

        lines.append("\n## Relationships (derived from logged interactions; 6-month half-life)")
        rel_lines = 0
        for (a, b), r in sorted(readings.items(), key=lambda kv: -abs(kv[1].value)):
            if a in scope and b in scope and (a in focus or b in focus) and abs(r.value) >= 0.3:
                topics = f"; over {', '.join(enc(t) for t in r.topics)}" if r.topics else ""
                ab, ba = directed.get((a, b)), directed.get((b, a))
                asym = ""
                if ab and ba and ab.sign and ba.sign and ab.sign != ba.sign:
                    asym = f"; asymmetric: {L(a)}→{L(b)} {'+' if ab.sign > 0 else '−'}, {L(b)}→{L(a)} {'+' if ba.sign > 0 else '−'}"
                lines.append(f"- {L(a)}–{L(b)}: {r.weather}, {r.trend}; score {r.value:+.1f} from {r.count} interactions{topics}{asym} ({r.status}; {', '.join(sorted(r.notes))})")
                rel_lines += 1
        if not rel_lines:
            lines.append("- none recorded")

        recent = store.query(
            "SELECT i.*, t.label AS topic_label FROM interactions i LEFT JOIN topics t ON t.id = i.topic_id"
            " WHERE i.status != 'retired' ORDER BY i.date DESC LIMIT 400"
        )
        shown = [r for r in recent if r["actor_id"] in scope and r["target_id"] in scope and
                 (r["actor_id"] in focus or r["target_id"] in focus)][:20]
        if shown:
            lines.append("\n## Recent interactions")
            for r in shown:
                via = f", relayed by {L(r['relayed_by_id'])}" if r["relayed_by_id"] else ""
                topic = f" over {enc(r['topic_label'])}" if r["topic_label"] else ""
                interp = ", interpretation" if r["interpretation"] else ""
                lines.append(f"- I{r['id']} {r['date']}: {L(r['actor_id'])} {r['act']} {L(r['target_id'])}{topic} ({r['score']:+d}{via}{interp}; {r['status']}; {', '.join(jload(r['source_note_ids'], []))})")

        edges = [e for e in store.query("SELECT * FROM edges WHERE status != 'retired'")
                 if e["from_id"] in scope and e["to_id"] in scope]
        if edges:
            lines.append("\n## Other links")
            for e in edges:
                detail = f": {enc(e['detail'])}" if e["detail"] else ""
                lines.append(f"- D{e['id']} {L(e['from_id'])} → {L(e['to_id'])} {e['kind']}{detail} ({e['status']}; {', '.join(jload(e['source_note_ids'], []))})")
        kin = alm.query("SELECT * FROM family_links")
        kin = [k for k in kin if k["from_id"] in scope and k["to_id"] in scope]
        if kin:
            lines.append("\n## Family")
            for k in kin:
                lines.append(f"- {L(k['from_id'])} is {L(k['to_id'])}'s {k['relation']}")

        trust = [t for t in store.query("SELECT * FROM trust") if t["truster_id"] in scope and t["trustee_id"] in scope
                 and t["truster_id"] != self_id]
        if trust:
            lines.append("\n## Trust between them")
            for t in trust:
                lines.append(f"- {L(t['truster_id'])} trusts {L(t['trustee_id'])} with {t['domain']}: {t['level']:+d} ({t['basis']}; {t['status']}; {', '.join(jload(t['source_note_ids'], []))})")

        claims = [c for c in store.query("SELECT * FROM claims WHERE status = 'open' ORDER BY id DESC LIMIT 200")
                  if c["source_person_id"] in focus or set(jload(c["about_person_ids"], [])) & focus]
        if claims:
            lines.append("\n## Unconfirmed claims")
            for c in claims[:12]:
                src = L(c["source_person_id"]) if c["source_person_id"] else "unknown"
                lines.append(f"- C{c['id']} from {src} ({CREDIBILITY_TEXT[c['credibility']]}; {c['note_id']}): {enc(c['text'])}")

        triads = [t for t in unbalanced_triads(signed_graph(readings, self.friendships()))
                  if len(set(t) & (focus | {self_id})) >= 2]
        if triads:
            lines.append("\n## Pressure points (unbalanced triads)")
            for t in triads[:8]:
                lines.append("- " + ", ".join(L(x) for x in t))

        topic_rows = store.query(
            "SELECT DISTINCT t.* FROM topics t JOIN person_topics pt ON pt.topic_id = t.id WHERE pt.person_id IN (%s)"
            % ",".join("?" * len(focus)) if focus else "SELECT * FROM topics WHERE 0",
            tuple(focus),
        )
        if topic_rows:
            lines.append("\n## Topics")
            for t in topic_rows:
                frames = " | ".join(enc(f) for f in jload(t["frames"], [])) or "no frames yet"
                lines.append(f"- T{t['id']} {enc(t['label'])}: sensitivity {t['sensitivity']}/3; frames: {frames} ({t['status']})")
                if t["sensitivity"] >= 3:
                    mirror_reasons.append(f"high-sensitivity topic T{t['id']} is live")

        obs = store.query("SELECT * FROM self_observations ORDER BY created_at DESC LIMIT 200")
        obs = [o for o in obs if set(jload(o["person_ids"], [])) & focus or (o["event_id"] and o["event_id"] == event_id)][:10]
        if obs:
            lines.append("\n## The author's own record with them")
            for o in obs:
                extra = ""
                if o["kind"] == "prediction":
                    extra = f"; predicted: {enc(o['predicted_outcome'] or '')}; scored: {o['score'] if o['score'] is not None else 'not yet'}"
                lines.append(f"- {o['kind']} ({o['note_id'] or 'brief'}): {enc(o['description'])}{extra}")

        assessment_refs: set[str] = set()
        if include_assessments:
            rows = store.query("SELECT * FROM assessments WHERE retired = 0")
            rows = [r for r in rows if r["person_id"] in focus]
            if rows:
                lines.append("\n## Assessments (private patterns; use sparingly and mark each use)")
                for r in rows:
                    sup, con = jload(r["supporting_note_ids"], []), jload(r["counter_note_ids"], [])
                    status = assessment_status(len(sup), len(con), False)
                    assessment_refs.add(f"A{r['id']}")
                    lines.append(f"- A{r['id']} {L(r['person_id'])}: {enc(r['pattern'])} ({status}; for {', '.join(sup)}; against {', '.join(con) or 'none'})")

        mirror_allowed = bool(self.mirror.eligible_patterns()) and bool(mirror_reasons) if mode == "brief" else bool(self.mirror.eligible_patterns())
        lines.append(f"\nmirror_allowed: {'yes' if mirror_allowed else 'no'}" + (f" ({'; '.join(mirror_reasons)})" if mirror_reasons and mirror_allowed else ""))

        # Notes, ranked by recency and how charged they are for this group.
        intensity: dict[str, float] = defaultdict(float)
        for r in recent:
            if r["actor_id"] in focus or r["target_id"] in focus:
                for nid in jload(r["source_note_ids"], []):
                    intensity[nid] += abs(r["score"])
        candidates: dict[str, dict] = {}
        if focus:
            for n in store.query(
                "SELECT DISTINCT n.* FROM notes n JOIN note_people np ON np.note_id = n.id WHERE np.person_id IN (%s)"
                % ",".join("?" * len(focus)),
                tuple(focus),
            ):
                candidates[n["id"]] = dict(n)
        if event_id:
            for n in store.query("SELECT * FROM notes WHERE event_id = ?", (event_id,)):
                candidates[n["id"]] = dict(n)
        for nid in focus_note_ids:
            row = store.one("SELECT * FROM notes WHERE id = ?", (nid,))
            if row:
                candidates[nid] = dict(row)

        def rank(n: dict) -> float:
            age = (today - datetime.fromisoformat(n["created_at"]).date()).days
            return (100 if n["id"] in focus_note_ids else 0) + 0.5 ** (age / NOTE_HALF_LIFE_DAYS) + 0.3 * intensity[n["id"]]

        ordered = sorted(candidates.values(), key=rank, reverse=True)
        budget = budget_tokens * 4 - sum(len(x) for x in lines)
        full_budget = int(budget * 0.75)
        note_lines, summary_lines, used, summarised, omitted = [], [], 0, 0, 0
        for n in ordered:
            day = n["created_at"][:10]
            full = f"<note id=\"{n['id']}\" date=\"{day}\">\n{enc(n['raw_text'])}\n</note>"
            if used + len(full) <= full_budget:
                note_lines.append(full)
                used += len(full)
                continue
            short = f"- {n['id']} ({day}): {enc(n['summary'] or n['raw_text'][:160])}"
            if used + len(short) <= budget:
                summary_lines.append(short)
                used += len(short)
                summarised += 1
            else:
                omitted += 1
        valid = {n["id"] for n in ordered}
        lines.append("\n## Notes")
        lines.extend(note_lines or ["(no notes)"])
        if summary_lines:
            lines.append(f"\nOlder notes, summarised to fit ({summarised}):")
            lines.extend(summary_lines)
        if omitted:
            lines.append(f"\n({omitted} older notes did not fit and are not shown.)")
        stored = {f"I{r['id']}" for r in shown} | {f"D{e['id']}" for e in edges}
        return Context("\n".join(lines), valid, pseudo, focus, scope, assessment_refs, stored,
                       "; ".join(mirror_reasons) or None, summarised)

    # ------------------------------------------------------------------ helpers
    def _cited(self, items, ctx: Context, require: bool = True) -> tuple[list[dict], int]:
        out, dropped = [], 0
        for it in items:
            cites = [n for n in dict.fromkeys(it.note_ids) if n in ctx.note_ids]
            if require and not cites:
                dropped += 1
                continue
            out.append({"text": ctx.pseudo.decode(_me(it.text)), "cites": cites})
        return out, dropped

    def _mirror(self, line: str, context: str, pattern: str = "edge_case_argument", brief_id: int | None = None) -> dict | None:
        if not line or not self.mirror.allowed(pattern):
            return None
        self.mirror.record_shown()
        with self.vault.tx() as c:
            cur = c.execute(
                "INSERT INTO mirror_log (created_at, context, pattern, text, brief_id) VALUES (?, ?, ?, ?, ?)",
                (utcnow(), context, pattern, line, brief_id),
            )
        return {"id": cur.lastrowid, "text": line}

    def _store_brief(self, mode: str, event_id, person_ids, query, output: dict, used_assessments: bool) -> int:
        with self.vault.tx() as c:
            cur = c.execute(
                "INSERT INTO briefs (mode, created_at, event_id, person_ids, query, output, used_assessments) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (mode, utcnow(), event_id, jdump(sorted(person_ids)), query, jdump(output), int(used_assessments)),
            )
            return cur.lastrowid

    def _finish(self, mode, event_id, person_ids, query, output, used_assessments) -> dict:
        bid = self._store_brief(mode, event_id, person_ids, query, output, used_assessments)
        output["id"] = bid
        if output.get("mirror_line"):
            output["mirror"] = self._mirror(output.pop("mirror_line"), mode, brief_id=bid)
        else:
            output.pop("mirror_line", None)
            output["mirror"] = None
        with self.vault.tx() as c:
            c.execute("UPDATE briefs SET output = ? WHERE id = ?", (jdump(output), bid))
        return output

    # -------------------------------------------------------------------- modes
    def brief(self, person_ids: list[int], event_id: int | None, query: str, deep: bool) -> dict:
        ctx = self.assemble(person_ids, event_id, deep, mode="brief")
        if not ctx.focus:
            raise ValueError("Pick an event or at least one person")
        user = ctx.text + (f"\n\nThe author asks: {ctx.pseudo.encode(query)}" if query else "") + \
            "\n\nWrite the brief."

        def check(out: BriefOut) -> None:
            bad = [a for a in out.uses_assessments if a not in ctx.assessment_refs]
            if bad:
                raise ValueError(f"uses_assessments lists refs that were not supplied: {bad}")

        out = self.llm.structured(task="brief", system=BRIEF_SYSTEM, user=user, schema=BriefOut,
                                  effort=self.settings.effort_brief, validate=check)
        today = self.today()
        sections, dropped = [], 0
        for title, items in (("Fault lines", out.fault_lines), ("Trust cautions", out.trust_cautions),
                             ("Topics to avoid", out.avoid), ("Seating and pairing", out.seating)):
            cited, d = self._cited(items, ctx)
            dropped += d
            if cited:
                sections.append({"title": title, "items": cited})
        wants = []
        for w in out.wants:
            cites = [n for n in w.note_ids if n in ctx.note_ids]
            if not cites:
                dropped += 1
                continue
            frame = f" Frame: {w.frame}." if w.frame else ""
            wants.append({"text": ctx.pseudo.decode(_me(f"{w.person}: {w.wants}.{frame}")), "cites": cites})
        if wants:
            sections.insert(1 if sections and sections[0]["title"] == "Fault lines" else 0, {"title": "What each wants", "items": wants})
        pressure = self.pressure_points(ctx)
        if pressure:
            sections.insert(1, {"title": "Low pressure", "items": pressure})
        follow = [{"text": f"{f['prompt']} (due {f['due']})", "cites": []} for f in follow_ups_due(self.almanac, today, ctx.focus)]
        if follow:
            sections.append({"title": "Remember to ask", "items": follow})
        prediction, _ = self._cited([out.prediction], ctx, require=False)
        words = sum(len(i["text"].split()) for s in sections for i in s["items"]) + len(prediction[0]["text"].split())
        output = {
            "mode": "brief",
            "event_id": event_id,
            "sections": sections,
            "prediction": prediction[0],
            "mirror_line": ctx.pseudo.decode(_me(out.mirror_line)) if ctx.mirror_reason else "",
            "assessments_used": sorted(set(out.uses_assessments)),
            "dropped_uncited": dropped,
            "summarised_notes": ctx.summarised,
            "words": words,
        }
        return self._finish("brief", event_id, ctx.focus, query, output, deep)

    def pressure_points(self, ctx: Context) -> list[dict]:
        readings = self.interactions()
        self_id = self.almanac.self_id()
        names = self.almanac.names()
        names[self_id] = "you"
        out = []
        for a, b, c in self.triads(readings):
            if len(set((a, b, c)) & (ctx.focus | {self_id})) < 2:
                continue
            cites, parts = set(), []
            for x, y in ((a, b), (a, c), (b, c)):
                r = readings.get((min(x, y), max(x, y)))
                sign = "+" if (r and r.sign > 0) or not r else "−"
                parts.append(f"{names.get(x, '?')}–{names.get(y, '?')} {sign}")
                if r:
                    cites |= r.notes
            out.append({"text": "Unbalanced: " + ", ".join(parts) + ". Tension is building here; easy to be pulled into the middle.",
                        "cites": sorted(cites, key=lambda n: int(n[1:]))})
        return out[:4]

    def explain(self, person_ids: list[int], event_id: int | None, note_ids: list[str], query: str, deep: bool) -> dict:
        ctx = self.assemble(person_ids, event_id, deep, focus_note_ids=note_ids, mode="explain")
        if not (ctx.focus or note_ids or query):
            raise ValueError("Pick people, an event or a note, or ask a question")
        user = ctx.text + (f"\n\nQuestion: {ctx.pseudo.encode(query)}" if query else "\n\nExplain what happened.")

        def check(out: ExplainOut) -> None:
            if len(out.explanations) < 2:
                raise ValueError("Give at least two competing explanations")

        out = self.llm.structured(task="explain", system=EXPLAIN_SYSTEM, user=user, schema=ExplainOut,
                                  effort=self.settings.effort_analysis, validate=check)
        happened, dropped = self._cited(out.what_happened, ctx)
        hyps = []
        for h in out.explanations:
            hyps.append({
                "text": ctx.pseudo.decode(_me(h.explanation)),
                "supporting": [n for n in h.supporting if n in ctx.note_ids],
                "contradicting": [n for n in h.contradicting if n in ctx.note_ids],
                "confidence": h.confidence,
            })
        output = {
            "mode": "explain",
            "event_id": event_id,
            "sections": [{"title": "What happened", "items": happened}],
            "explanations": hyps,
            "tell_apart": [ctx.pseudo.decode(_me(t)) for t in out.would_tell_apart],
            "mirror_line": ctx.pseudo.decode(_me(out.mirror_line)),
            "dropped_uncited": dropped,
            "summarised_notes": ctx.summarised,
        }
        return self._finish("explain", event_id, ctx.focus, query, output, deep)

    def record_check(self, note_ids: list[str]) -> dict:
        store = self.vault.store
        if not note_ids:
            note_ids = [r["id"] for r in store.query("SELECT id FROM notes ORDER BY seq DESC LIMIT 8")]
        ctx = self.assemble([], None, False, focus_note_ids=note_ids, mode="record")
        user = ctx.text + "\n\nNotes in focus: " + ", ".join(note_ids) + "\n\nCheck the record."
        out = self.llm.structured(task="record check", system=RECORD_SYSTEM, user=user, schema=RecordCheckOut,
                                  effort=self.settings.effort_analysis)

        def conv(items):
            res = []
            for it in items:
                res.append({
                    "text": ctx.pseudo.decode(_me(it.text)),
                    "cites": [n for n in it.note_ids if n in ctx.note_ids],
                    "target": it.target if it.target in ctx.stored_refs else None,
                })
            return res

        output = {
            "mode": "record",
            "sections": [
                {"title": "Not yet recorded", "items": conv(out.missing)},
                {"title": "Contradicts the record", "items": conv(out.contradictions)},
                {"title": "Stale, could retire", "items": conv(out.stale)},
            ],
            "note_ids": note_ids,
            "mirror_line": "",
        }
        return self._finish("record", None, ctx.focus, "", output, False)

    def reflect(self, start: date, end: date) -> dict:
        store = self.vault.store
        stats = self.reflect_stats(start, end)
        obs = store.query(
            "SELECT * FROM self_observations WHERE substr(created_at, 1, 10) BETWEEN ? AND ? ORDER BY created_at",
            (start.isoformat(), end.isoformat()),
        )
        notes = store.query(
            "SELECT id, created_at, summary, raw_text FROM notes WHERE substr(created_at, 1, 10) BETWEEN ? AND ? ORDER BY seq",
            (start.isoformat(), end.isoformat()),
        )
        output = {"mode": "reflect", "start": start.isoformat(), "end": end.isoformat(), "stats": stats,
                  "sections": [], "mirror_line": ""}
        if obs or notes:
            pseudo = Pseudonymiser(self.almanac.people(include_self=True), self.settings.pseudonymise)
            lines = [f"Period: {start} to {end}.", "\n## The author's own observations"]
            for o in obs:
                extra = ""
                if o["kind"] == "prediction":
                    extra = f" | predicted: {pseudo.encode(o['predicted_outcome'] or '')} | actual: {pseudo.encode(o['actual_outcome'] or 'not recorded')} | score: {o['score']}"
                lines.append(f"- {o['kind']} ({o['note_id'] or 'brief'}): {pseudo.encode(o['description'])}{extra}")
            mirror_rows = store.query(
                "SELECT pattern, rating FROM mirror_log WHERE substr(created_at, 1, 10) BETWEEN ? AND ?",
                (start.isoformat(), end.isoformat()),
            )
            if mirror_rows:
                lines.append("\n## Mirror warnings shown")
                for m in mirror_rows:
                    lines.append(f"- {m['pattern']} (rated {m['rating'] or 'unrated'})")
            lines.append("\n## Notes in the period (summaries)")
            for n in notes[-150:]:
                lines.append(f"- {n['id']} ({n['created_at'][:10]}): {pseudo.encode(n['summary'] or n['raw_text'][:200])}")
            allowed = bool(self.mirror.eligible_patterns())
            lines.append(f"\nmirror_allowed: {'yes' if allowed else 'no'}")
            ctx = Context("\n".join(lines), {n["id"] for n in notes} | {o["note_id"] for o in obs if o["note_id"]},
                          pseudo, set(), set())
            out = self.llm.structured(task="reflect", system=REFLECT_SYSTEM, user=ctx.text + "\n\nReflect.",
                                      schema=ReflectOut, effort=self.settings.effort_analysis)
            for title, items in (("Patterns", out.patterns), ("Misfires", out.misfire_themes), ("Wins", out.wins)):
                cited, _ = self._cited(items, ctx)
                if cited:
                    output["sections"].append({"title": title, "items": cited})
            output["mirror_line"] = pseudo.decode(_me(out.mirror_line))
        return self._finish("reflect", None, set(), "", output, False)

    def reflect_stats(self, start: date, end: date) -> dict:
        store = self.vault.store
        scored = store.query("SELECT created_at, score FROM self_observations WHERE kind = 'prediction' AND score IS NOT NULL ORDER BY created_at")
        by_month: dict[str, list[float]] = defaultdict(list)
        for r in scored:
            by_month[r["created_at"][:7]].append(r["score"])
        series = [{"month": m, "accuracy": round(sum(v) / len(v), 2), "n": len(v)} for m, v in sorted(by_month.items())]
        in_range = [r["score"] for r in scored if start.isoformat() <= r["created_at"][:10] <= end.isoformat()]
        counts = {r["kind"]: r["n"] for r in store.query(
            "SELECT kind, count(*) AS n FROM self_observations WHERE substr(created_at, 1, 10) BETWEEN ? AND ? GROUP BY kind",
            (start.isoformat(), end.isoformat()))}
        misfires = [dict(r) for r in store.query(
            "SELECT id, note_id, kind, description, created_at FROM self_observations WHERE kind IN ('misfire', 'win')"
            " AND substr(created_at, 1, 10) BETWEEN ? AND ? ORDER BY created_at DESC LIMIT 30",
            (start.isoformat(), end.isoformat()))]
        names = self.almanac.names()
        bias = []
        for a in store.query("SELECT * FROM assessments WHERE retired = 0"):
            sup, con = jload(a["supporting_note_ids"], []), jload(a["counter_note_ids"], [])
            if con:
                status = assessment_status(len(sup), len(con), False)
                bias.append({"id": a["id"], "person": names.get(a["person_id"], "?"), "pattern": a["pattern"],
                             "supporting": sup, "counter": con, "status": status})
        open_preds = [dict(r) for r in store.query(
            "SELECT id, description, predicted_outcome, event_id, created_at FROM self_observations"
            " WHERE kind = 'prediction' AND score IS NULL ORDER BY created_at DESC LIMIT 10")]
        mirror = store.one("SELECT sum(rating = 'useful') AS useful, sum(rating = 'not_useful') AS not_useful, count(*) AS shown FROM mirror_log")
        return {
            "accuracy": round(sum(in_range) / len(in_range), 2) if in_range else None,
            "scored": len(in_range),
            "series": series,
            "counts": counts,
            "observations": misfires,
            "ledger": initiation_ledger(self.almanac, start, end),
            "bias_check": sorted(bias, key=lambda b: b["status"] != "flag for retirement"),
            "open_predictions": open_preds,
            "mirror": {k: (mirror[k] or 0) for k in ("useful", "not_useful", "shown")} if mirror else {},
        }

    # ------------------------------------------------------------- map & people
    def map_data(self, event_id: int | None) -> dict:
        readings = self.interactions()
        self_id = self.almanac.self_id()
        names = self.almanac.names()
        people = {p["id"]: p for p in self.almanac.people(include_self=True)}
        allowed = None
        if event_id:
            allowed = {r["person_id"] for r in self.almanac.query("SELECT person_id FROM event_attendees WHERE event_id = ?", (event_id,))} | {self_id}
        edges, used = [], set()
        for (a, b), r in readings.items():
            if abs(r.value) < 0.3 or (allowed and not {a, b} <= allowed):
                continue
            edges.append({"id": f"r{a}-{b}", "source": a, "target": b, "kind": "relationship", "value": round(r.value, 2),
                          "trend": r.trend, "weather": r.weather, "width": round(min(9.0, 1 + abs(r.value) * 1.5), 1),
                          "status": r.status, "notes": sorted(r.notes, key=lambda n: int(n[1:]))})
            used |= {a, b}
        trust = defaultdict(dict)
        for t in self.vault.store.query("SELECT truster_id, trustee_id, avg(level) AS lvl FROM trust GROUP BY truster_id, trustee_id"):
            trust[(t["truster_id"], t["trustee_id"])] = t["lvl"]
        seen = set()
        for (a, b), lvl in trust.items():
            key = (min(a, b), max(a, b))
            if key in seen or (allowed and not {a, b} <= allowed):
                continue
            seen.add(key)
            back = trust.get((b, a))
            gap = lvl < 0 or (back is not None and back < 0) or (back is not None and abs(lvl - back) >= 2)
            if gap:
                edges.append({"id": f"t{key[0]}-{key[1]}", "source": key[0], "target": key[1], "kind": "trust-gap"})
                used |= {a, b}
        for e in self.vault.store.query("SELECT * FROM edges WHERE kind = 'friendship' AND status != 'retired'"):
            if allowed and not {e["from_id"], e["to_id"]} <= allowed:
                continue
            edges.append({"id": f"f{e['id']}", "source": e["from_id"], "target": e["to_id"], "kind": "friendship"})
            used |= {e["from_id"], e["to_id"]}
        if allowed:
            used |= allowed
        grades = self.grades()
        nodes = [{"id": pid, "label": "You" if pid == self_id else names.get(pid, "?"), "self": pid == self_id,
                  "layer": people.get(pid, {}).get("layer"), "group": people.get(pid, {}).get("grp"),
                  "grade": grades.get(pid, {}).get("grade")} for pid in sorted(used) if pid in names]
        triads = [list(t) for t in self.triads(readings) if not allowed or set(t) <= allowed]
        return {"nodes": nodes, "edges": edges, "triads": triads}

    def person_deep(self, pid: int) -> dict:
        store = self.vault.store
        self_id = self.almanac.self_id()
        names = self.almanac.names()
        names[self_id] = "You"
        readings = self.interactions()
        weather = []
        for (a, b), r in readings.items():
            if pid in (a, b):
                other = b if a == pid else a
                weather.append({"person_id": other, "person": names.get(other, "?"), **r.as_dict()})
        weather.sort(key=lambda w: -abs(w["value"]))
        radar = {d: {"mine": None, "theirs": None} for d in ("secrets", "money", "practical help", "emotional support", "judgement")}
        for t in store.query("SELECT * FROM trust WHERE (truster_id = ? AND trustee_id = ?) OR (truster_id = ? AND trustee_id = ?)",
                             (self_id, pid, pid, self_id)):
            radar[t["domain"]]["mine" if t["truster_id"] == self_id else "theirs"] = t["level"]
        notes = [dict(r) for r in store.query(
            "SELECT n.id, n.created_at, n.summary, n.raw_text, n.input_type FROM notes n JOIN note_people np ON np.note_id = n.id"
            " WHERE np.person_id = ? ORDER BY n.seq DESC LIMIT 12", (pid,))]
        motives = [dict(r) | {"source_note_ids": jload(r["source_note_ids"], [])} for r in store.query(
            "SELECT * FROM motives WHERE person_id = ? ORDER BY last_seen DESC", (pid,))]
        frames = [dict(r) | {"source_note_ids": jload(r["source_note_ids"], [])} for r in store.query(
            "SELECT pt.*, t.label, t.sensitivity FROM person_topics pt JOIN topics t ON t.id = pt.topic_id WHERE pt.person_id = ?", (pid,))]
        claims = [dict(r) for r in store.query(
            "SELECT id, note_id, text, credibility, status FROM claims WHERE source_person_id = ? ORDER BY id DESC LIMIT 10", (pid,))]
        prof = store.one("SELECT relativism_tolerance FROM person_profiles WHERE person_id = ?", (pid,))
        n_assess = store.scalar("SELECT count(*) FROM assessments WHERE person_id = ? AND retired = 0", (pid,), 0)
        grade = self.grades().get(pid, {"grade": "F", "text": SOURCE_GRADE_TEXT["F"], "confirmed": 0, "contradicted": 0, "claims": 0})
        return {
            "weather": weather,
            "trust_radar": radar,
            "notes": notes,
            "motives": motives,
            "frames": frames,
            "claims": [c | {"credibility_text": CREDIBILITY_TEXT[c["credibility"]]} for c in claims],
            "source_grade": grade,
            "relativism_tolerance": prof["relativism_tolerance"] if prof else None,
            "assessment_count": n_assess,
        }

    def assessments(self, pid: int) -> list[dict]:
        today = self.today()
        out = []
        for a in self.vault.store.query("SELECT * FROM assessments WHERE person_id = ? ORDER BY retired, last_seen DESC", (pid,)):
            sup, con = jload(a["supporting_note_ids"], []), jload(a["counter_note_ids"], [])
            last = parse_date(a["last_seen"])
            out.append({
                "id": a["id"], "pattern": a["pattern"], "supporting": sup, "counter": con, "your_take": a["your_take"],
                "status": assessment_status(len(sup), len(con), bool(a["retired"])),
                "confidence": assessment_confidence(len(sup), len(con), last, today),
                "first_seen": a["first_seen"][:10], "last_seen": a["last_seen"][:10],
            })
        return out


def _me(text: str) -> str:
    import re

    return re.sub(r"\bME\b", "you", text or "")
