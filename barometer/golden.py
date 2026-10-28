"""Golden notes: the extraction regression set.

``barometer golden`` runs every case in golden/cases.jsonl (plus, optionally, the
corrections you've made in the app) through the real extraction pipeline against a
throwaway data directory, and reports the share that needed no correction. The M2
bar is 90%.
"""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .commit import Resolution
from .schemas import Extraction

LABEL_WORDS = re.compile(r"\b(narcissist\w*|psychopath\w*|sociopath\w*|passive[- ]aggressive|toxic|bipolar|borderline|manipulative)\b", re.I)


def canonical(ext: Extraction, res: Resolution, names: dict[int, str]) -> dict:
    """A compact, name-based view of an extraction, used as a golden expectation."""

    def n(ref: str) -> str:
        if ref == "ME":
            return "ME"
        return names.get(res.refs.get(ref, -1), ref)

    return {
        "people": sorted({n(p.ref) for p in ext.people}),
        "interactions": [{"actor": n(x.actor), "target": n(x.target), "sign": (x.score > 0) - (x.score < 0)} for x in ext.interactions],
        "key_dates": [{"person": n(k.person), "kind": k.kind, "date": k.date} for k in ext.key_dates],
        "trust": [{"truster": n(t.truster), "trustee": n(t.trustee), "domain": t.domain, "sign": (t.level > 0) - (t.level < 0)} for t in ext.trust],
        "self_observations": [s.kind for s in ext.self_observations],
        "contacts": [{"person": n(c.person), "initiated_by": c.initiated_by} for c in ext.contacts],
        "gestures": [{"person": n(g.person)} for g in ext.gestures],
        "mirror": ext.mirror.pattern if ext.mirror.pattern != "none" else None,
    }


@dataclass
class CaseResult:
    id: str
    passed: bool
    problems: list[str]


def _subset_problems(expect: dict, got: dict) -> list[str]:
    problems = []
    for name in expect.get("people", []):
        if name not in got["people"]:
            problems.append(f"missing person {name}")
    for want in expect.get("interactions", []):
        if not any(g["actor"] == want["actor"] and g["target"] == want["target"] and g["sign"] == want["sign"] for g in got["interactions"]):
            problems.append(f"missing interaction {want}")
    for want in expect.get("key_dates", []):
        yearly = want.get("kind") in ("birthday", "anniversary")
        if not any(
            g["person"] == want["person"]
            and want.get("kind", "any") in ("any", g["kind"])
            and (g["date"][5:] == want["date"][5:] if yearly else g["date"] == want["date"])
            for g in got["key_dates"]
        ):
            problems.append(f"missing key date {want}")
    for key in ("contacts", "gestures"):
        for want in expect.get(key, []):
            if not any(all(g[k] == want[k] for k in want) for g in got.get(key, [])):
                problems.append(f"missing {key[:-1]} {want}")
    for want in expect.get("trust", []):
        if not any(all(g[k] == want[k] for k in want) for g in got["trust"]):
            problems.append(f"missing trust {want}")
    for kind in expect.get("self_observations", []):
        if kind not in got["self_observations"]:
            problems.append(f"missing self-observation {kind}")
    if expect.get("mirror") == "any":
        if got["mirror"] is None:
            problems.append("mirror: expected a warning, got none")
    elif "mirror" in expect and expect["mirror"] != got["mirror"]:
        problems.append(f"mirror: wanted {expect['mirror']}, got {got['mirror']}")
    for name in expect.get("absent_people", []):
        if name in got["people"]:
            problems.append(f"unexpected person {name}")
    return problems


def run_case(case: dict, llm_factory) -> CaseResult:
    from datetime import date

    from .almanac import Almanac
    from .config import load_settings
    from .deep import DeepVault
    from .pipeline import Pipeline

    with tempfile.TemporaryDirectory() as tmp:
        settings = load_settings(tmp)
        settings.kdf_strength = "interactive"
        almanac = Almanac(settings)
        vault = DeepVault(settings)
        vault.initialise("golden-run-passphrase")
        self_id = almanac.self_id()
        with almanac.tx() as c:
            for p in case.get("people", []):
                cur = c.execute(
                    "INSERT INTO people (display_name, aliases, grp, layer, source, created_at, updated_at) VALUES (?, ?, ?, ?, 'form', '', '')",
                    (p["name"], json.dumps(p.get("aliases", [])), p.get("group"), p.get("layer")),
                )
                if p.get("relation"):
                    c.execute(
                        "INSERT INTO family_links (from_id, to_id, relation, source, status, created_at) VALUES (?, ?, ?, 'form', 'corroborated', '')",
                        (cur.lastrowid, self_id, p["relation"]),
                    )
        pipeline = Pipeline(settings, almanac, vault, llm_factory(), inline=True)
        if case.get("today"):
            fixed = date.fromisoformat(case["today"])
            pipeline.today = lambda: fixed  # type: ignore[method-assign]
        note_id = pipeline.capture_text(case["note"])
        row = vault.store.one("SELECT extraction_status, last_error FROM notes WHERE id = ?", (note_id,))
        if row["extraction_status"] != "done":
            return CaseResult(case["id"], False, [f"extraction {row['extraction_status']}: {row['last_error']}"])
        e = vault.store.one("SELECT raw_json, resolution FROM extractions WHERE note_id = ? AND active = 1", (note_id,))
        ext = Extraction.model_validate_json(e["raw_json"])
        got = canonical(ext, Resolution.from_dict(json.loads(e["resolution"])), almanac.names())
        problems = _subset_problems(case.get("expect", {}), got)
        if LABEL_WORDS.search(" ".join(m.detail for m in ext.motives) + " " + " ".join(a.pattern for a in ext.assessments)):
            problems.append("a character label or diagnosis was stored")
        if len(ext.assessments) < case.get("expect", {}).get("assessments_min", 0):
            problems.append("character judgement not routed to assessments")
        vault.lock_now()
        return CaseResult(case["id"], not problems, problems)


def load_cases(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip() and not line.startswith("//")]


def corrections_as_cases(vault) -> list[dict]:
    out = []
    for r in vault.store.query("SELECT * FROM golden_cases ORDER BY id"):
        expected = json.loads(r["expected"])
        people = [{"name": p} for p in expected.get("people", []) if p != "ME"]
        out.append({
            "id": f"correction-{r['id']}",
            "note": r["note_text"],
            "people": people,
            "expect": {k: expected[k] for k in ("people", "interactions", "key_dates", "trust") if k in expected},
        })
    return out
