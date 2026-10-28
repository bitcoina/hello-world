import { get, post } from "../api.js";
import { field, fmtDay, h, initial, seg, spinner, toast, todayIso } from "../ui.js";
import { cited, mirrorCard } from "./common.js";
import { reflectOutput } from "./reflect.js";

const MODES = [["brief", "Brief"], ["explain", "Explain"], ["record", "Record check"], ["reflect", "Reflect"]];

export async function renderBrief(root) {
  const [tray, notes] = await Promise.all([get("/api/tray"), get("/api/notes?limit=15")]);
  const sel = { people: new Map(), event: null, notes: new Map() };
  let mode = "brief";

  const queryInput = h("input", { type: "text", placeholder: "Add people or an event, or ask a question", "aria-label": "Question" });
  const zone = h("div", { class: "dropzone", "aria-label": "Context" });
  const deepToggle = h("input", { type: "checkbox" });
  const deepLabel = h("label", { class: "toggle", title: "Draw on private assessments for this one query" }, deepToggle, "Deep brief");
  const start = h("input", { type: "date" });
  const end = h("input", { type: "date", value: todayIso() });
  const d0 = new Date();
  d0.setDate(d0.getDate() - 30);
  start.value = `${d0.getFullYear()}-${String(d0.getMonth() + 1).padStart(2, "0")}-${String(d0.getDate()).padStart(2, "0")}`;
  const range = h("div", { class: "row" }, field("From", start), field("To", end));
  const run = h("button", { class: "btn primary", type: "button" }, "Read the room");
  const output = h("div");
  const notesStrip = h("div", { class: "strip", "aria-label": "Recent notes" });

  function drawZone() {
    zone.replaceChildren();
    if (sel.event) zone.append(chip(sel.event.title, { event: true, selected: true, remove: () => { sel.event = null; drawZone(); } }));
    for (const [id, p] of sel.people) zone.append(chip(p.name, { layer: p.layer, selected: true, remove: () => { sel.people.delete(id); drawZone(); } }));
    for (const [id] of sel.notes) zone.append(chip(id, { event: true, selected: true, remove: () => { sel.notes.delete(id); drawZone(); } }));
    zone.append(queryInput);
  }

  function addPerson(p) { sel.people.set(p.id, p); drawZone(); }
  function addEvent(e) { sel.event = e; drawZone(); }

  zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("over"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("over"));
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("over");
    const [kind, id] = (e.dataTransfer.getData("text/plain") || "").split(":");
    if (kind === "p") { const p = tray.people.find((x) => String(x.id) === id); if (p) addPerson(p); }
    if (kind === "e") { const ev = tray.events.find((x) => String(x.id) === id); if (ev) addEvent(ev); }
  });

  const search = h("input", { type: "search", placeholder: "Find someone", "aria-label": "Find someone" });
  const peopleTray = h("div", { class: "tray", "aria-label": "People" });
  function drawTray() {
    const q = search.value.trim().toLowerCase();
    peopleTray.replaceChildren(...tray.people.filter((p) => !q || p.name.toLowerCase().includes(q)).map((p) =>
      chip(p.name, { layer: p.layer, drag: `p:${p.id}`, onclick: () => addPerson(p) })));
    if (!tray.people.length) peopleTray.append(h("p", { class: "empty" }, "People appear here as you write notes."));
  }
  search.addEventListener("input", drawTray);
  const eventStrip = h("div", { class: "strip", "aria-label": "Events" }, tray.events.map((e) =>
    chip(`${e.title}${e.date ? " · " + fmtDay(e.date, false) : ""}`, { event: true, drag: `e:${e.id}`, onclick: () => addEvent(e) })));
  for (const n of notes) {
    notesStrip.append(chip(`${n.id} · ${(n.summary || n.text).slice(0, 42)}`, { event: true, onclick: () => { sel.notes.set(n.id, n); drawZone(); } }));
  }

  const modeHelp = h("p", { class: "faint" });
  const extras = h("div", { class: "row spread" });
  function drawMode() {
    modeHelp.textContent = {
      brief: "Fault lines, what each wants, trust cautions, what to avoid, and one prediction. Under 250 words.",
      explain: "What happened, at least two competing explanations, and what would tell them apart.",
      record: "Checks notes against the record: what's missing, what contradicts it, what's gone stale. Uses the notes you add, or the last eight.",
      reflect: "Your misfires, wins, prediction accuracy and who usually gets in touch first.",
    }[mode];
    extras.replaceChildren(
      mode === "reflect" ? range : (mode === "brief" || mode === "explain" ? deepLabel : h("span")),
      run);
    run.textContent = { brief: "Read the room", explain: "Explain", record: "Check the record", reflect: "Reflect" }[mode];
    notesWrap.hidden = !(mode === "explain" || mode === "record");
    zoneWrap.hidden = mode === "reflect";
    trayWrap.hidden = mode === "reflect" || mode === "record";
  }

  run.addEventListener("click", async () => {
    const body = {
      mode,
      person_ids: [...sel.people.keys()],
      event_id: sel.event ? sel.event.id : null,
      note_ids: [...sel.notes.keys()],
      query: queryInput.value.trim(),
      deep: deepToggle.checked,
      start: start.value || null,
      end: end.value || null,
    };
    if (mode === "brief" && !body.person_ids.length && !body.event_id) {
      toast("Add an event or some people first.");
      return;
    }
    run.disabled = true;
    output.replaceChildren(h("div", { class: "card" }, spinner(mode === "brief" ? "Reading the room…" : "Thinking…")));
    output.scrollIntoView({ behavior: "smooth", block: "nearest" });
    try {
      const res = await post("/api/analyse", body);
      output.replaceChildren(renderOutput(res));
    } catch (e) {
      output.replaceChildren(h("div", { class: "card" }, h("p", { class: "muted" }, e.message)));
    } finally {
      run.disabled = false;
    }
  });

  const zoneWrap = h("div", { class: "stack" }, zone);
  const notesWrap = h("div", {}, h("p", { class: "section-label" }, "Notes"), notesStrip);
  const trayWrap = h("div", { class: "stack" },
    h("p", { class: "section-label" }, "Events"), eventStrip,
    h("p", { class: "section-label" }, "People"), search, peopleTray);

  root.append(h("div", { class: "stack" },
    zoneWrap,
    seg(MODES, mode, (m) => { mode = m; drawMode(); }),
    modeHelp,
    extras,
    output,
    notesWrap,
    trayWrap));
  drawZone();
  drawTray();
  drawMode();
}

