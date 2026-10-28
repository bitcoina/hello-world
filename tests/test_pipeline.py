from datetime import date, timedelta

from barometer.db import jload
from barometer.llm import LLMError
from barometer.llm_fake import empty_extraction, person

from .conftest import PASS, add_person, person_id

NOTE = "Mum rang. She said Sarah and Mike had a blazing row about the inheritance on Sunday. Her hip op is on the 14th."


def family(bm):
    return (
        add_person(bm, "Jean", ["Mum"], relation="mother"),
        add_person(bm, "Sarah", relation="sister"),
        add_person(bm, "Mike"),
    )


def row_extraction(j, s, m, op_day: str):
    P = lambda x: f"P{x}"  # noqa: E731
    return empty_extraction(
        summary=f"{P(j)} reports a row between {P(s)} and {P(m)} over the inheritance",
        people=[person(P(j), "Mum", relation_to_author="mother"), person(P(s), "Sarah"), person(P(m), "Mike")],
        interactions=[
            {"actor": P(s), "target": P(m), "act": "criticised", "score": -2, "witnessed": "relayed", "relayed_by": P(j),
             "topic": "the inheritance", "date": "", "interpretation": False, "confidence": "med", "quote": "a blazing row"},
            {"actor": P(m), "target": P(s), "act": "criticised", "score": -2, "witnessed": "relayed", "relayed_by": P(j),
             "topic": "the inheritance", "date": "", "interpretation": False, "confidence": "med", "quote": "a blazing row"},
        ],
        claims=[{"source": P(j), "about": [P(s), P(m)], "text": f"{P(s)} and {P(m)} rowed about the inheritance",
                 "credibility": 3, "relates_to": "", "relation": "none", "quote": "She said"}],
        topics=[{"ref": "new", "label": "the inheritance", "sensitivity": 3, "frames": ["fairness", "reward for effort"],
                 "people": [{"person": P(s), "frame": "fairness", "salience": 3}], "quote": "the inheritance"}],
        key_dates=[{"person": P(j), "kind": "hospital", "label": "hip operation", "date": op_day, "recurring": False,
                    "quote": "Her hip op is on the 14th."}],
        contacts=[{"person": P(j), "initiated_by": "them", "channel": "call", "date": "", "quote": "Mum rang."}],
    )


def op_day():
    return (date.today() + timedelta(days=10)).isoformat()


def test_capture_extracts_commits_and_reads_back(unlocked, llm):
    bm = unlocked
    j, s, m = family(bm)
    llm.push(row_extraction(j, s, m, op_day()))
    note_id = bm.pipeline.capture_text(NOTE)
    st = bm.pipeline.status(note_id)
    assert st["status"] == "done", st
    rb = st["readback"]
    assert "tension between Sarah and Mike over the inheritance (provisional)" in rb
    assert "Jean told you, so they're a likely info route." in rb
    assert "hip operation" in rb and "follow up" in rb

    # Names never reached the model.
    prompt = llm.calls[-1]["user"]
    for name in ("Jean", "Sarah", "Mike", "Mum"):
        assert name not in prompt.split("<note>")[1], name

    store = bm.vault.store
    assert store.scalar("SELECT count(*) FROM interactions") == 2
    assert store.scalar("SELECT count(*) FROM edges WHERE kind = 'info-flow' AND from_id = ?", (j,)) == 1
    assert store.scalar("SELECT sensitivity FROM topics WHERE label = 'the inheritance'") == 3
    # The model wrote refs; what's stored reads naturally.
    assert store.scalar("SELECT text FROM claims") == "Sarah and Mike rowed about the inheritance"
    kd = bm.almanac.one("SELECT * FROM key_dates WHERE person_id = ?", (j,))
    assert kd["status"] == "provisional" and jload(kd["source_note_ids"]) == [note_id]
    fu = bm.almanac.one("SELECT * FROM follow_ups WHERE person_id = ?", (j,))
    assert fu["prompt"] == "Ask Jean how the hip operation went"
    assert fu["due_date"] == (date.fromisoformat(op_day()) + timedelta(days=2)).isoformat()
    assert bm.almanac.scalar("SELECT initiated_by FROM contacts WHERE person_id = ?", (j,)) == "them"


