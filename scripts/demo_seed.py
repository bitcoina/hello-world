"""Seed a throwaway data directory with a fictional family, for trying the UI without an API key.

    python scripts/demo_seed.py ./demo-data
    BAROMETER_DATA_DIR=./demo-data BAROMETER_LLM=fake BAROMETER_SECURE_COOKIES=0 barometer serve

Login: demo / demo-login-pass   Private passphrase: demo private passphrase
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from barometer.auth import add_user  # noqa: E402
from barometer.config import load_settings  # noqa: E402
from barometer.core import Barometer  # noqa: E402
from barometer.llm_fake import FakeLLM, empty_extraction, person  # noqa: E402

PASSPHRASE = "demo private passphrase"


def main(target: str) -> None:
    settings = load_settings(target)
    settings.kdf_strength = "interactive"
    llm = FakeLLM()
    bm = Barometer(settings, llm=llm, inline_jobs=True)
    if bm.vault.initialised:
        print("Already seeded.")
        return
    bm.vault.initialise(PASSPHRASE)
    add_user(bm.almanac, "demo", "demo-login-pass", "owner")
    add_user(bm.almanac, "partner", "partner-login-pass", "almanac")
    me = bm.almanac.self_id()
    now = datetime.now(timezone.utc)

    def add(name, grp, layer, aliases=(), relation=None):
        with bm.almanac.tx() as c:
            cur = c.execute(
                "INSERT INTO people (display_name, aliases, grp, layer, source, created_at, updated_at) VALUES (?, ?, ?, ?, 'form', ?, ?)",
                (name, json.dumps(list(aliases)), grp, layer, (now - timedelta(days=200)).isoformat(), now.isoformat()),
            )
            if relation:
                c.execute(
                    "INSERT INTO family_links (from_id, to_id, relation, source, status, created_at) VALUES (?, ?, ?, 'form', 'corroborated', '')",
                    (cur.lastrowid, me, relation),
                )
            return cur.lastrowid

    jean = add("Jean", "family", 15, ["Mum"], "mother")
    sarah = add("Sarah", "family", 15, [], "sister")
    tom = add("Tom", "family", 15, [], "brother")
    mike = add("Mike", "family", 50)
    priya = add("Priya", "friends", 15)
    ken = add("Ken", "family", 150, ["Uncle Ken"], "uncle")
    val = add("Val", "family", 150, ["Aunt Val"], "aunt")
    add("Dan", "friends", 50)
    with bm.almanac.tx() as c:
        c.execute("INSERT INTO family_links (from_id, to_id, relation, source, status, created_at) VALUES (?, ?, 'husband', 'form', 'corroborated', '')", (mike, sarah))
        c.execute("INSERT INTO key_dates (person_id, kind, label, date, recurring, status, source, created_at) VALUES (?, 'birthday', 'birthday', ?, 1, 'corroborated', 'form', '')",
                  (sarah, (now + timedelta(days=5)).date().replace(year=1984).isoformat()))
        cur = c.execute("INSERT INTO events (title, date, location, type, source, created_at) VALUES ('Christmas at Sarah''s', ?, 'Sarah''s house', 'gathering', 'form', '')",
                        ((now + timedelta(days=58)).date().isoformat(),))
        for pid in (jean, sarah, tom, mike, ken):
            c.execute("INSERT INTO event_attendees (event_id, person_id) VALUES (?, ?)", (cur.lastrowid, pid))

    P = lambda x: f"P{x}"  # noqa: E731

    def act(a, b, verb, score, topic="", relayed_by="", quote="."):
        return {"actor": a, "target": b, "act": verb, "score": score, "witnessed": "relayed" if relayed_by else "me",
                "relayed_by": relayed_by, "topic": topic, "date": "", "interpretation": False, "confidence": "med", "quote": quote}

    op_day = (now + timedelta(days=9)).date().isoformat()
    notes = [
        (62, "Sunday lunch at Mum's. Tom and Sarah argued about who should host Christmas; Mum sided with Sarah. Uncle Ken said nothing all afternoon.",
         empty_extraction(
             summary=f"{P(tom)} and {P(sarah)} argued over hosting Christmas; {P(jean)} sided with {P(sarah)}",
             people=[person(P(jean), "Mum"), person(P(tom), "Tom"), person(P(sarah), "Sarah"), person(P(ken), "Uncle Ken")],
             interactions=[act(P(tom), P(sarah), "criticised", -2, "hosting Christmas"), act(P(sarah), P(tom), "criticised", -2, "hosting Christmas"),
                           act(P(jean), P(sarah), "sided with", 2, "hosting Christmas")],
             topics=[{"ref": "new", "label": "hosting Christmas", "sensitivity": 2, "frames": ["fairness: take turns", "tradition: Mum's way"],
                      "people": [{"person": P(tom), "frame": "fairness: take turns", "salience": 3}, {"person": P(sarah), "frame": "tradition: Mum's way", "salience": 3}], "quote": "."}],
             contacts=[{"person": P(jean), "initiated_by": "mutual", "channel": "in person", "date": "", "quote": "."}],
         )),
        (45, "Mum rang. She said Sarah and Mike had a blazing row about the inheritance. Her hip op is on the 14th.",
         empty_extraction(
             summary=f"{P(jean)} reports a row between {P(sarah)} and {P(mike)} over the inheritance",
             people=[person(P(jean), "Mum"), person(P(sarah), "Sarah"), person(P(mike), "Mike")],
             interactions=[act(P(sarah), P(mike), "criticised", -2, "the inheritance", P(jean)), act(P(mike), P(sarah), "criticised", -2, "the inheritance", P(jean))],
             claims=[{"source": P(jean), "about": [P(sarah), P(mike)], "text": f"{P(sarah)} and {P(mike)} rowed about the inheritance", "credibility": 3, "relates_to": "", "relation": "none", "quote": "."}],
             topics=[{"ref": "new", "label": "the inheritance", "sensitivity": 3, "frames": ["fairness", "reward for effort"],
                      "people": [{"person": P(sarah), "frame": "fairness", "salience": 3}, {"person": P(mike), "frame": "reward for effort", "salience": 2}], "quote": "."}],
             key_dates=[{"person": P(jean), "kind": "hospital", "label": "hip operation", "date": op_day, "recurring": False, "quote": "."}],
             contacts=[{"person": P(jean), "initiated_by": "them", "channel": "call", "date": "", "quote": "."}],
         )),
        (30, "Coffee with Priya. She confided she's thinking of leaving her job and asked me not to tell anyone. Aunt Val already knew somehow — Val tells Mum everything.",
         empty_extraction(
             summary=f"{P(priya)} confided about leaving her job; {P(val)} already knew",
             people=[person(P(priya), "Priya"), person(P(val), "Aunt Val"), person(P(jean), "Mum")],
             interactions=[act(P(priya), "ME", "confided in", 2)],
             edges=[{"from_ref": P(val), "to_ref": P(jean), "kind": "info-flow", "relation": "", "direction": "directed", "intensity": 2, "detail": "passes on news", "topics": [], "confidence": "med", "quote": "."}],
             trust=[{"truster": "ME", "trustee": P(val), "domain": "secrets", "level": -2, "basis": "observed", "confidence": "med", "quote": "."},
                    {"truster": P(priya), "trustee": "ME", "domain": "secrets", "level": 2, "basis": "stated", "confidence": "high", "quote": "."},
                    {"truster": "ME", "trustee": P(priya), "domain": "emotional support", "level": 2, "basis": "observed", "confidence": "med", "quote": "."}],
             contacts=[{"person": P(priya), "initiated_by": "them", "channel": "coffee", "date": "", "quote": "."}],
         )),
        (20, "Tom drove Mum to her pre-op appointment and helped Mike fix the car afterwards. He and Mike seem to be getting on.",
         empty_extraction(
             summary=f"{P(tom)} helped {P(jean)} and {P(mike)}",
             people=[person(P(tom), "Tom"), person(P(jean), "Mum"), person(P(mike), "Mike")],
             interactions=[act(P(tom), P(jean), "helped", 2), act(P(tom), P(mike), "helped", 2)],
         )),
        (12, "Argued the trolley problem with Uncle Ken at the pub. He got upset and left early. That was a misfire.",
         empty_extraction(
             summary=f"Trolley-problem argument with {P(ken)} went badly",
             people=[person(P(ken), "Uncle Ken")],
             interactions=[act(P(ken), "ME", "broke contact", -1)],
             profiles=[{"person": P(ken), "relativism_tolerance": "low", "quote": "."}],
             self_observations=[{"kind": "misfire", "description": f"Argued the trolley problem with {P(ken)}; he left upset", "predicted_outcome": "", "people": [P(ken)], "quote": "."}],
             contacts=[{"person": P(ken), "initiated_by": "me", "channel": "pub", "date": "", "quote": "."}],
         )),
        (6, "Sarah texted me first to ask about Christmas. She wants everyone at hers this year and said Tom 'always makes it about himself'.",
         empty_extraction(
             summary=f"{P(sarah)} wants to host Christmas; critical of {P(tom)}",
             people=[person(P(sarah), "Sarah"), person(P(tom), "Tom")],
             interactions=[act(P(sarah), P(tom), "criticised", -1, "hosting Christmas")],
             motives=[{"person": P(sarah), "kind": "want", "category": "belonging", "detail": "everyone together at hers for Christmas", "confidence": "high", "quote": "."}],
             contacts=[{"person": P(sarah), "initiated_by": "them", "channel": "text", "date": "", "quote": "."}],
         )),
        (3, "Mum told me Mike put Sarah down about money in front of the kids again. Mum's worried the family is splitting.",
         empty_extraction(
             summary=f"{P(mike)} undermined {P(sarah)} about money; {P(jean)} worried",
             people=[person(P(jean), "Mum"), person(P(mike), "Mike"), person(P(sarah), "Sarah")],
             interactions=[act(P(mike), P(sarah), "undermined", -2, "money", P(jean))],
             motives=[{"person": P(jean), "kind": "fear", "category": "belonging", "detail": "the family splitting", "confidence": "high", "quote": "."}],
             assessments=[{"person": P(mike), "pattern": f"Belittles {P(sarah)} about money when others are present", "stance": "supports", "existing": "", "quote": "."}],
             contacts=[{"person": P(jean), "initiated_by": "them", "channel": "call", "date": "", "quote": "."}],
         )),
        (1, "Sent Mum flowers ahead of her op; she loved them. Prediction: Christmas will be tense unless the hosting question is settled early.",
         empty_extraction(
             summary=f"Flowers for {P(jean)}; prediction about Christmas",
             people=[person(P(jean), "Mum"), person(P(sarah), "Sarah"), person(P(tom), "Tom")],
             gestures=[{"person": P(jean), "kind": "gift", "what": "flowers before her op", "date": "", "landed": "she loved them", "quote": "."}],
             self_observations=[{"kind": "prediction", "description": "Christmas will be tense unless hosting is settled early", "predicted_outcome": "tense Christmas", "people": [P(sarah), P(tom)], "quote": "."}],
         )),
    ]
    bm.unlock(PASSPHRASE)
    for days_ago, text, extraction in notes:
        llm.push(extraction)
        created = (now - timedelta(days=days_ago)).replace(microsecond=0).isoformat()
        nid, _ = bm.almanac.next_note_id()
        bm.pipeline.capture_text(text, note_id=nid, created_at=created)
    with bm.vault.tx() as c:
        c.execute("UPDATE self_observations SET created_at = ? WHERE kind = 'misfire'", ((now - timedelta(days=12)).isoformat(),))
        for months_ago, score in ((4, 0.5), (3, 1), (2, 0), (1, 1)):
            day = (now - timedelta(days=30 * months_ago)).isoformat()
            c.execute("INSERT INTO self_observations (kind, description, predicted_outcome, score, person_ids, created_at, scored_at)"
                      " VALUES ('prediction', 'Earlier prediction', 'x', ?, '[]', ?, ?)", (score, day, day))
    bm.lock()
    print(f"Seeded {target}. Login demo / demo-login-pass; Private passphrase: {PASSPHRASE}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "./demo-data")