function chip(label, { layer, event, selected, drag, remove, onclick } = {}) {
  const el = h("button", { type: "button", class: ["chip", event ? "event" : "", selected ? "selected" : ""] },
    event ? null : initial(label, layer), label,
    remove ? h("span", { class: "x", "aria-hidden": "true" }, "×") : null);
  if (remove) {
    el.setAttribute("aria-label", `Remove ${label}`);
    el.addEventListener("click", remove);
  }
  if (onclick) el.addEventListener("click", onclick);
  if (drag) {
    el.draggable = true;
    el.addEventListener("dragstart", (e) => { e.dataTransfer.setData("text/plain", drag); el.classList.add("dragging"); });
    el.addEventListener("dragend", () => el.classList.remove("dragging"));
  }
  return el;
}

export function renderOutput(res) {
  if (res.mode === "reflect") return reflectOutput(res);
  const card = h("article", { class: "card brief-card" });
  for (const sec of res.sections || []) {
    if (!sec.items.length && res.mode !== "record") continue;
    card.append(h("h3", {}, sec.title));
    if (!sec.items.length) {
      card.append(h("p", { class: "faint" }, "Nothing."));
      continue;
    }
    card.append(h("ul", {}, sec.items.map((it) => h("li", {}, cited(it.text, it.cites),
      it.target ? h("button", { class: "btn small ghost", type: "button", onclick: async (e) => {
        await post("/api/record/retire", { target: it.target });
        e.target.replaceWith(h("span", { class: "faint" }, " retired"));
      } }, "Retire") : null))));
  }
  if (res.mode === "explain") {
    card.append(h("h3", {}, "Competing explanations"));
    for (const x of res.explanations) {
      card.append(h("div", { class: "hypothesis" },
        h("p", {}, x.explanation, " ", h("span", { class: `conf conf-${x.confidence}` }, x.confidence)),
        x.supporting.length ? cited("For:", x.supporting, "p") : null,
        x.contradicting.length ? cited("Against:", x.contradicting, "p") : null));
    }
    if (res.tell_apart.length) {
      card.append(h("h3", {}, "What would tell them apart"), h("ul", {}, res.tell_apart.map((t) => h("li", {}, t))));
    }
  }
  if (res.mode === "brief" && res.prediction?.text) {
    const save = h("button", { class: "btn small", type: "button" }, "Save brief and log prediction");
    save.addEventListener("click", async () => {
      await post(`/api/briefs/${res.id}/save`);
      save.replaceWith(h("span", { class: "faint" }, "Saved. Score it on Today after the event."));
    });
    card.append(h("div", { class: "prediction" }, h("p", { class: "section-label" }, "Prediction"),
      cited(res.prediction.text, res.prediction.cites, "p"), h("div", { class: "row" }, save)));
  }
  const notes = [];
  if (res.assessments_used?.length) notes.push(`Drew on private assessments: ${res.assessments_used.join(", ")}.`);
  if (res.dropped_uncited) notes.push(`${res.dropped_uncited} uncited point${res.dropped_uncited > 1 ? "s" : ""} left out.`);
  if (res.summarised_notes) notes.push(`${res.summarised_notes} older notes were summarised to fit.`);
  if (res.words) notes.push(`${res.words} words.`);
  if (notes.length) card.append(h("p", { class: "faint" }, notes.join(" ")));
  const wrap = h("div", { class: "stack" }, card);
  if (res.mirror) wrap.append(mirrorCard(res.mirror));
  return wrap;
}
