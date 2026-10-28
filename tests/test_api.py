import time

from barometer.llm_fake import empty_extraction, person

from .conftest import PASS, add_person, login


def test_requires_login(client):
    assert client.get("/api/today").status_code == 401
    assert client.get("/api/session").json()["authenticated"] is False


def test_bad_password_and_throttle(client):
    for _ in range(5):
        assert client.post("/api/login", json={"username": "phil", "password": "nope"}).status_code == 401
    assert client.post("/api/login", json={"username": "phil", "password": "nope"}).status_code == 429


def test_mutations_need_custom_header(client):
    del client.headers["x-barometer"]
    r = client.post("/api/login", json={"username": "phil", "password": "owner-login-pass"})
    assert r.status_code == 403


def test_security_headers(client):
    r = client.get("/")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"


def test_almanac_login_cannot_capture_or_open_private(client, bm):
    login(client, "partner", "almanac-login-pass")
    assert client.post("/api/capture", json={"text": "hello"}).status_code == 403
    assert client.post("/api/deep/unlock", json={"passphrase": PASS}).status_code == 403
    assert client.get("/api/tray").status_code == 403
    # ...but can add birthdays and gifts.
    pid = client.post("/api/people", json={"display_name": "Gran", "group": "family"}).json()["id"]
    assert client.post(f"/api/people/{pid}/key_dates", json={"kind": "birthday", "label": "Birthday", "date": "1940-05-01"}).status_code == 200
    assert client.post(f"/api/people/{pid}/gestures", json={"what": "Flowers", "kind": "gift"}).status_code == 200
    data = client.get(f"/api/people/{pid}").json()
    assert data["key_dates"][0]["status"] == "corroborated" and data["deep"] is None


def test_owner_flow_capture_readback_correct_undo(client, bm, llm):
    login(client)
    jean = add_person(bm, "Jean", ["Mum"])
    assert client.get("/api/tray").status_code == 423
    r = client.post("/api/deep/unlock", json={"passphrase": "wrong one"})
    assert r.status_code == 401
    assert client.post("/api/deep/unlock", json={"passphrase": PASS}).status_code == 200
    llm.push(empty_extraction(people=[person(f"P{jean}", "Mum")], contacts=[
        {"person": f"P{jean}", "initiated_by": "them", "channel": "call", "date": "", "quote": "Mum rang"}]))
    st = client.post("/api/capture", json={"text": "Mum rang for a chat."}).json()
    assert st["status"] == "done" and "In touch with Jean." in st["readback"]
    note_id = st["note_id"]
    llm.push(empty_extraction(people=[person(f"P{jean}", "Mum")], contacts=[
        {"person": f"P{jean}", "initiated_by": "me", "channel": "call", "date": "", "quote": "I rang"}]))
    st = client.post(f"/api/notes/{note_id}/correct", json={"text": "I rang her, not the other way round"}).json()
    assert st["status"] == "done"
    assert bm.almanac.scalar("SELECT initiated_by FROM contacts") == "me"
    st = client.post(f"/api/notes/{note_id}/undo").json()
    assert st["status"] == "undone"
    assert bm.almanac.scalar("SELECT count(*) FROM contacts") == 0
    notes = client.get("/api/notes").json()
    assert notes[0]["text"] == "Mum rang for a chat."


def test_person_page_shows_private_reading_only_when_open(client, bm, llm):
    login(client)
    a, b = add_person(bm, "Ann"), add_person(bm, "Bea")
    assert client.get(f"/api/people/{a}").json()["deep"] is None
    client.post("/api/deep/unlock", json={"passphrase": PASS})
    llm.push(empty_extraction(people=[person(f"P{a}", "Ann"), person(f"P{b}", "Bea")], interactions=[
        {"actor": f"P{a}", "target": f"P{b}", "act": "criticised", "score": -2, "witnessed": "me", "relayed_by": "",
         "topic": "", "date": "", "interpretation": False, "confidence": "high", "quote": "x"}]))
    client.post("/api/capture", json={"text": "Ann had a go at Bea."})
    deep = client.get(f"/api/people/{a}").json()["deep"]
    assert deep["weather"][0]["person"] == "Bea" and deep["weather"][0]["weather"] in ("Changeable", "Stormy")
    m = client.get("/api/map").json()
    assert {n["label"] for n in m["nodes"]} >= {"Ann", "Bea"}


