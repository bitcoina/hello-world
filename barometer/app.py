"""HTTP API and static files."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import Session, add_user, check_login
from .core import Barometer
from .crypto import BadPassphrase
from .db import jload, parse_date, utcnow
from .deep import DeepLocked
from .llm import LLMError, LLMUnavailable
from .relationships import today_view

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
COOKIE = "bm_session"
MAX_AUDIO_BYTES = 25 * 1024 * 1024


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------
class LoginIn(BaseModel):
    username: str = Field(max_length=80)
    password: str = Field(max_length=200)


class PassIn(BaseModel):
    passphrase: str = Field(max_length=500)


class CaptureIn(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    event_id: int | None = None


class CorrectIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class AnswerIn(BaseModel):
    value: str = Field(max_length=40)


class PersonIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)
    group: Literal["family", "friends", "work-adjacent"] | None = None
    layer: Literal[5, 15, 50, 150] | None = None
    aliases: list[str] = Field(default_factory=list, max_length=20)


class PersonPatch(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    group: Literal["family", "friends", "work-adjacent"] | None = None
    layer: Literal[5, 15, 50, 150, 0] | None = None
    aliases: list[str] | None = Field(default=None, max_length=20)


class KeyDateIn(BaseModel):
    kind: Literal["birthday", "anniversary", "bereavement", "hospital", "exam", "interview", "house move", "new job", "result", "other"]
    label: str = Field(min_length=1, max_length=120)
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    recurring: bool = False


class GestureIn(BaseModel):
    kind: Literal["text", "gift", "photo", "article", "coffee", "call", "visit", "other"] = "gift"
    what: str = Field(min_length=1, max_length=200)
    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    landed: str | None = Field(default=None, max_length=200)


class ContactIn(BaseModel):
    initiated_by: Literal["me", "them", "mutual"] = "me"
    channel: str = Field(default="", max_length=40)


class EventIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    location: str | None = Field(default=None, max_length=120)
    type: Literal["gathering", "1:1", "message thread"] = "gathering"
    attendee_ids: list[int] = Field(default_factory=list, max_length=60)


class AnalyseIn(BaseModel):
    mode: Literal["brief", "explain", "record", "reflect"]
    person_ids: list[int] = Field(default_factory=list, max_length=40)
    event_id: int | None = None
    note_ids: list[str] = Field(default_factory=list, max_length=30)
    query: str = Field(default="", max_length=1000)
    deep: bool = False
    start: str | None = None
    end: str | None = None


class ScoreIn(BaseModel):
    score: Literal[0, 0.5, 1]
    actual: str = Field(default="", max_length=1000)


class AssessmentPatch(BaseModel):
    your_take: str | None = Field(default=None, max_length=2000)
    retired: bool | None = None


class RateIn(BaseModel):
    useful: bool


class RetireIn(BaseModel):
    target: str = Field(pattern=r"^[ID]\d+$")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
def create_app(bm: Barometer, start_background: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app):
        if start_background:
            bm.start()
        yield
        bm.stop()

    app = FastAPI(title="Barometer", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.bm = bm

    @app.middleware("http")
    async def security(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD"):
            # Custom header + SameSite=Strict cookie: a cross-site form can't forge either.
            if request.headers.get("x-barometer") != "1":
                return JSONResponse({"detail": "Missing request header"}, status_code=403)
        response = await call_next(request)
        h = response.headers
        h["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self'; "
            "script-src 'self'; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'self'; object-src 'none'"
        )
        h["X-Frame-Options"] = "DENY"
        h["X-Content-Type-Options"] = "nosniff"
        h["Referrer-Policy"] = "no-referrer"
        h["Permissions-Policy"] = "camera=(), geolocation=(), microphone=(self)"
        if request.url.path.startswith("/api/") or request.url.path == "/":
            h["Cache-Control"] = "no-store"
        return response

    # -- dependencies --------------------------------------------------------------
    def current(request: Request) -> Session | None:
        return bm.sessions.get(request.cookies.get(COOKIE))

    def user(s: Session | None = Depends(current)) -> Session:
        if s is None:
            raise HTTPException(401, "Signed out")
        return s

    def owner(s: Session = Depends(user)) -> Session:
        if not s.is_owner:
            raise HTTPException(403, "Not available on this login")
        return s

    def deep(s: Session = Depends(owner)) -> Session:
        if not (s.deep and bm.vault.is_open):
            s.deep = False
            raise HTTPException(423, "Private is locked")
        bm.vault.touch()
        return s

    def note_access(note_id: str, s: Session = Depends(owner)) -> Session:
        """Readbacks are Deep content: visible to a session that opened Private, or that wrote the note."""
        if not (s.deep and bm.vault.is_open) and note_id not in s.captured:
            raise HTTPException(423, "Private is locked")
        if s.deep:
            bm.vault.touch()
        return s

    def today() -> date:
        return datetime.now(bm.settings.tz).date()

    def adopt(c, pid: int) -> None:
        """A person edited by hand is never removed by undoing a note."""
        c.execute("UPDATE people SET source = 'form', updated_at = ? WHERE id = ? AND is_self = 0", (utcnow(), pid))

    def need_person(pid: int) -> dict:
        p = bm.almanac.person(pid)
        if p is None or p["is_self"]:
            raise HTTPException(404, "No such person")
        return p

    @app.exception_handler(DeepLocked)
    async def _locked(_req, _exc):
        return JSONResponse({"detail": "Private is locked"}, status_code=423)

    @app.exception_handler(LLMUnavailable)
    async def _llm_down(_req, exc):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(LLMError)
    async def _llm_err(_req, exc):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    # -- session -------------------------------------------------------------------
    @app.get("/api/session")
    def session(s: Session | None = Depends(current)):
        base = {
            "authenticated": s is not None,
            "deep_initialised": bm.vault.initialised,
            "voice": bm.pipeline.transcriber.available,
            "llm": bm.llm.name,
            "idle_seconds": bm.settings.session_idle_seconds,
        }
        if s:
            if s.deep and not bm.vault.is_open:
                s.deep = False
            base.update({"username": s.username, "role": s.role, "deep": s.deep})
        return base

    @app.post("/api/login")
    def login(body: LoginIn, response: Response):
        key = body.username.strip().lower()
        wait = bm.login_throttle.wait_for(key)
        if wait > 0:
            raise HTTPException(429, f"Too many attempts. Try again in {int(wait) + 1}s")
        row = check_login(bm.almanac, body.username, body.password)
        if row is None:
            bm.login_throttle.failed(key)
            raise HTTPException(401, "That didn't match")
        bm.login_throttle.succeeded(key)
        s = bm.sessions.create(row)
        response.set_cookie(COOKIE, s.token, httponly=True, samesite="strict", secure=bm.settings.secure_cookies, path="/")
        return {"ok": True, "role": s.role}

    @app.post("/api/logout")
    def logout(request: Request, response: Response, s: Session | None = Depends(current)):
        if s and s.deep:
            bm.lock()
        bm.sessions.destroy(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.post("/api/deep/unlock")
    def unlock(body: PassIn, s: Session = Depends(owner)):
        wait = bm.deep_throttle.wait_for("deep")
        if wait > 0:
            raise HTTPException(429, f"Too many attempts. Try again in {int(wait) + 1}s")
        # Flag the session first so housekeeping never sees an open vault with no Private session.
        s.deep = True
        try:
            bm.unlock(body.passphrase)
        except BadPassphrase:
            s.deep = False
            bm.deep_throttle.failed("deep")
            raise HTTPException(401, "That didn't match")
        bm.deep_throttle.succeeded("deep")
        return {"ok": True, "sealed_waiting": bm.almanac.scalar("SELECT count(*) FROM deep_inbox", (), 0)}

    @app.post("/api/deep/lock")
    def lock(_s: Session = Depends(owner)):
        bm.lock()
        return {"ok": True}

    @app.post("/api/panic")
    def panic(_s: Session = Depends(user)):
        bm.lock()
        return {"ok": True}

    # -- Today ---------------------------------------------------------------------
    @app.get("/api/today")
    def get_today(s: Session = Depends(user)):
        view = today_view(bm.almanac, today())
        view["questions"] = []
        view["monthly_reflect"] = False
        view["open_predictions"] = []
        if s.deep and bm.vault.is_open:
            store = bm.vault.store
            view["questions"] = [
                {"note_id": q["note_id"], "prompt": q["prompt"], "options": jload(q["options"], [])}
                for q in store.query("SELECT * FROM questions WHERE answered_at IS NULL ORDER BY id DESC LIMIT 5")
            ]
            view["monthly_reflect"] = today().day <= 7
            view["open_predictions"] = [
                dict(r) for r in store.query(
                    "SELECT o.id, o.description, o.predicted_outcome, o.event_id FROM self_observations o"
                    " WHERE o.kind = 'prediction' AND o.score IS NULL AND o.brief_id IS NOT NULL ORDER BY o.id DESC LIMIT 3")
            ]
        return view

    @app.post("/api/nudges/{nid}/{action}")
    def nudge(nid: int, action: Literal["done", "dismiss"], _s: Session = Depends(user)):
        col = "done_at" if action == "done" else "dismissed_at"
        with bm.almanac.tx() as c:
            c.execute(f"UPDATE nudges SET {col} = ? WHERE id = ?", (utcnow(), nid))
            row = c.execute("SELECT person_id FROM nudges WHERE id = ?", (nid,)).fetchone()
            if action == "done" and row and row["person_id"]:
                c.execute(
                    "INSERT INTO contacts (person_id, date, initiated_by, channel, source, created_at) VALUES (?, ?, 'me', 'nudge', 'form', ?)",
                    (row["person_id"], today().isoformat(), utcnow()),
                )
        return {"ok": True}

    @app.post("/api/followups/{fid}/{action}")
    def followup(fid: int, action: Literal["done", "dismiss"], _s: Session = Depends(user)):
        col = "done_at" if action == "done" else "dismissed_at"
        with bm.almanac.tx() as c:
            c.execute(f"UPDATE follow_ups SET {col} = ? WHERE id = ?", (utcnow(), fid))
        return {"ok": True}

    # -- People (Almanac) ------------------------------------------------------------
    @app.get("/api/people")
    def people(_s: Session = Depends(user)):
        from .relationships import last_contacts

        last = last_contacts(bm.almanac)
        out = []
        for p in bm.almanac.people():
            out.append({
                "id": p["id"], "name": p["display_name"], "group": p["grp"], "layer": p["layer"],
                "aliases": p["aliases"], "last_contact": last[p["id"]].isoformat() if p["id"] in last else None,
                "updated_at": p["updated_at"],
            })
        return out

    @app.post("/api/people")
    def add_person(body: PersonIn, _s: Session = Depends(user)):
        now = utcnow()
        with bm.almanac.tx() as c:
            cur = c.execute(
                "INSERT INTO people (display_name, aliases, grp, layer, source, created_at, updated_at) VALUES (?, ?, ?, ?, 'form', ?, ?)",
                (body.display_name.strip(), jdump_list(body.aliases), body.group, body.layer, now, now),
            )
        return {"id": cur.lastrowid}

    @app.get("/api/people/{pid}")
    def person(pid: int, s: Session = Depends(user)):
        from .relationships import last_contacts, upcoming_dates

        p = need_person(pid)
        alm = bm.almanac
        names = alm.names()
        family = []
        self_id = alm.self_id()
        for r in alm.query("SELECT * FROM family_links WHERE from_id = ? OR to_id = ?", (pid, pid)):
            rel = r["relation"]
            if r["from_id"] == pid:
                other = r["to_id"]
                text = f"Your {rel}" if other == self_id else f"{rel.capitalize()} of {names.get(other, '?')}"
            else:
                other = r["from_id"]
                text = f"You are their {rel}" if other == self_id else f"{names.get(other, '?')} is their {rel}"
            family.append({"person_id": None if other == self_id else other, "text": text, "status": r["status"]})
        out = {
            "id": p["id"], "name": p["display_name"], "aliases": p["aliases"], "group": p["grp"], "layer": p["layer"],
            "source": p["source"],
            "key_dates": [dict(r) for r in alm.query("SELECT id, kind, label, date, recurring, status, source FROM key_dates WHERE person_id = ? ORDER BY date", (pid,))],
            "upcoming": upcoming_dates(alm, today(), 60, {pid}),
            "gestures": [dict(r) for r in alm.query("SELECT id, date, kind, what, landed FROM gestures WHERE person_id = ? ORDER BY date DESC LIMIT 30", (pid,))],
            "family": family,
            "events": [dict(r) for r in alm.query(
                "SELECT e.id, e.title, e.date FROM events e JOIN event_attendees a ON a.event_id = e.id WHERE a.person_id = ? ORDER BY e.date DESC LIMIT 10", (pid,))],
            "last_contact": (lambda d: d.isoformat() if d else None)(last_contacts(alm).get(pid)),
            "follow_ups": [dict(r) for r in alm.query(
                "SELECT id, due_date, prompt FROM follow_ups WHERE person_id = ? AND done_at IS NULL AND dismissed_at IS NULL AND due_date >= ? ORDER BY due_date",
                (pid, (today() - timedelta(days=14)).isoformat()))],
            "deep": None,
        }
        if s.deep and bm.vault.is_open:
            bm.vault.touch()
            out["deep"] = bm.analyst.person_deep(pid)
        return out

    @app.patch("/api/people/{pid}")
    def edit_person(pid: int, body: PersonPatch, _s: Session = Depends(user)):
        need_person(pid)
        fields = body.model_dump(exclude_unset=True)
        with bm.almanac.tx() as c:
            if "display_name" in fields and fields["display_name"]:
                c.execute("UPDATE people SET display_name = ? WHERE id = ?", (fields["display_name"].strip(), pid))
            if "group" in fields:
                c.execute("UPDATE people SET grp = ? WHERE id = ?", (fields["group"], pid))
            if "layer" in fields:
                c.execute("UPDATE people SET layer = ? WHERE id = ?", (fields["layer"] or None, pid))
            if "aliases" in fields and fields["aliases"] is not None:
                c.execute("UPDATE people SET aliases = ? WHERE id = ?", (jdump_list(fields["aliases"]), pid))
            adopt(c, pid)
        return {"ok": True}

    @app.post("/api/people/{pid}/contact")
    def contact(pid: int, body: ContactIn, _s: Session = Depends(user)):
        need_person(pid)
        with bm.almanac.tx() as c:
            c.execute(
                "INSERT INTO contacts (person_id, date, initiated_by, channel, source, created_at) VALUES (?, ?, ?, ?, 'form', ?)",
                (pid, today().isoformat(), body.initiated_by, body.channel, utcnow()),
            )
            adopt(c, pid)
        return {"ok": True}

    @app.post("/api/people/{pid}/key_dates")
    def add_key_date(pid: int, body: KeyDateIn, _s: Session = Depends(user)):
        need_person(pid)
        if parse_date(body.date) is None:
            raise HTTPException(422, "Bad date")
        recurring = body.recurring or body.kind in ("birthday", "anniversary", "bereavement")
        with bm.almanac.tx() as c:
            c.execute(
                "INSERT INTO key_dates (person_id, kind, label, date, recurring, status, source, created_at) VALUES (?, ?, ?, ?, ?, 'corroborated', 'form', ?)",
                (pid, body.kind, body.label.strip(), body.date, int(recurring), utcnow()),
            )
            adopt(c, pid)
        return {"ok": True}

    @app.delete("/api/key_dates/{kid}")
    def delete_key_date(kid: int, _s: Session = Depends(user)):
        with bm.almanac.tx() as c:
            c.execute("DELETE FROM key_dates WHERE id = ?", (kid,))
        return {"ok": True}

    @app.post("/api/people/{pid}/gestures")
    def add_gesture(pid: int, body: GestureIn, _s: Session = Depends(user)):
        need_person(pid)
        with bm.almanac.tx() as c:
            c.execute(
                "INSERT INTO gestures (person_id, date, kind, what, landed, source, created_at) VALUES (?, ?, ?, ?, ?, 'form', ?)",
                (pid, body.date or today().isoformat(), body.kind, body.what.strip(), body.landed, utcnow()),
            )
            adopt(c, pid)
        return {"ok": True}

    @app.delete("/api/people/{pid}")
    def forget(pid: int, _s: Session = Depends(deep)):
        from .forget import forget_person

        need_person(pid)
        bm.pipeline.jobs.call(bm.pipeline.drain)
        result = bm.pipeline.jobs.call(forget_person, bm.almanac, bm.vault, pid)
        return {"ok": True, **result}

    # -- Events ----------------------------------------------------------------------
    @app.get("/api/events")
    def events(_s: Session = Depends(user)):
        rows = bm.almanac.query("SELECT * FROM events ORDER BY coalesce(date, '9999') DESC LIMIT 100")
        out = []
        for e in rows:
            attendees = [r["person_id"] for r in bm.almanac.query("SELECT person_id FROM event_attendees WHERE event_id = ?", (e["id"],))]
            out.append({"id": e["id"], "title": e["title"], "date": e["date"], "location": e["location"], "type": e["type"], "attendee_ids": attendees})
        return out

    @app.post("/api/events")
    def add_event(body: EventIn, _s: Session = Depends(user)):
        with bm.almanac.tx() as c:
            cur = c.execute(
                "INSERT INTO events (title, date, location, type, source, created_at) VALUES (?, ?, ?, ?, 'form', ?)",
                (body.title.strip(), body.date, body.location, body.type, utcnow()),
            )
            for pid in body.attendee_ids:
                c.execute("INSERT OR IGNORE INTO event_attendees (event_id, person_id) VALUES (?, ?)", (cur.lastrowid, pid))
                adopt(c, pid)
        return {"id": cur.lastrowid}

    @app.put("/api/events/{eid}/attendees")
    def set_attendees(eid: int, body: EventIn, _s: Session = Depends(user)):
        with bm.almanac.tx() as c:
            c.execute("UPDATE events SET title = ?, date = ?, location = ?, type = ? WHERE id = ?",
                      (body.title, body.date, body.location, body.type, eid))
            c.execute("DELETE FROM event_attendees WHERE event_id = ?", (eid,))
            for pid in body.attendee_ids:
                c.execute("INSERT OR IGNORE INTO event_attendees (event_id, person_id) VALUES (?, ?)", (eid, pid))
        return {"ok": True}

    # -- Capture ---------------------------------------------------------------------
    @app.post("/api/capture")
    def capture(body: CaptureIn, s: Session = Depends(owner)):
        if not bm.vault.initialised:
            raise HTTPException(409, "Run `barometer init` first")
        note_id = bm.pipeline.capture_text(body.text, body.event_id)
        s.captured.add(note_id)
        if bm.vault.is_open:
            bm.vault.touch()
        return bm.pipeline.status(note_id)

    @app.post("/api/capture/voice")
    async def capture_voice(audio: UploadFile = File(...), event_id: int | None = Form(None), s: Session = Depends(owner)):
        if not bm.pipeline.transcriber.available:
            raise HTTPException(501, "Voice isn't set up on this Pi")
        data = await audio.read(MAX_AUDIO_BYTES + 1)
        if len(data) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "Recording too long")
        ctype = (audio.content_type or "").split(";")[0]
        suffix = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a", "audio/mpeg": ".mp3",
                  "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/aac": ".aac"}.get(ctype, ".webm")
        note_id = bm.pipeline.capture_voice(data, suffix, event_id)
        s.captured.add(note_id)
        return bm.pipeline.status(note_id)

    @app.get("/api/notes/{note_id}/status")
    def note_status(note_id: str, _s: Session = Depends(note_access)):
        st = bm.pipeline.status(note_id)
        if st["status"] == "unknown":
            raise HTTPException(404, "No such note")
        return st

    @app.post("/api/notes/{note_id}/correct")
    def correct(note_id: str, body: CorrectIn, _s: Session = Depends(note_access)):
        bm.pipeline.correct(note_id, body.text)
        return bm.pipeline.status(note_id)

    @app.post("/api/notes/{note_id}/undo")
    def undo(note_id: str, _s: Session = Depends(note_access)):
        bm.pipeline.undo(note_id)
        return bm.pipeline.status(note_id)

    @app.post("/api/notes/{note_id}/retry")
    def retry(note_id: str, _s: Session = Depends(note_access)):
        bm.pipeline.retry(note_id)
        return bm.pipeline.status(note_id)

    @app.post("/api/notes/{note_id}/answer")
    def answer(note_id: str, body: AnswerIn, _s: Session = Depends(note_access)):
        try:
            return bm.pipeline.answer(note_id, body.value)
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @app.get("/api/notes")
    def notes(person_id: int | None = None, limit: int = 30, _s: Session = Depends(deep)):
        store = bm.vault.store
        limit = max(1, min(limit, 200))
        if person_id:
            rows = store.query(
                "SELECT n.* FROM notes n JOIN note_people np ON np.note_id = n.id WHERE np.person_id = ? ORDER BY n.seq DESC LIMIT ?",
                (person_id, limit))
        else:
            rows = store.query("SELECT * FROM notes ORDER BY seq DESC LIMIT ?", (limit,))
        return [_note_out(r) for r in rows]

    @app.get("/api/notes/{note_id}")
    def note(note_id: str, _s: Session = Depends(deep)):
        row = bm.vault.store.one("SELECT * FROM notes WHERE id = ?", (note_id,))
        if row is None:
            raise HTTPException(404, "No such note")
        return _note_out(row)

    # -- Deep: analysis ----------------------------------------------------------------
    @app.get("/api/tray")
    def tray(_s: Session = Depends(deep)):
        return bm.analyst.tray()

    @app.post("/api/analyse")
    def analyse(body: AnalyseIn, _s: Session = Depends(deep)):
        a = bm.analyst
        try:
            if body.mode == "brief":
                return a.brief(body.person_ids, body.event_id, body.query, body.deep)
            if body.mode == "explain":
                return a.explain(body.person_ids, body.event_id, body.note_ids, body.query, body.deep)
            if body.mode == "record":
                return a.record_check(body.note_ids)
            end = parse_date(body.end) or today()
            start = parse_date(body.start) or (end - timedelta(days=30))
            return a.reflect(start, end)
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @app.get("/api/briefs")
    def briefs(event_id: int | None = None, _s: Session = Depends(deep)):
        sql = "SELECT * FROM briefs WHERE saved = 1" + (" AND event_id = ?" if event_id else "") + " ORDER BY id DESC LIMIT 20"
        rows = bm.vault.store.query(sql, (event_id,) if event_id else ())
        return [{"id": r["id"], "mode": r["mode"], "created_at": r["created_at"], "event_id": r["event_id"],
                 "output": jload(r["output"], {})} for r in rows]

    @app.post("/api/briefs/{bid}/save")
    def save_brief(bid: int, _s: Session = Depends(deep)):
        store = bm.vault.store
        row = store.one("SELECT * FROM briefs WHERE id = ?", (bid,))
        if row is None:
            raise HTTPException(404, "No such brief")
        out = jload(row["output"], {})
        pred_id = None
        with bm.vault.tx() as c:
            c.execute("UPDATE briefs SET saved = 1 WHERE id = ?", (bid,))
            existing = c.execute("SELECT id FROM self_observations WHERE brief_id = ?", (bid,)).fetchone()
            if existing:
                pred_id = existing["id"]
            elif row["mode"] == "brief" and out.get("prediction", {}).get("text"):
                cur = c.execute(
                    "INSERT INTO self_observations (kind, description, predicted_outcome, event_id, brief_id, person_ids, created_at)"
                    " VALUES ('prediction', ?, ?, ?, ?, ?, ?)",
                    (out["prediction"]["text"], out["prediction"]["text"], row["event_id"], bid, row["person_ids"], utcnow()),
                )
                pred_id = cur.lastrowid
        return {"ok": True, "prediction_id": pred_id}

    @app.post("/api/predictions/{oid}/score")
    def score(oid: int, body: ScoreIn, _s: Session = Depends(deep)):
        with bm.vault.tx() as c:
            c.execute(
                "UPDATE self_observations SET score = ?, actual_outcome = ?, scored_at = ? WHERE id = ? AND kind = 'prediction'",
                (body.score, body.actual or None, utcnow(), oid),
            )
        return {"ok": True}

    @app.get("/api/map")
    def graph_map(event_id: int | None = None, _s: Session = Depends(deep)):
        return bm.analyst.map_data(event_id)

    @app.get("/api/people/{pid}/assessments")
    def assessments(pid: int, _s: Session = Depends(deep)):
        need_person(pid)
        return bm.analyst.assessments(pid)

    @app.patch("/api/assessments/{aid}")
    def edit_assessment(aid: int, body: AssessmentPatch, _s: Session = Depends(deep)):
        with bm.vault.tx() as c:
            if body.your_take is not None:
                c.execute("UPDATE assessments SET your_take = ? WHERE id = ?", (body.your_take, aid))
            if body.retired is not None:
                c.execute("UPDATE assessments SET retired = ? WHERE id = ?", (int(body.retired), aid))
        return {"ok": True}

    @app.post("/api/record/retire")
    def retire(body: RetireIn, _s: Session = Depends(deep)):
        table = "interactions" if body.target[0] == "I" else "edges"
        with bm.vault.tx() as c:
            c.execute(f"UPDATE {table} SET status = 'retired' WHERE id = ?", (int(body.target[1:]),))
        return {"ok": True}

    @app.post("/api/mirror/{mid}/dismiss")
    def mirror_dismiss(mid: int, _s: Session = Depends(deep)):
        row = bm.vault.store.one("SELECT pattern FROM mirror_log WHERE id = ?", (mid,))
        if row is None:
            raise HTTPException(404, "No such warning")
        with bm.vault.tx() as c:
            c.execute("UPDATE mirror_log SET dismissed_at = ? WHERE id = ?", (utcnow(), mid))
        muted = bm.pipeline.mirror.dismissed(row["pattern"])
        return {"ok": True, "muted": muted}

    @app.post("/api/mirror/{mid}/rate")
    def mirror_rate(mid: int, body: RateIn, _s: Session = Depends(deep)):
        with bm.vault.tx() as c:
            c.execute("UPDATE mirror_log SET rating = ? WHERE id = ?", ("useful" if body.useful else "not_useful", mid))
        return {"ok": True, "threshold": bm.pipeline.mirror.rated(body.useful)}

    @app.get("/api/mirror/recent")
    def mirror_recent(_s: Session = Depends(deep)):
        rows = bm.vault.store.query("SELECT * FROM mirror_log WHERE dismissed_at IS NULL ORDER BY id DESC LIMIT 5")
        return [dict(r) for r in rows]

    # -- Static ----------------------------------------------------------------------
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/sw.js")
    def sw():
        return FileResponse(STATIC / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    @app.get("/manifest.webmanifest")
    def manifest():
        return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")

    return app


def jdump_list(items: list[str]) -> str:
    from .db import jdump

    return jdump([a.strip() for a in items if a.strip()])


def _note_out(r) -> dict:
    return {
        "id": r["id"], "created_at": r["created_at"], "text": r["raw_text"], "input_type": r["input_type"],
        "event_id": r["event_id"], "status": r["extraction_status"], "summary": r["summary"], "readback": r["readback"],
        "error": r["last_error"],
    }


def make_default_app() -> FastAPI:
    """Entry point for uvicorn: ``uvicorn barometer.app:make_default_app --factory``."""
    from .config import load_settings

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app(Barometer(load_settings()))


__all__ = ["create_app", "make_default_app", "add_user"]
