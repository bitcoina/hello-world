from pathlib import Path

from barometer.golden import load_cases, run_case
from barometer.llm_fake import FakeLLM, empty_extraction, person

CASES = Path(__file__).resolve().parent.parent / "golden" / "cases.jsonl"


def test_golden_file_has_ten_cases():
    cases = load_cases(CASES)
    assert len(cases) == 10 and len({c["id"] for c in cases}) == 10


def test_run_case_scores_a_correct_and_a_wrong_extraction():
    case = next(c for c in load_cases(CASES) if c["id"] == "g04-label-becomes-pattern")

    def good(user: str) -> dict:
        # Seeded people are P2 (Ken) and P3 (Sarah); the author is P1.
        return empty_extraction(
            people=[person("P2", "Uncle Ken"), person("P3", "Sarah")],
            interactions=[{"actor": "P2", "target": "P3", "act": "criticised", "score": -2, "witnessed": "me",
                           "relayed_by": "", "topic": "", "date": "", "interpretation": False, "confidence": "high",
                           "quote": "snapped at Sarah"}],
            assessments=[{"person": "P2", "pattern": "Goes silent for the rest of a meal after being corrected",
                          "stance": "supports", "existing": "", "quote": "didn't speak for the rest of the meal"}],
        )

    def labelled(user: str) -> dict:
        data = good(user)
        data["assessments"][0]["pattern"] = "Is a narcissist"
        return data

    for response, should_pass in ((good, True), (labelled, False)):
        fake = FakeLLM()
        fake.push(response)
        result = run_case(case, lambda: fake)
        assert result.passed is should_pass, result.problems
