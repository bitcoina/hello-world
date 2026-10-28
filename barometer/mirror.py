"""The Mirror's rate limits. The lines themselves come from the model; this decides
whether one may be shown.

State lives in the Almanac settings table as pattern keys and counts only (no text),
so the caps hold even while the Deep store is locked.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .almanac import Almanac

PATTERN_LABELS = {
    "edge_case_argument": "arguing edge cases",
    "ends_justify_means": "ends-justify-means reasoning",
    "devils_advocate_on_values": "devil's advocate on values",
    "moral_relativism": "moral relativism",
}

DEFAULT_STATE = {"week": "", "count": 0, "threshold": 2.0, "dismissals": {}, "mutes": {}}


def _week_key(now: datetime) -> str:
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


class MirrorGate:
    def __init__(self, almanac: Almanac, weekly_cap: int):
        self.almanac = almanac
        self.weekly_cap = weekly_cap

    def state(self) -> dict:
        state = {**DEFAULT_STATE, **(self.almanac.get_setting("mirror_state", {}) or {})}
        now = datetime.now(timezone.utc)
        if state["week"] != _week_key(now):
            state["week"], state["count"] = _week_key(now), 0
        return state

    def _save(self, state: dict) -> None:
        self.almanac.set_setting("mirror_state", state)

    def muted(self, pattern: str, state: dict | None = None) -> bool:
        state = state or self.state()
        until = state["mutes"].get(pattern)
        return bool(until) and datetime.fromisoformat(until) > datetime.now(timezone.utc)

    def allowed(self, pattern: str, strength: int = 3) -> bool:
        if not pattern or pattern == "none":
            return False
        state = self.state()
        return state["count"] < self.weekly_cap and not self.muted(pattern, state) and strength >= state["threshold"]

    def eligible_patterns(self) -> list[str]:
        state = self.state()
        if state["count"] >= self.weekly_cap:
            return []
        return [p for p in PATTERN_LABELS if not self.muted(p, state)]

    def record_shown(self) -> None:
        state = self.state()
        state["count"] += 1
        self._save(state)

    def dismissed(self, pattern: str) -> bool:
        """Three dismissals of the same pattern mute it for 30 days. Returns True if now muted."""
        state = self.state()
        n = state["dismissals"].get(pattern, 0) + 1
        muted = n >= 3
        if muted:
            state["mutes"][pattern] = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
            n = 0
        state["dismissals"][pattern] = n
        self._save(state)
        return muted

    def rated(self, useful: bool) -> float:
        """Ratings tune how clear a pattern must be before the Mirror speaks (1..3)."""
        state = self.state()
        step = -0.25 if useful else 0.25
        state["threshold"] = min(3.0, max(1.0, state["threshold"] + step))
        self._save(state)
        return state["threshold"]
