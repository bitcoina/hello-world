import { api, get, patch, post } from "../api.js";
import { h, spinner, toast } from "../ui.js";
import { isDeep, state } from "../main.js";
import { mirrorCard } from "./common.js";

const BUSY = new Set(["extracting", "transcribing", "saved"]);

export async function renderCapture(root) {
  let events = [];
  try {
    events = (await get("/api/events")).slice(0, 12);
  } catch { /* tagging is optional */ }
  const text = h("textarea", {
    class: "capture-box", rows: 6, placeholder: "What happened? Who said what, and how did it land?",
    "aria-label": "Note", autocapitalize: "sentences",
  });
  const eventSel = h("select", { "aria-label": "Tag an event" },
    h("option", { value: "" }, "No event"),
    events.map((e) => h("option", { value: e.id }, `${e.title}${e.date ? " · " + e.date : ""}`)));
  const save = h("button", { class: "btn primary", type: "button" }, "Save");
  const results = h("div", { class: "stack" });
  const timers = new Set();

  async function submit() {
    const value = text.value.trim();
    if (!value) return text.focus();
    save.disabled = true;
    try {
      const st = await post("/api/capture", { text: value, event_id: eventSel.value ? Number(eventSel.value) : null });
      text.value = "";
      results.prepend(captureCard(st, timers));
    } catch (e) {
      toast(e.message);
    } finally {
      save.disabled = false;
      text.focus();
    }
  }
  save.addEventListener("click", submit);
  text.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit(); });

  root.append(
    h("div", { class: "stack" },
      text,
      state.session.voice ? talkButton(eventSel, results, timers) : null,
      h("div", { class: "row spread" }, events.length ? eventSel : h("span"), save),
      isDeep() ? null : h("p", { class: "faint" }, "Private is closed: the note is sealed as it arrives. Dates, gifts and people are filed now; the rest waits until you next open Private."),
      results));
  text.focus();
  return () => timers.forEach((t) => clearTimeout(t));
}

function talkButton(eventSel, results, timers) {
  const btn = h("button", { class: "talk", type: "button" }, micIcon(), h("span", {}, "Hold to talk"));
  let rec = null;
  let chunks = [];
  let stream = null;

  async function start(e) {
    e.preventDefault();
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      toast("Microphone not available here.");
      return;
    }
    chunks = [];
    rec = new MediaRecorder(stream);
    rec.ondataavailable = (ev) => { if (ev.data.size) chunks.push(ev.data); };
    rec.onstop = upload;
    rec.start();
    btn.classList.add("recording");
    btn.lastChild.textContent = "Recording… release to save";
  }

  function stop() {
    if (rec && rec.state === "recording") rec.stop();
    stream?.getTracks().forEach((t) => t.stop());
    btn.classList.remove("recording");
    btn.lastChild.textContent = "Hold to talk";
  }

  async function upload() {
    const blob = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
    chunks = [];
    if (blob.size < 2000) {
      toast("Too short. Hold the button while you talk.");
      return;
    }
    const form = new FormData();
    form.append("audio", blob, "note");
    if (eventSel.value) form.append("event_id", eventSel.value);
    try {
      const st = await api("/api/capture/voice", { method: "POST", form });
      results.prepend(captureCard(st, timers));
    } catch (e) {
      toast(e.message);
    }
  }

  btn.addEventListener("pointerdown", start);
  btn.addEventListener("pointerup", stop);
  btn.addEventListener("pointerleave", stop);
  btn.addEventListener("pointercancel", stop);
  btn.addEventListener("contextmenu", (e) => e.preventDefault());
  return btn;
}

function micIcon() {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  for (const d of ["M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3z", "M5 11a7 7 0 0 0 14 0", "M12 18v3"]) {
    const p = document.createElementNS(ns, "path");
    p.setAttribute("d", d);
    svg.append(p);
  }
  return svg;
}

