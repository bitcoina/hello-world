"""Wires the pieces together and runs the background housekeeping."""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from .almanac import Almanac
from .auth import Sessions, Throttle
from .config import Settings
from .deep import DeepVault
from .llm import LLM, make_llm
from .pipeline import Pipeline
from .relationships import is_due, send_weekly_push

log = logging.getLogger(__name__)


class Barometer:
    def __init__(self, settings: Settings, llm: LLM | None = None, inline_jobs: bool = False):
        from .retrieval import Analyst

        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings = settings
        self.almanac = Almanac(settings)
        self.almanac.self_id()
        self.vault = DeepVault(settings)
        self.llm = llm or make_llm(settings)
        self.pipeline = Pipeline(settings, self.almanac, self.vault, self.llm, inline=inline_jobs)
        self.analyst = Analyst(settings, self.almanac, self.vault, self.llm, self.pipeline.mirror)
        self.sessions = Sessions(settings.session_idle_seconds)
        self.login_throttle = Throttle()
        self.deep_throttle = Throttle(free_attempts=3)
        self._stop = threading.Event()
        self._last_backup_day: str | None = None

    # -- Deep lifecycle ------------------------------------------------------------
    def unlock(self, passphrase: str) -> None:
        self.vault.unlock(passphrase)
        self.pipeline.jobs.submit(self.pipeline.drain)

    def lock(self) -> None:
        self.vault.lock_now()
        self.sessions.clear_deep()
        self.pipeline.forget_plaintext()

    # -- housekeeping ----------------------------------------------------------------
    def start(self) -> None:
        self.pipeline.resume_spool()
        threading.Thread(target=self._housekeeping, name="barometer-housekeeping", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def tick(self) -> None:
        self.sessions.expire()
        self.pipeline.expire()
        if self.vault.is_open and self.pipeline.jobs.idle():
            idle = self.vault.idle_for() > self.settings.session_idle_seconds
            if idle or not self.sessions.any_deep():
                log.info("Locking Deep store (idle)")
                self.lock()
        now = datetime.now(self.settings.tz)
        if is_due(now):
            try:
                send_weekly_push(self.almanac, self.settings.ntfy_url, now.date())
            except Exception:  # noqa: BLE001
                log.exception("weekly digest failed")
        if self.settings.backup_dir and self.settings.backup_public_key and now.hour == 3:
            day = now.date().isoformat()
            if self._last_backup_day != day:
                self._last_backup_day = day
                try:
                    from .backup import make_backup

                    make_backup(self.settings)
                except Exception:  # noqa: BLE001
                    log.exception("nightly backup failed")

    def _housekeeping(self) -> None:
        while not self._stop.wait(20):
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("housekeeping failed")