def test_second_note_corroborates(unlocked, llm):
    bm = unlocked
    j, s, m = family(bm)
    llm.push(row_extraction(j, s, m, op_day()))
    bm.pipeline.capture_text(NOTE)
    llm.push(row_extraction(j, s, m, op_day()))
    n2 = bm.pipeline.capture_text("Sarah told me herself about the row with Mike. Jean's op is still on the 14th.")
    assert bm.vault.store.scalar("SELECT count(*) FROM interactions") == 2
    assert {r["status"] for r in bm.vault.store.query("SELECT status FROM interactions")} == {"corroborated"}
    assert bm.almanac.scalar("SELECT status FROM key_dates") == "corroborated"
    assert "(corroborated)" in bm.pipeline.status(n2)["readback"]


def test_undo_is_one_action_and_note_stays(unlocked, llm):
    bm = unlocked
    j, s, m = family(bm)
    ext = row_extraction(j, s, m, op_day())
    ext["people"].append(person("X1", "Auntie Val", relation_to_author="aunt"))
    ext["edges"] = [{"from_ref": "X1", "to_ref": f"P{j}", "kind": "info-flow", "relation": "", "direction": "directed",
                     "intensity": 1, "detail": "", "topics": [], "confidence": "med", "quote": "x"}]
    llm.push(ext)
    note_id = bm.pipeline.capture_text(NOTE + " Auntie Val passes everything on to Mum.")
    assert person_id(bm, "Auntie Val")
    bm.pipeline.undo(note_id)
    store = bm.vault.store
    for table in ("interactions", "edges", "topics", "claims"):
        assert store.scalar(f"SELECT count(*) FROM {table}") == 0, table
    assert bm.almanac.scalar("SELECT count(*) FROM key_dates") == 0
    assert bm.almanac.scalar("SELECT count(*) FROM follow_ups") == 0
    assert person_id(bm, "Auntie Val") is None  # created by this note only
    assert person_id(bm, "Jean")  # added by hand, never removed by undo
    note = store.one("SELECT * FROM notes WHERE id = ?", (note_id,))
    assert note["raw_text"].startswith("Mum rang.") and note["extraction_status"] == "undone"


def test_correction_replaces_and_becomes_golden_case(unlocked, llm):
    bm = unlocked
    j, s, m = family(bm)
    llm.push(row_extraction(j, s, m, op_day()))
    note_id = bm.pipeline.capture_text(NOTE)
    fixed = row_extraction(j, s, m, op_day())
    fixed["interactions"] = fixed["interactions"][:1]
    fixed["interactions"][0]["act"] = "excluded"
    fixed["interactions"][0]["score"] = -1
    llm.push(fixed)
    bm.pipeline.correct(note_id, "It wasn't a row; Sarah just left Mike out of the plans.")
    assert "correction" in llm.calls[-1]["user"]
    assert "P" in llm.calls[-1]["user"] and "Sarah just left" not in llm.calls[-1]["user"]
    store = bm.vault.store
    rows = store.query("SELECT act, score FROM interactions")
    assert [(r["act"], r["score"]) for r in rows] == [("excluded", -1)]
    assert "Sarah excluded Mike" in bm.pipeline.status(note_id)["readback"]
    golden = store.one("SELECT * FROM golden_cases")
    assert golden["note_id"] == note_id and "left Mike out" in golden["correction"]


def test_invalid_output_writes_nothing(unlocked, llm):
    bm = unlocked
    bad = empty_extraction(interactions=[{"actor": "P999", "target": "ME", "act": "praised", "score": 1, "witnessed": "me",
                                          "relayed_by": "", "topic": "", "date": "", "interpretation": False,
                                          "confidence": "low", "quote": "x"}])
    llm.push(bad)
    note_id = bm.pipeline.capture_text("Someone praised me.")
    st = bm.pipeline.status(note_id)
    assert st["status"] == "failed" and "not ME or listed" in st["error"]
    assert bm.vault.store.scalar("SELECT count(*) FROM interactions") == 0
    assert bm.almanac.scalar("SELECT count(*) FROM people WHERE is_self = 0") == 0


def test_llm_unavailable_leaves_note_pending_then_retry(unlocked, llm):
    from barometer.llm import LLMUnavailable

    bm = unlocked
    llm.push(LLMUnavailable("offline"))
    note_id = bm.pipeline.capture_text("Coffee with Priya.")
    assert bm.pipeline.status(note_id)["status"] == "pending"
    bm.pipeline.retry(note_id)
    assert bm.pipeline.status(note_id)["status"] == "done"
    assert person_id(bm, "Priya")