function captureCard(initial, timers) {
  const card = h("article", { class: "card readback-card stack" });
  let st = initial;

  function poll() {
    const t = setTimeout(async () => {
      timers.delete(t);
      try {
        st = await get(`/api/notes/${st.note_id}/status`);
        render();
      } catch {
        poll();
      }
    }, 1500);
    timers.add(t);
  }

  function render() {
    card.replaceChildren();
    const head = h("div", { class: "row spread" }, h("span", { class: "section-label" }, `${st.note_id}${st.status === "undone" ? " · undone" : ""}`));
    card.append(head);
    if (st.status === "transcribing") {
      card.append(spinner("Transcribing on the Pi…"));
      return poll();
    }
    if (BUSY.has(st.status)) {
      card.append(spinner("Reading…"));
      return poll();
    }
    if (st.status === "pending") {
      card.append(h("p", { class: "muted" }, "Saved. The reading will happen when the Claude API is reachable."),
        st.error ? h("p", { class: "faint" }, st.error) : null,
        h("div", { class: "row" }, h("button", { class: "btn small", type: "button", onclick: retry }, "Try now")));
      return;
    }
    if (st.status === "failed") {
      card.append(h("p", { class: "muted" }, "Saved, but I couldn't file it. Nothing was written."),
        st.error ? h("p", { class: "faint" }, st.error) : null,
        h("div", { class: "row" }, h("button", { class: "btn small", type: "button", onclick: retry }, "Try again")));
      return;
    }
    if (st.status === "sealed") {
      card.append(h("p", { class: "muted" }, "Saved and sealed. Open Private to see the reading."));
      return;
    }
    if (st.status === "undone") {
      card.append(h("p", { class: "muted" }, "Undone. The note is kept; nothing derived from it remains."),
        st.can_correct ? h("div", { class: "row" }, h("button", { class: "btn small", type: "button", onclick: retry }, "Read it again")) : null);
      return;
    }
    card.append(h("p", { class: "readback" }, st.readback || "Saved."));
    if (st.mirror) card.append(mirrorCard(st.mirror, { compact: true }));
    for (const p of st.new_people || []) card.append(layerPicker(p));
    if (st.question) card.append(question());
    if (st.can_correct) card.append(correction());
  }

  async function retry() {
    st = await post(`/api/notes/${st.note_id}/retry`);
    render();
  }

  function question() {
    const box = h("div", { class: "question" }, h("p", {}, st.question.prompt));
    const row = h("div", { class: "row" });
    for (const o of st.question.options) {
      row.append(h("button", { class: "btn small", type: "button", onclick: async () => {
        try {
          st = await post(`/api/notes/${st.note_id}/answer`, { value: o.value });
          render();
        } catch (e) { toast(e.message); }
      } }, o.label));
    }
    box.append(row, h("p", { class: "faint" }, "Skip it and the item just stays provisional."));
    return box;
  }

  function correction() {
    const input = h("textarea", { rows: 2, placeholder: "That's not right… say what's wrong in plain words" });
    const fix = h("button", { class: "btn small", type: "button" }, "Fix it");
    const undo = h("button", { class: "btn small ghost", type: "button" }, "Undo");
    fix.addEventListener("click", async () => {
      if (!input.value.trim()) return input.focus();
      fix.disabled = true;
      try {
        st = await post(`/api/notes/${st.note_id}/correct`, { text: input.value.trim() });
        render();
      } catch (e) {
        toast(e.message);
        fix.disabled = false;
      }
    });
    undo.addEventListener("click", async () => {
      try {
        st = await post(`/api/notes/${st.note_id}/undo`);
        render();
      } catch (e) { toast(e.message); }
    });
    return h("div", { class: "stack" }, input, h("div", { class: "row" }, fix, undo));
  }

  function layerPicker(p) {
    const row = h("div", { class: "row" }, h("span", { class: "faint" }, `How close is ${p.name}?`));
    const opts = [[5, "Inner 5"], [15, "15"], [50, "50"], [150, "150"]];
    for (const [layer, label] of opts) {
      const b = h("button", { class: ["btn small", p.layer === layer ? "primary" : ""], type: "button" }, label);
      b.addEventListener("click", async () => {
        await patch(`/api/people/${p.id}`, { layer });
        p.layer = layer;
        for (const other of row.querySelectorAll("button")) other.classList.remove("primary");
        b.classList.add("primary");
      });
      row.append(b);
    }
    return row;
  }

  render();
  return card;
}
