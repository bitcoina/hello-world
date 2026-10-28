import pytest
from fastapi.testclient import TestClient

from barometer.app import create_app
from barometer.auth import add_user
from barometer.config import load_settings
from barometer.core import Barometer
from barometer.llm_fake import FakeLLM

PASS = "deep passphrase for tests"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("BAROMETER_"):
            monkeypatch.delenv(key, raising=False)
    s = load_settings(tmp_path / "data")
    s.kdf_strength = "interactive"
    s.secure_cookies = False
    s.llm = "fake"
    return s


@pytest.fixture
def llm():
    return FakeLLM()


@pytest.fixture
def bm(settings, llm):
    b = Barometer(settings, llm=llm, inline_jobs=True)
    b.vault.initialise(PASS)
    b.lock()
    add_user(b.almanac, "phil", "owner-login-pass", "owner")
    add_user(b.almanac, "partner", "almanac-login-pass", "almanac")
    return b


@pytest.fixture
def unlocked(bm):
    bm.unlock(PASS)
    return bm


def person_id(bm, name):
    row = bm.almanac.one("SELECT id FROM people WHERE display_name = ?", (name,))
    return row["id"] if row else None


def add_person(bm, name, aliases=(), group="family", relation=None):
    import json

    with bm.almanac.tx() as c:
        cur = c.execute(
            "INSERT INTO people (display_name, aliases, grp, source, created_at, updated_at) VALUES (?, ?, ?, 'form', '2026-01-01T00:00:00+00:00', '')",
            (name, json.dumps(list(aliases)), group),
        )
        pid = cur.lastrowid
        if relation:
            c.execute(
                "INSERT INTO family_links (from_id, to_id, relation, source, status, created_at) VALUES (?, ?, ?, 'form', 'corroborated', '')",
                (pid, bm.almanac.self_id(), relation),
            )
    return pid


@pytest.fixture
def client(bm):
    app = create_app(bm, start_background=False)
    with TestClient(app) as c:
        c.headers["x-barometer"] = "1"
        yield c


def login(client, username="phil", password="owner-login-pass"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r