def test_locked_capture_extracts_now_and_files_private_part_on_unlock(bm, llm, settings):
    j, s, m = family(bm)
    llm.push(row_extraction(j, s, m, op_day()))
    note_id = bm.pipeline.capture_text(NOTE)
    st = bm.pipeline.status(note_id)
    assert st["status"] == "done"
    assert "sealed until you next open Private" in st["readback"]
    assert "tension between Sarah and Mike" in st["readback"]
    # Shareable facts landed in the Almanac straight away.
    assert bm.almanac.scalar("SELECT count(*) FROM follow_ups") == 1
    # The note and the political part are only in the sealed inbox.
    sealed = bm.almanac.one("SELECT sealed FROM deep_inbox WHERE note_id = ?", (note_id,))["sealed"]
    assert b"inheritance" not in sealed and b"Sarah" not in sealed
    assert b"inheritance" not in settings.almanac_path.read_bytes()

    bm.unlock(PASS)
    store = bm.vault.store
    assert bm.almanac.scalar("SELECT count(*) FROM deep_inbox") == 0
    assert store.one("SELECT extraction_status FROM notes WHERE id = ?", (note_id,))["extraction_status"] == "done"
    assert store.scalar("SELECT count(*) FROM interactions") == 2
    assert store.scalar("SELECT count(*) FROM topics") == 1
    assert bm.almanac.scalar("SELECT count(*) FROM follow_ups") == 1  # not applied twice


def test_locked_correction_and_undo_before_unlock(bm, llm):
    j, s, m = family(bm)
    llm.push(row_extraction(j, s, m, op_day()))
    note_id = bm.pipeline.capture_text(NOTE)
    fixed = row_extraction(j, s, m, op_day())
    fixed["key_dates"] = []
    llm.push(fixed)
    bm.pipeline.correct(note_id, "The op date isn't fixed yet.")
    assert bm.almanac.scalar("SELECT count(*) FROM key_dates") == 0
    bm.pipeline.undo(note_id)
    assert bm.almanac.scalar("SELECT count(*) FROM contacts") == 0
    bm.unlock(PASS)
    assert bm.vault.store.one("SELECT extraction_status FROM notes WHERE id = ?", (note_id,))["extraction_status"] == "undone"
    assert bm.vault.store.scalar("SELECT count(*) FROM interactions") == 0


def test_locked_capture_after_restart_is_extracted_on_unlock(bm, llm, settings):
    from barometer.core import Barometer

    bm.pipeline.jobs.inline = False  # simulate the box dying before extraction ran
    bm.pipeline.jobs.submit = lambda *a, **k: None
    note_id = bm.pipeline.capture_text("Dinner with Priya on Friday.")
    again = Barometer(settings, llm=llm, inline_jobs=True)
    assert again.pipeline.status(note_id)["status"] == "sealed"
    again.unlock(PASS)
    assert again.vault.store.one("SELECT extraction_status FROM notes WHERE id = ?", (note_id,))["extraction_status"] == "done"
    assert person_id(again, "Priya")


def test_ambiguous_name_asks_one_question_and_remaps(unlocked, llm):
    bm = unlocked
    s1 = add_person(bm, "Sarah", ["Sarah"], relation="sister")
    s2 = add_person(bm, "Sarah Cole", ["Sarah"], group="friends")
    llm.push(empty_extraction(
        people=[person(f"P{s1}", "Sarah", candidates=[f"P{s2}"])],
        motives=[{"person": f"P{s1}", "kind": "want", "category": "belonging", "detail": "to be included",
                  "confidence": "med", "quote": "x"}],
    ))
    note_id = bm.pipeline.capture_text("Sarah feels left out.")
    q = bm.pipeline.status(note_id)["question"]
    assert q and {o["value"] for o in q["options"]} == {str(s1), str(s2)}
    bm.pipeline.answer(note_id, str(s2))
    assert bm.vault.store.scalar("SELECT person_id FROM motives") == s2
    assert bm.pipeline.status(note_id)["question"] is None
    assert len(llm.calls) == 1  # answering doesn't re-run the model


def test_new_name_close_to_existing_asks_and_defaults_to_new(unlocked, llm):
    bm = unlocked
    sarah = add_person(bm, "Sarah")
    llm.push(empty_extraction(people=[person("X1", "Sara")]))
    note_id = bm.pipeline.capture_text("Sara called.")
    q = bm.pipeline.status(note_id)["question"]
    assert q and q["options"][0]["value"] == str(sarah)
    assert person_id(bm, "Sara")
    bm.pipeline.answer(note_id, str(sarah))
    assert person_id(bm, "Sara") is None


