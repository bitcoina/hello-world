"""Capture -> (transcribe) -> extract -> commit -> readback, with corrections and questions.

All mutations after capture run on one worker thread, in order. That keeps the two
stores consistent without cross-database transactions: a locked-mode extraction
always finishes before the drain that follows an unlock.

Locked mode (Deep closed): the note is sealed into the inbox the moment it arrives.
Extraction still runs straight away, in memory, using the Almanac's context. The
shareable facts (people, dates, follow-ups, gifts, events) land in the Almanac; the
political part is sealed back into the inbox and applied on the next unlock. The
plaintext is kept in memory only until the session idles out, so you can correct
the readback right after capturing.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable

from .almanac import Almanac
from .commit import (
    Resolution,
    apply_almanac,
    apply_deep,
    compose_readback,
    prune_unsupported_people,
    undo_almanac,
    undo_deep,
)
from .config import Settings
from .db import jdump, jload, utcnow
from .deep import DeepLocked, DeepVault
from .extraction import build_context, run_extraction
from .llm import LLM, LLMError, LLMUnavailable
from .mirror import MirrorGate
from .schemas import Extraction
from .voice import Transcriber, shred

log = logging.getLogger(__name__)


class JobQueue:
    """A single worker thread. ``call`` waits for the result; ``submit`` doesn't."""

    def __init__(self, inline: bool = False):
        self.inline = inline
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        if not inline:
            self._thread = threading.Thread(target=self._run, name="barometer-jobs", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            fn, args, fut = self._q.get()
            if fut.set_running_or_notify_cancel():
                try:
                    fut.set_result(fn(*args))
                except BaseException as exc:  # noqa: BLE001 - surfaced through the future
                    log.exception("job %s failed", getattr(fn, "__name__", fn))
                    fut.set_exception(exc)

    def submit(self, fn: Callable, *args) -> Future:
        fut: Future = Future()
        if self.inline:
            try:
                fut.set_result(fn(*args))
            except BaseException as exc:  # noqa: BLE001
                fut.set_exception(exc)
            return fut
        self._q.put((fn, args, fut))
        return fut

    def call(self, fn: Callable, *args, timeout: float = 120.0) -> Any:
        return self.submit(fn, *args).result(timeout=timeout)

    def idle(self) -> bool:
        return self._q.empty()


@dataclass
class Recent:
    """In-memory view of a capture made while Deep was locked."""

    note: dict | None
    status: str
    expires: float
    readback: str | None = None
    question: dict | None = None
    new_people: list[int] = field(default_factory=list)
    error: str | None = None
    payload: dict | None = None


class Pipeline:
    def __init__(self, settings: Settings, almanac: Almanac, vault: DeepVault, llm: LLM, inline: bool = False):
        self.settings = settings
        self.almanac = almanac
        self.vault = vault
        self.llm = llm
        self.jobs = JobQueue(inline=inline)
        self.recent: dict[str, Recent] = {}
        self.mirror = MirrorGate(almanac, settings.mirror_weekly_cap)
        self.transcriber = Transcriber(settings)
        self._recent_lock = threading.Lock()

    # ------------------------------------------------------------------ helpers
    def local_day(self, iso_utc: str) -> date:
        return datetime.fromisoformat(iso_utc).astimezone(self.settings.tz).date()

    def today(self) -> date:
        return datetime.now(self.settings.tz).date()

    def _remember(self, note_id: str, **kw) -> Recent:
        with self._recent_lock:
            r = self.recent.get(note_id)
            if r is None:
                r = self.recent[note_id] = Recent(note=None, status="saved", expires=0)
            for k, v in kw.items():
                setattr(r, k, v)
            r.expires = time.monotonic() + self.settings.session_idle_seconds
            return r

    def forget_plaintext(self) -> None:
        """Called on lock, panic and logout: drop every in-memory note."""
        with self._recent_lock:
            self.recent.clear()

    def expire(self) -> None:
        now = time.monotonic()
        with self._recent_lock:
            for k in [k for k, r in self.recent.items() if r.expires < now]:
                del self.recent[k]

    def _deep_note(self, note_id: str) -> dict | None:
        if not self.vault.is_open:
            return None
        row = self.vault.store.one("SELECT * FROM notes WHERE id = ?", (note_id,))
        return dict(row) if row else None

    # ------------------------------------------------------------------ capture
    def capture_text(self, text: str, event_id: int | None = None, input_type: str = "text",
                     note_id: str | None = None, created_at: str | None = None) -> str:
        text = text.strip()
        if not text:
            raise ValueError("Empty note")
        if note_id is None:
            note_id, _ = self.almanac.next_note_id()
        note = {
            "id": note_id,
            "seq": int(note_id[1:]),
            "created_at": created_at or utcnow(),
            "raw_text": text,
            "input_type": input_type,
            "event_id": event_id,
        }
        with self.vault.lock:
            if self.vault.is_open:
                with self.vault.tx() as c:
                    c.execute(
                        "INSERT INTO notes (id, seq, created_at, raw_text, input_type, event_id, extraction_status)"
                        " VALUES (:id, :seq, :created_at, :raw_text, :input_type, :event_id, 'extracting')",
                        note,
                    )
                plain = None
            else:
                with self.almanac.tx() as c:
                    c.execute(
                        "INSERT INTO deep_inbox (note_id, sealed, created_at) VALUES (?, ?, ?)",
                        (note_id, self.vault.seal({"note": note}), note["created_at"]),
                    )
                self._remember(note_id, note=note, status="extracting")
                plain = note
        self.jobs.submit(self._extract, note_id, plain, None)
        return note_id

    def capture_voice(self, audio: bytes, suffix: str, event_id: int | None = None) -> str:
        if not self.transcriber.available:
            raise RuntimeError("Voice capture is not set up on this Pi")
        note_id, _ = self.almanac.next_note_id()
        spool = self.settings.spool_dir
        spool.mkdir(parents=True, exist_ok=True)
        audio_path = spool / f"{note_id}{suffix}"
        audio_path.write_bytes(audio)
        (spool / f"{note_id}.json").write_text(json.dumps({"event_id": event_id, "created_at": utcnow(), "audio": audio_path.name}))
        self._remember(note_id, status="transcribing")
        self.jobs.submit(self._transcribe, note_id)
        return note_id

    def resume_spool(self) -> None:
        spool = self.settings.spool_dir
        if spool.exists():
            for meta in sorted(spool.glob("N*.json")):
                self.jobs.submit(self._transcribe, meta.stem)

    def _transcribe(self, note_id: str) -> None:
        meta_path = self.settings.spool_dir / f"{note_id}.json"
        if not meta_path.exists():
            return
        meta = json.loads(meta_path.read_text())
        audio = self.settings.spool_dir / meta["audio"]
        try:
            text = self.transcriber.transcribe(audio)
        except Exception as exc:  # noqa: BLE001 - keep the audio for a retry after restart
            log.exception("transcription failed for %s", note_id)
            self._remember(note_id, status="failed", error=f"Transcription failed: {exc}")
            return
        shred(audio)
        meta_path.unlink(missing_ok=True)
        if not text:
            self._remember(note_id, status="failed", error="Couldn't hear anything in that recording.")
            return
        self.capture_text(text, meta.get("event_id"), "voice", note_id=note_id, created_at=meta["created_at"])

    # --------------------------------------------------------------- extraction
    def _extract(self, note_id: str, plain: dict | None, correction: str | None) -> None:
        note = self._deep_note(note_id)
        mode = "open" if note else ("locked" if plain else None)
        if mode is None:
            return  # sealed with no plaintext in memory: the next drain picks it up
        note = note or plain
        previous = None
        if correction:
            previous = (note.get("readback") if mode == "open" else (self.recent.get(note_id).readback if note_id in self.recent else None))
        try:
            ctx = build_context(self.almanac, self.vault if mode == "open" else _Closed(), self.today(),
                                self.settings.pseudonymise)
            ext = run_extraction(
                self.llm, ctx, note_id=note_id, created_at=note["created_at"], text=note["raw_text"],
                input_type=note["input_type"], event_id=note.get("event_id"), effort=self.settings.effort_extract,
                correction=correction, previous_summary=previous,
            )
        except LLMError as exc:
            status = "pending" if isinstance(exc, LLMUnavailable) else "failed"
            self._mark(note_id, mode, status, str(exc))
            return
        try:
            self._commit(note_id, mode, ext, overrides={}, correction=correction)
        except Exception as exc:  # noqa: BLE001 - nothing partial is left behind; say so
            log.exception("commit failed for %s", note_id)
            self._mark(note_id, mode, "failed", f"Couldn't file this note: {exc}")

    def _mark(self, note_id: str, mode: str, status: str, error: str | None) -> None:
        if mode == "open":
            with self.vault.tx() as c:
                c.execute("UPDATE notes SET extraction_status = ?, last_error = ? WHERE id = ?", (status, error, note_id))
        else:
            self._remember(note_id, status=status, error=error)

    def _commit(self, note_id: str, mode: str, ext: Extraction, overrides: dict, correction: str | None) -> None:
        if mode == "open":
            self._commit_open(note_id, ext, overrides, correction)
        else:
            self._commit_locked(note_id, ext, overrides, correction)

    def _mirror_line(self, ext: Extraction) -> str | None:
        m = ext.mirror
        if m.pattern != "none" and m.line and self.mirror.allowed(m.pattern, m.strength):
            self.mirror.record_shown()
            return m.line
        return None

    def _commit_open(self, note_id: str, ext: Extraction, overrides: dict, correction: str | None,
                     mirror_line: str | None = None, use_mirror: bool = True) -> None:
        note = self._deep_note(note_id)
        assert note is not None
        day = self.local_day(note["created_at"])
        had_previous = bool(self.vault.store.scalar(
            "SELECT count(*) FROM extractions WHERE note_id = ? AND active = 1", (note_id,), 0))
        if had_previous:
            undo_almanac(self.almanac, note_id, prune_people=False)
            with self.vault.tx() as c:
                undo_deep(c, note_id)
        res, alm_out, event_id = apply_almanac(self.almanac, ext, note_id, day, overrides, note.get("event_id"))
        names = self.almanac.names()
        try:
            with self.vault.tx() as c:
                deep_out = apply_deep(c, ext, res, note_id, day, names)
                question = res.question or deep_out.question
                if use_mirror and mirror_line is None:
                    mirror_line = self._mirror_line(ext)
                if mirror_line:
                    c.execute(
                        "INSERT INTO mirror_log (created_at, context, pattern, text, note_id) VALUES (?, 'capture', ?, ?, ?)",
                        (utcnow(), ext.mirror.pattern, mirror_line, note_id),
                    )
                statuses = {**alm_out.statuses, **deep_out.statuses}
                readback = compose_readback(ext, res, names, statuses, alm_out.follow_ups,
                                            deep_out.new_topics, alm_out.event_title, mirror_line, False)
                c.execute(
                    "INSERT INTO extractions (note_id, created_at, raw_json, correction, overrides, resolution)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (note_id, utcnow(), ext.model_dump_json(), correction, jdump(overrides), jdump(res.as_dict())),
                )
                if question:
                    c.execute(
                        "INSERT INTO questions (note_id, kind, prompt, options, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (note_id, question["kind"], question["prompt"], jdump(question["options"]), jdump(question), utcnow()),
                    )
                c.execute(
                    "UPDATE notes SET extraction_status = 'done', summary = ?, readback = ?, event_id = ?, last_error = NULL WHERE id = ?",
                    (_summary(ext.summary, names), readback, event_id, note_id),
                )
                for pid in set(res.refs.values()) - {res.refs["ME"]}:
                    c.execute("INSERT OR IGNORE INTO note_people (note_id, person_id) VALUES (?, ?)", (note_id, pid))
                if correction:
                    from .golden import canonical

                    c.execute(
                        "INSERT INTO golden_cases (note_id, note_text, correction, expected, created_at) VALUES (?, ?, ?, ?, ?)",
                        (note_id, note["raw_text"], correction, jdump(canonical(ext, res, names)), utcnow()),
                    )
        except BaseException:
            undo_almanac(self.almanac, note_id)
            raise
        prune_unsupported_people(self.almanac)
        with self._recent_lock:
            self.recent.pop(note_id, None)

    def _commit_locked(self, note_id: str, ext: Extraction, overrides: dict, correction: str | None) -> None:
        recent = self.recent.get(note_id)
        if recent is None or recent.note is None:
            return
        note = recent.note
        day = self.local_day(note["created_at"])
        if recent.payload and recent.payload.get("extraction"):
            undo_almanac(self.almanac, note_id, prune_people=False)
        res, alm_out, event_id = apply_almanac(self.almanac, ext, note_id, day, overrides, note.get("event_id"))
        names = self.almanac.names()
        mirror_line = self._mirror_line(ext)
        readback = compose_readback(ext, res, names, alm_out.statuses, alm_out.follow_ups, set(),
                                    alm_out.event_title, mirror_line, True)
        payload = {
            "note": note,
            "extraction": ext.model_dump(),
            "overrides": overrides,
            "resolution": res.as_dict(),
            "event_id": event_id,
            "readback": readback,
            "question": res.question,
            "correction": correction,
            "mirror_line": mirror_line,
        }
        self._reseal(note_id, payload)
        prune_unsupported_people(self.almanac)
        self._remember(note_id, status="done", readback=readback, question=res.question,
                       new_people=res.created, error=None, payload=payload)

    def _reseal(self, note_id: str, payload: dict) -> None:
        with self.almanac.tx() as c:
            c.execute("UPDATE deep_inbox SET sealed = ? WHERE note_id = ?", (self.vault.seal(payload), note_id))

    # -------------------------------------------------------------------- drain
    def drain(self) -> int:
        """Apply sealed captures to the Deep store. Runs on the worker after an unlock."""
        if not self.vault.is_open:
            return 0
        count = 0
        for row in self.almanac.query("SELECT id, note_id, sealed FROM deep_inbox ORDER BY id"):
            try:
                payload = self.vault.open_sealed(row["sealed"])
            except DeepLocked:
                return count
            note = payload["note"]
            if not self.vault.store.one("SELECT 1 FROM notes WHERE id = ?", (note["id"],)):
                with self.vault.tx() as c:
                    c.execute(
                        "INSERT INTO notes (id, seq, created_at, raw_text, input_type, event_id, extraction_status)"
                        " VALUES (:id, :seq, :created_at, :raw_text, :input_type, :event_id, 'pending')",
                        note,
                    )
            if payload.get("undone"):
                with self.vault.tx() as c:
                    c.execute("UPDATE notes SET extraction_status = 'undone' WHERE id = ?", (note["id"],))
            elif payload.get("extraction"):
                self._apply_sealed(note, payload)
            else:
                self.jobs.submit(self._extract, note["id"], None, None)
            with self.almanac.tx() as c:
                c.execute("DELETE FROM deep_inbox WHERE id = ?", (row["id"],))
            with self._recent_lock:
                self.recent.pop(note["id"], None)
            count += 1
        for r in self.vault.store.query("SELECT id FROM notes WHERE extraction_status IN ('pending', 'extracting')"):
            self.jobs.submit(self._extract, r["id"], None, None)
        return count

    def _apply_sealed(self, note: dict, payload: dict) -> None:
        ext = Extraction.model_validate(payload["extraction"])
        res = Resolution.from_dict(payload["resolution"])
        note_id = note["id"]
        day = self.local_day(note["created_at"])
        names = self.almanac.names()
        # Drop refs to people deleted since (e.g. forgotten) rather than failing the whole note.
        res.refs = {k: v for k, v in res.refs.items() if v in names or k == "ME"}
        ext = _prune_refs(ext, set(res.refs))
        with self.vault.tx() as c:
            deep_out = apply_deep(c, ext, res, note_id, day, names)
            question = res.question or deep_out.question
            if payload.get("mirror_line"):
                c.execute(
                    "INSERT INTO mirror_log (created_at, context, pattern, text, note_id) VALUES (?, 'capture', ?, ?, ?)",
                    (utcnow(), ext.mirror.pattern, payload["mirror_line"], note_id),
                )
            readback = compose_readback(ext, res, names, deep_out.statuses, {}, deep_out.new_topics,
                                        None, payload.get("mirror_line"), False)
            c.execute(
                "INSERT INTO extractions (note_id, created_at, raw_json, correction, overrides, resolution) VALUES (?, ?, ?, ?, ?, ?)",
                (note_id, utcnow(), ext.model_dump_json(), payload.get("correction"), jdump(payload.get("overrides", {})),
                 jdump(res.as_dict())),
            )
            if question:
                c.execute(
                    "INSERT INTO questions (note_id, kind, prompt, options, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (note_id, question["kind"], question["prompt"], jdump(question["options"]), jdump(question), utcnow()),
                )
            c.execute(
                "UPDATE notes SET extraction_status = 'done', summary = ?, readback = ?, event_id = ? WHERE id = ?",
                (_summary(ext.summary, names), payload.get("readback") or readback, payload.get("event_id"), note_id),
            )
            if payload.get("correction"):
                from .golden import canonical

                c.execute(
                    "INSERT INTO golden_cases (note_id, note_text, correction, expected, created_at) VALUES (?, ?, ?, ?, ?)",
                    (note_id, note["raw_text"], payload["correction"], jdump(canonical(ext, res, names)), utcnow()),
                )

    # -------------------------------------------------------------- the readback
    def status(self, note_id: str) -> dict:
        note = self._deep_note(note_id)
        if note:
            q = self.vault.store.one(
                "SELECT id, prompt, options FROM questions WHERE note_id = ? AND answered_at IS NULL ORDER BY id DESC LIMIT 1",
                (note_id,),
            )
            res = self.vault.store.one(
                "SELECT resolution FROM extractions WHERE note_id = ? AND active = 1 ORDER BY id DESC LIMIT 1", (note_id,)
            )
            created = jload(res["resolution"], {}).get("created", []) if res else []
            mirror = self.vault.store.one(
                "SELECT id, text FROM mirror_log WHERE note_id = ? AND dismissed_at IS NULL AND rating IS NULL ORDER BY id DESC LIMIT 1",
                (note_id,),
            )
            return {
                "mirror": {"id": mirror["id"], "text": mirror["text"]} if mirror else None,
                "note_id": note_id,
                "status": note["extraction_status"],
                "readback": note["readback"],
                "question": {"prompt": q["prompt"], "options": jload(q["options"], [])} if q else None,
                "new_people": self._people_brief(created),
                "error": note["last_error"],
                "text": note["raw_text"],
                "can_correct": True,
            }
        r = self.recent.get(note_id)
        if r:
            q = r.question
            return {
                "note_id": note_id,
                "status": r.status,
                "readback": r.readback,
                "question": {"prompt": q["prompt"], "options": q["options"]} if q else None,
                "new_people": self._people_brief(r.new_people),
                "error": r.error,
                "text": r.note["raw_text"] if r.note else None,
                "can_correct": r.note is not None,
            }
        sealed = self.almanac.one("SELECT 1 FROM deep_inbox WHERE note_id = ?", (note_id,))
        if sealed:
            return {"note_id": note_id, "status": "sealed", "readback": None, "question": None, "new_people": [],
                    "error": None, "text": None, "can_correct": False}
        return {"note_id": note_id, "status": "unknown", "readback": None, "question": None, "new_people": [],
                "error": None, "text": None, "can_correct": False}

    def _people_brief(self, ids: list[int]) -> list[dict]:
        out = []
        for pid in ids:
            p = self.almanac.person(pid)
            if p:
                out.append({"id": p["id"], "name": p["display_name"], "layer": p["layer"]})
        return out

    # ------------------------------------------------------- correct, undo, answer
    def correct(self, note_id: str, correction: str) -> None:
        correction = correction.strip()
        if not correction:
            raise ValueError("Say what's wrong")
        if self._deep_note(note_id):
            self._set_status(note_id, "extracting")
            self.jobs.submit(self._extract, note_id, None, correction)
            return
        r = self.recent.get(note_id)
        if r is None or r.note is None:
            raise DeepLocked()
        self._remember(note_id, status="extracting")
        self.jobs.submit(self._extract, note_id, r.note, correction)

    def _set_status(self, note_id: str, status: str) -> None:
        with self.vault.tx() as c:
            c.execute("UPDATE notes SET extraction_status = ? WHERE id = ?", (status, note_id))

    def undo(self, note_id: str) -> None:
        self.jobs.call(self._undo, note_id)

    def _undo(self, note_id: str) -> None:
        if self._deep_note(note_id):
            undo_almanac(self.almanac, note_id)
            with self.vault.tx() as c:
                undo_deep(c, note_id)
                c.execute("UPDATE notes SET extraction_status = 'undone', readback = NULL WHERE id = ?", (note_id,))
            return
        r = self.recent.get(note_id)
        if r is None or r.note is None:
            raise DeepLocked()
        undo_almanac(self.almanac, note_id)
        self._reseal(note_id, {"note": r.note, "undone": True})
        self._remember(note_id, status="undone", readback=None, question=None, new_people=[], payload=None)

    def retry(self, note_id: str) -> None:
        if self._deep_note(note_id):
            self._set_status(note_id, "extracting")
            self.jobs.submit(self._extract, note_id, None, None)
            return
        r = self.recent.get(note_id)
        if r is None or r.note is None:
            raise DeepLocked()
        self._remember(note_id, status="extracting", error=None)
        self.jobs.submit(self._extract, note_id, r.note, None)

    def answer(self, note_id: str, value: str) -> dict:
        return self.jobs.call(self._answer, note_id, value)

    def _answer(self, note_id: str, value: str) -> dict:
        if self._deep_note(note_id):
            row = self.vault.store.one(
                "SELECT * FROM questions WHERE note_id = ? AND answered_at IS NULL ORDER BY id DESC LIMIT 1", (note_id,)
            )
            if row is None:
                raise ValueError("No open question for this note")
            q = jload(row["payload"], {})
            valid = {o["value"] for o in q.get("options", [])}
            if value not in valid:
                raise ValueError("Not one of the options")
            with self.vault.tx() as c:
                c.execute("UPDATE questions SET answered_at = ?, answer = ? WHERE id = ?", (utcnow(), value, row["id"]))
                if q["kind"] == "contradiction" and q.get("interaction_id"):
                    c.execute(
                        "UPDATE interactions SET status = ? WHERE id = ?",
                        ("corroborated" if value == "real" else "retired", q["interaction_id"]),
                    )
                elif q["kind"] == "claim_check":
                    confirmed = value == "confirmed"
                    c.execute(
                        "UPDATE claims SET status = ?, credibility = ?, resolved_by_note_id = ? WHERE id = ?",
                        ("confirmed" if confirmed else "contradicted", 1 if confirmed else 5, note_id, q["claim_id"]),
                    )
                    if q.get("new_claim_id"):
                        c.execute("UPDATE claims SET credibility = ? WHERE id = ?", (2 if confirmed else 4, q["new_claim_id"]))
            if q["kind"] in ("ambiguous_person", "possible_duplicate") and value != q.get("guess"):
                ext_row = self.vault.store.one(
                    "SELECT raw_json, overrides, correction FROM extractions WHERE note_id = ? AND active = 1 ORDER BY id DESC LIMIT 1",
                    (note_id,),
                )
                ext = Extraction.model_validate_json(ext_row["raw_json"])
                overrides = {**jload(ext_row["overrides"], {}), q["ref"]: value}
                mirror_line = self.vault.store.scalar(
                    "SELECT text FROM mirror_log WHERE note_id = ? ORDER BY id DESC LIMIT 1", (note_id,))
                self._commit_open(note_id, _answered(ext), overrides, ext_row["correction"],
                                  mirror_line=mirror_line, use_mirror=False)
                with self.vault.tx() as c:
                    c.execute("DELETE FROM questions WHERE note_id = ? AND answered_at IS NULL", (note_id,))
            return self.status(note_id)
        r = self.recent.get(note_id)
        if r is None or not r.question or not r.payload:
            raise DeepLocked()
        q = r.question
        if value not in {o["value"] for o in q["options"]}:
            raise ValueError("Not one of the options")
        if value != q.get("guess"):
            ext = Extraction.model_validate(r.payload["extraction"])
            overrides = {**r.payload.get("overrides", {}), q["ref"]: value}
            self._commit_locked(note_id, ext, overrides, r.payload.get("correction"))
        payload = {**self.recent[note_id].payload, "question": None}
        self._reseal(note_id, payload)
        self._remember(note_id, question=None, payload=payload)
        return self.status(note_id)


class _Closed:
    """Stand-in vault for building a locked-mode context."""

    is_open = False


def _summary(text: str, names: dict[int, str]) -> str:
    from .commit import _decode

    return _decode(text, names)


def _answered(ext: Extraction) -> Extraction:
    """After the author answers who someone is, don't ask again on re-commit."""
    data = ext.model_dump()
    for p in data["people"]:
        p["candidates"] = []
    return Extraction.model_validate(data)


def _prune_refs(ext: Extraction, valid: set[str]) -> Extraction:
    data = ext.model_dump()
    person_fields = {
        "interactions": ("actor", "target"), "edges": ("from_ref", "to_ref"), "trust": ("truster", "trustee"),
        "claims": ("source",), "motives": ("person",), "key_dates": ("person",), "gestures": ("person",),
        "contacts": ("person",), "assessments": ("person",), "profiles": ("person",),
    }
    for key, fields in person_fields.items():
        data[key] = [x for x in data[key] if all(x[f] in valid for f in fields)]
    for x in data["interactions"]:
        if x["relayed_by"] and x["relayed_by"] not in valid:
            x["relayed_by"] = ""
    for x in data["claims"]:
        x["about"] = [a for a in x["about"] if a in valid]
    for t in data["topics"]:
        t["people"] = [p for p in t["people"] if p["person"] in valid]
    for s in data["self_observations"]:
        s["people"] = [p for p in s["people"] if p in valid]
    for e in data["events"]:
        e["attendees"] = [a for a in e["attendees"] if a in valid]
    data["people"] = [p for p in data["people"] if p["ref"] in valid]
    return Extraction.model_validate(data)
