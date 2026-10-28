from barometer.crypto import db_key_from_private, secret_decrypt
from barometer.llm_fake import empty_extraction, person

from .conftest import PASS, add_person, person_id


def deep_image(bm, settings) -> bytes:
    return secret_decrypt(db_key_from_private(bm.vault.verify(PASS)), settings.deep_path.read_bytes())


def all_almanac_bytes(settings) -> bytes:
    data = settings.almanac_path.read_bytes()
    wal = settings.almanac_path.with_name("almanac.db-wal")
    return data + (wal.read_bytes() if wal.exists() else b"")


def test_forget_person_leaves_no_trace(unlocked, llm, settings):
    bm = unlocked
    jean = add_person(bm, "Jean")
    priya = add_person(bm, "Priyanka", ["Pri"], group="friends")
    llm.push(empty_extraction(
        summary=f"P{priya} confided in P{jean}",
        people=[person(f"P{jean}", "Jean"), person(f"P{priya}", "Priyanka")],
        interactions=[{"actor": f"P{priya}", "target": f"P{jean}", "act": "confided in", "score": 2, "witnessed": "me",
                       "relayed_by": "", "topic": "Priyanka's divorce", "date": "", "interpretation": False,
                       "confidence": "high", "quote": "Priyanka confided in Jean"}],
        trust=[{"truster": "ME", "trustee": f"P{priya}", "domain": "secrets", "level": -1, "basis": "observed",
                "confidence": "med", "quote": "x"}],
        key_dates=[{"person": f"P{priya}", "kind": "birthday", "label": "Pri's birthday", "date": "1985-04-02",
                    "recurring": True, "quote": "x"}],
        assessments=[{"person": f"P{priya}", "pattern": "Shares other people's news within a day", "stance": "supports",
                      "existing": "", "quote": "x"}],
    ))
    note_id = bm.pipeline.capture_text("Priyanka confided in Jean about her divorce. Pri's birthday is 2 April.")
    with bm.almanac.tx() as c:
        c.execute("INSERT INTO gestures (person_id, date, what, source, created_at) VALUES (?, '2026-10-01', 'Book for Priyanka', 'form', '')", (priya,))
    assert b"Priyanka" in deep_image(bm, settings)

    from barometer.forget import forget_person

    forget_person(bm.almanac, bm.vault, priya)

    assert person_id(bm, "Priyanka") is None
    store = bm.vault.store
    for table in ("interactions", "trust", "assessments"):
        assert store.scalar(f"SELECT count(*) FROM {table}") == 0, table
    text = store.one("SELECT raw_text FROM notes WHERE id = ?", (note_id,))["raw_text"]
    assert "Priyanka" not in text and "Pri's" not in text and "Jean" in text
    image = deep_image(bm, settings)
    for needle in (b"Priyanka", b"Pri's"):
        assert needle not in image, needle
        assert needle not in all_almanac_bytes(settings), needle
    assert person_id(bm, "Jean")


def test_forget_via_api_requires_private(client, bm):
    from .conftest import login

    login(client)
    pid = add_person(bm, "Someone")
    assert client.delete(f"/api/people/{pid}").status_code == 423
    client.post("/api/deep/unlock", json={"passphrase": PASS})
    assert client.delete(f"/api/people/{pid}").status_code == 200
    assert person_id(bm, "Someone") is None


def test_backup_round_trip(unlocked, settings, tmp_path):
    from barometer.backup import keygen, make_backup, verify

    bm = unlocked
    bm.pipeline.capture_text("A note worth keeping.")
    private, public = keygen()
    settings.backup_dir = tmp_path / "backups"
    settings.backup_public_key = public
    path = make_backup(settings)
    assert b"worth keeping" not in path.read_bytes()
    report = verify(path, private, PASS)
    assert report["notes"] == 1 and "almanac.db" in report["files"]