def test_contradicting_a_corroborated_alliance_asks(unlocked, llm):
    bm = unlocked
    a, b = add_person(bm, "Ann"), add_person(bm, "Bea")

    def praise(score, act="sided with"):
        return empty_extraction(people=[person(f"P{a}", "Ann"), person(f"P{b}", "Bea")], interactions=[
            {"actor": f"P{a}", "target": f"P{b}", "act": act, "score": score, "witnessed": "me", "relayed_by": "",
             "topic": "", "date": "", "interpretation": False, "confidence": "high", "quote": "x"}])

    llm.push(praise(3))
    bm.pipeline.capture_text("Ann backed Bea all evening.")
    llm.push(praise(3, "helped"))
    bm.pipeline.capture_text("Ann helped Bea move.")
    llm.push(praise(-3, "undermined"))
    note_id = bm.pipeline.capture_text("Ann undermined Bea in front of everyone.")
    q = bm.pipeline.status(note_id)["question"]
    assert q and "Is this a real change?" in q["prompt"]
    bm.pipeline.answer(note_id, "one-off")
    assert bm.vault.store.scalar("SELECT status FROM interactions WHERE act = 'undermined'") == "retired"


def test_claim_check_updates_source_grade(unlocked, llm):
    bm = unlocked
    j, mike = add_person(bm, "Jean"), add_person(bm, "Mike")
    llm.push(empty_extraction(people=[person(f"P{j}", "Jean"), person(f"P{mike}", "Mike")], claims=[
        {"source": f"P{j}", "about": [f"P{mike}"], "text": f"P{mike} is changing jobs", "credibility": 3,
         "relates_to": "", "relation": "none", "quote": "x"}]))
    bm.pipeline.capture_text("Jean says Mike is changing jobs.")
    cid = bm.vault.store.scalar("SELECT id FROM claims")
    llm.push(empty_extraction(people=[person(f"P{mike}", "Mike")], claims=[
        {"source": f"P{mike}", "about": [f"P{mike}"], "text": "new job confirmed", "credibility": 2,
         "relates_to": f"C{cid}", "relation": "confirms", "quote": "x"}]))
    n2 = bm.pipeline.capture_text("Mike told me he starts his new job Monday.")
    assert "Does this note confirm" in bm.pipeline.status(n2)["question"]["prompt"]
    bm.pipeline.answer(n2, "confirmed")
    claim = bm.vault.store.one("SELECT * FROM claims WHERE id = ?", (cid,))
    assert claim["status"] == "confirmed" and claim["credibility"] == 1


def test_mirror_line_at_capture_respects_weekly_cap(unlocked, llm):
    bm = unlocked
    line = "You're rehearsing a trolley-problem argument; in mixed company it tends to land as 'he'd do it to me'."
    for _ in range(3):
        llm.push(empty_extraction(mirror={"pattern": "edge_case_argument", "strength": 3, "line": line}))
    shown = [line in (bm.pipeline.status(bm.pipeline.capture_text("Going to argue the trolley problem at dinner."))["readback"] or "")
             for _ in range(3)]
    assert shown == [True, True, False]  # weekly cap of 2


def test_assessments_never_in_readback(unlocked, llm):
    bm = unlocked
    k = add_person(bm, "Ken")
    llm.push(empty_extraction(people=[person(f"P{k}", "Ken")], assessments=[
        {"person": f"P{k}", "pattern": "Goes quiet for days after being contradicted in public", "stance": "supports",
         "existing": "", "quote": "x"}]))
    note_id = bm.pipeline.capture_text("Ken sulked again after I corrected him at lunch.")
    assert "quiet" not in bm.pipeline.status(note_id)["readback"]
    assert bm.vault.store.scalar("SELECT count(*) FROM assessments") == 1


def test_validator_rejects_unknown_refs():
    from barometer.extraction import ExtractionContext, validator
    from barometer.pseudo import Pseudonymiser
    from barometer.schemas import Extraction

    ctx = ExtractionContext(today=date.today(), pseudo=Pseudonymiser([]), index_text="", person_refs={"ME", "P1"})
    ext = Extraction.model_validate(empty_extraction(key_dates=[
        {"person": "P1", "kind": "birthday", "label": "birthday", "date": "14th", "recurring": True, "quote": "x"}]))
    try:
        validator(ctx)(ext)
        raise AssertionError("expected rejection")
    except ValueError as exc:
        assert "P1" in str(exc) and "YYYY-MM-DD" in str(exc)
    assert LLMError
