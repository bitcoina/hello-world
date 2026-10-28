from datetime import date, timedelta

import pytest

from barometer.graph import decay, interaction_rows_to_readings, source_grade, unbalanced_triads, weather
from barometer.mirror import MirrorGate
from barometer.pseudo import Pseudonymiser
from barometer.relationships import next_occurrence, plan_nudges, today_view, upcoming_dates, week_start

from .conftest import add_person


def row(a, b, score, day, status="corroborated", notes=("N1",)):
    import json

    return {"actor_id": a, "target_id": b, "score": score, "date": day.isoformat(), "status": status,
            "source_note_ids": json.dumps(list(notes)), "topic_label": None}


class R(dict):
    def keys(self):
        return super().keys()


def test_half_life_is_six_months():
    assert decay(0) == 1
    assert decay(182.6) == pytest.approx(0.5)
    assert decay(365.2) == pytest.approx(0.25)


def test_old_feud_fades_and_trend():
    today = date(2026, 10, 28)
    old = [R(row(1, 2, -3, today - timedelta(days=730)))]
    reading = interaction_rows_to_readings(old, today)[(1, 2)]
    assert abs(reading.value) < 0.25 and reading.trend == "stable"
    recent = [R(row(1, 2, -2, today - timedelta(days=5))), R(row(2, 1, -2, today - timedelta(days=3)))]
    reading = interaction_rows_to_readings(recent, today)[(1, 2)]
    assert reading.trend == "cooling" and reading.weather == "Stormy"


def test_direction_matters():
    today = date(2026, 10, 28)
    rows = [R(row(2, 1, 3, today))]  # B sided with A
    directed = interaction_rows_to_readings(rows, today, directed=True)
    assert (2, 1) in directed and (1, 2) not in directed


def test_provisional_weighs_less():
    today = date(2026, 10, 28)
    a = interaction_rows_to_readings([R(row(1, 2, 3, today, "provisional"))], today)[(1, 2)].value
    b = interaction_rows_to_readings([R(row(1, 2, 3, today, "corroborated"))], today)[(1, 2)].value
    assert a < b


def test_unbalanced_triads():
    # A likes B, B likes C, A and C in conflict: unstable.
    assert unbalanced_triads({(1, 2): 1, (2, 3): 1, (1, 3): -1}) == [(1, 2, 3)]
    assert unbalanced_triads({(1, 2): 1, (2, 3): 1, (1, 3): 1}) == []
    assert unbalanced_triads({(1, 2): -1, (2, 3): -1, (1, 3): 1}) == []  # enemy of my enemy
    assert unbalanced_triads({(1, 2): -1, (2, 3): -1, (1, 3): -1}) == [(1, 2, 3)]


def test_source_grades():
    assert source_grade(0, 0) == "F"
    assert source_grade(5, 0) == "A"
    assert source_grade(3, 1) == "B"
    assert source_grade(1, 3) == "E"


def test_weather_words():
    assert weather(3, 3, 0) == "Fair"
    assert weather(0.2, 0.2, 0) == "Settled"
    assert weather(0, 2, 2) == "Changeable"
    assert weather(-3, 0, 3) == "Stormy"


def test_pseudonymiser_round_trip():
    people = [
        {"id": 1, "display_name": "Me", "aliases": ["me"], "is_self": True},
        {"id": 3, "display_name": "Jean", "aliases": ["Mum", "my mother"]},
        {"id": 7, "display_name": "Will", "aliases": []},
        {"id": 8, "display_name": "Sarah", "aliases": []},
        {"id": 9, "display_name": "Sarah Cole", "aliases": ["Sarah"]},
    ]
    p = Pseudonymiser(people)
    text = "My mother said Will will come. mum's fine. Sarah Cole and Sarah argued."
    enc = p.encode(text)
    assert "P3 said P7 will come" in enc
    assert "P3's fine" in enc
    assert "P9 and Sarah argued" in enc  # bare "Sarah" is ambiguous, left as written
    assert p.ambiguous == {"sarah": [8, 9]}
    assert p.decode("P3 and P7") == "Jean and Will"


def test_next_occurrence_and_week_start():
    today = date(2026, 10, 28)
    assert next_occurrence("1950-11-02", True, today) == date(2026, 11, 2)
    assert next_occurrence("1950-03-02", True, today) == date(2027, 3, 2)
    assert next_occurrence("2026-11-14", False, today) == date(2026, 11, 14)
    assert week_start(today) == date(2026, 10, 25)  # a Sunday


def test_digest_dates_followups_and_at_most_three_nudges(bm):
    today = date(2026, 10, 28)
    with bm.almanac.tx() as c:
        for i in range(6):
            c.execute(
                "INSERT INTO people (display_name, layer, source, created_at, updated_at) VALUES (?, 15, 'form', '2026-01-01', '')",
                (f"Friend {i}",),
            )
    jean = add_person(bm, "Jean")
    with bm.almanac.tx() as c:
        c.execute("INSERT INTO key_dates (person_id, kind, label, date, recurring, created_at) VALUES (?, 'birthday', 'birthday', '1950-11-02', 1, '')", (jean,))
        c.execute("INSERT INTO follow_ups (person_id, due_date, prompt, created_at) VALUES (?, '2026-10-29', 'Ask Jean how the op went', '')", (jean,))
        c.execute("INSERT INTO follow_ups (person_id, due_date, prompt, created_at) VALUES (?, '2026-09-01', 'Very old', '')", (jean,))
    view = today_view(bm.almanac, today)
    assert [d["label"] for d in view["dates"]] == ["birthday"]
    assert view["dates"][0]["years"] == 76
    assert [f["prompt"] for f in view["follow_ups"]] == ["Ask Jean how the op went"]  # no backlog
    assert len(view["nudges"]) == 3
    # Same week: no new nudges pile up.
    assert len(today_view(bm.almanac, today)["nudges"]) == 3


def test_recent_contact_means_no_nudge(bm):
    today = date(2026, 10, 28)
    pid = add_person(bm, "Close friend")
    with bm.almanac.tx() as c:
        c.execute("UPDATE people SET layer = 15 WHERE id = ?", (pid,))
        c.execute("INSERT INTO contacts (person_id, date, initiated_by, created_at) VALUES (?, ?, 'me', '')", (pid, (today - timedelta(days=3)).isoformat()))
    assert plan_nudges(bm.almanac, today) == []


def test_upcoming_dates_filtered_by_person(bm):
    a, b = add_person(bm, "A"), add_person(bm, "B")
    with bm.almanac.tx() as c:
        c.execute("INSERT INTO key_dates (person_id, kind, label, date, recurring, created_at) VALUES (?, 'exam', 'exam', ?, 0, '')", (a, (date.today() + timedelta(days=2)).isoformat()))
        c.execute("INSERT INTO key_dates (person_id, kind, label, date, recurring, created_at) VALUES (?, 'exam', 'exam', ?, 0, '')", (b, (date.today() + timedelta(days=2)).isoformat()))
    assert [d["person"] for d in upcoming_dates(bm.almanac, date.today(), 14, {a})] == ["A"]


def test_mirror_gate_mutes_after_three_dismissals_and_tunes(bm):
    gate = MirrorGate(bm.almanac, weekly_cap=5)
    assert gate.allowed("edge_case_argument", 2)
    assert not gate.dismissed("edge_case_argument")
    assert not gate.dismissed("edge_case_argument")
    assert gate.dismissed("edge_case_argument")
    assert not gate.allowed("edge_case_argument", 3)
    assert gate.allowed("moral_relativism", 2)
    assert gate.rated(False) == 2.25
    assert not gate.allowed("moral_relativism", 2)