def test_brief_cites_every_claim_and_hides_names(client, bm, llm):
    login(client)
    a, b = add_person(bm, "Ann"), add_person(bm, "Bea")
    client.post("/api/deep/unlock", json={"passphrase": PASS})
    llm.push(empty_extraction(people=[person(f"P{a}", "Ann"), person(f"P{b}", "Bea")], interactions=[
        {"actor": f"P{a}", "target": f"P{b}", "act": "criticised", "score": -2, "witnessed": "me", "relayed_by": "",
         "topic": "", "date": "", "interpretation": False, "confidence": "high", "quote": "x"}]))
    note_id = client.post("/api/capture", json={"text": "Ann had a go at Bea about the wedding."}).json()["note_id"]
    llm.push({
        "fault_lines": [{"text": f"P{a} and P{b} are at odds (provisional).", "note_ids": [note_id]},
                        {"text": "An uncited claim that must be dropped.", "note_ids": []},
                        {"text": "A made-up citation.", "note_ids": ["N999"]}],
        "wants": [{"person": f"P{a}", "wants": "to be heard", "frame": "", "note_ids": [note_id]}],
        "trust_cautions": [], "avoid": [{"text": "The wedding.", "note_ids": [note_id]}], "seating": [],
        "prediction": {"text": f"P{a} will leave early.", "note_ids": [note_id]},
        "mirror_line": "", "uses_assessments": [],
    })
    out = client.post("/api/analyse", json={"mode": "brief", "person_ids": [a, b]}).json()
    prompt = llm.calls[-1]["user"]
    assert "Ann" not in prompt and "Bea" not in prompt and note_id in prompt
    claims = [i for s in out["sections"] for i in s["items"] if s["title"] != "Remember to ask"]
    assert claims and all(i["cites"] for i in claims)
    assert out["dropped_uncited"] == 2
    assert out["sections"][0]["items"][0]["text"] == "Ann and Bea are at odds (provisional)."
    assert out["prediction"]["text"] == "Ann will leave early."
    saved = client.post(f"/api/briefs/{out['id']}/save").json()
    assert client.post(f"/api/predictions/{saved['prediction_id']}/score", json={"score": 1}).status_code == 200
    stats = client.post("/api/analyse", json={"mode": "reflect"}).json()["stats"]
    assert stats["accuracy"] == 1.0


def test_explain_needs_two_hypotheses(client, bm, llm):
    login(client)
    a = add_person(bm, "Ann")
    client.post("/api/deep/unlock", json={"passphrase": PASS})
    client.post("/api/capture", json={"text": "Ann went quiet after the party."})
    llm.push({"what_happened": [], "explanations": [{"explanation": "only one", "supporting": [], "contradicting": [], "confidence": "low"}],
              "would_tell_apart": [], "mirror_line": ""})
    r = client.post("/api/analyse", json={"mode": "explain", "person_ids": [a], "query": "why did Ann go quiet?"})
    assert r.status_code == 502


def test_panic_locks_private(client, bm):
    login(client)
    client.post("/api/deep/unlock", json={"passphrase": PASS})
    assert client.get("/api/tray").status_code == 200
    client.post("/api/panic")
    assert not bm.vault.is_open
    assert client.get("/api/tray").status_code == 423
    assert client.get("/api/today").status_code == 200  # still signed in to the Almanac


def test_idle_logout_and_autolock(client, bm):
    login(client)
    client.post("/api/deep/unlock", json={"passphrase": PASS})
    bm.sessions.idle = 0.01
    bm.settings.session_idle_seconds = 0
    time.sleep(0.05)
    bm.tick()
    assert not bm.vault.is_open
    assert client.get("/api/today").status_code == 401


def test_readbacks_need_private_or_authorship(client, bm, settings):
    from fastapi.testclient import TestClient

    from barometer.app import create_app

    login(client)
    client.post("/api/deep/unlock", json={"passphrase": PASS})
    note_id = client.post("/api/capture", json={"text": "Private thoughts about Ann."}).json()["note_id"]
    with TestClient(create_app(bm, start_background=False)) as other:
        other.headers["x-barometer"] = "1"
        login(other)  # same owner, second device, Private not opened here
        assert other.get(f"/api/notes/{note_id}/status").status_code == 423
        mine = other.post("/api/capture", json={"text": "Quick one."}).json()["note_id"]
        assert other.get(f"/api/notes/{mine}/status").status_code == 200
