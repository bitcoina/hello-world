import { del, get, patch, post } from "../api.js";
import { field, fmtDay, h, initial, miniDial, relDay, s, sheet, toast, todayIso, trendWord } from "../ui.js";
import { go, isOwner } from "../main.js";
import { cited, section } from "./common.js";

const GROUPS = [["family", "Family"], ["friends", "Friends"], ["work-adjacent", "Work-adjacent"]];
const LAYERS = [[5, "Inner 5"], [15, "15"], [50, "50"], [150, "150"]];
const DATE_KINDS = ["birthday", "anniversary", "bereavement", "hospital", "exam", "interview", "house move", "new job", "result", "other"];
const GESTURE_KINDS = ["gift", "text", "photo", "article", "coffee", "call", "visit", "other"];

export async function renderPeople(root) {
  const people = await get("/api/people");
  const search = h("input", { type: "search", placeholder: "Search", "aria-label": "Search people" });
  const list = h("ul", { class: "list" });
  function draw() {
    const q = search.value.trim().toLowerCase();
    list.replaceChildren();
    const shown = people.filter((p) => !q || p.name.toLowerCase().includes(q) || p.aliases.some((a) => a.toLowerCase().includes(q)));
    for (const p of shown) {
      list.append(h("li", {}, h("a", { class: "person-row", href: `#/people/${p.id}` },
        initial(p.name, p.layer),
        h("span", { class: "grow" },
          h("span", { class: "name" }, p.name),
          h("span", { class: "faint" }, describe(p))))));
    }
    if (!shown.length) list.append(h("li", { class: "empty" }, people.length ? "No one matches." : "No one yet. People appear as you write notes, or add them here."));
  }
  search.addEventListener("input", draw);
  root.append(
    h("div", { class: "page-head" }, h("h1", {}, "People"),
      h("button", { class: "btn small", type: "button", onclick: addPerson }, "Add")),
    h("div", { class: "stack" }, search, h("div", { class: "card flat" }, list)));
  draw();
}

function describe(p) {
  const bits = [p.group, p.last_contact ? `in touch ${relDay(p.last_contact)}` : null].filter(Boolean);
  return bits.length ? ` · ${bits.join(" · ")}` : "";
}

async function addPerson() {
  const id = await sheet((close) => {
    const name = h("input", { type: "text", required: true, autocomplete: "off" });
    const grp = h("select", {}, h("option", { value: "" }, "—"), GROUPS.map(([v, l]) => h("option", { value: v }, l)));
    const form = h("form", { class: "stack" }, h("h2", {}, "Add a person"), field("Name", name), field("Group", grp),
      h("div", { class: "row spread" },
        h("button", { class: "btn ghost", type: "button", onclick: () => close(null) }, "Cancel"),
        h("button", { class: "btn primary", type: "submit" }, "Add")));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const r = await post("/api/people", { display_name: name.value, group: grp.value || null });
        close(r.id);
      } catch (ex) { toast(ex.message); }
    });
    return form;
  });
  if (id) go(`#/people/${id}`);
}

export async function renderPerson(root, idText) {
  const id = Number(idText);
  const p = await get(`/api/people/${id}`);
  const reload = () => go(`#/people/${id}`);

  const layerRow = h("div", { class: "layers", role: "group", "aria-label": "Closeness" });
  for (const [layer, label] of LAYERS) {
    const b = h("button", { class: ["btn small", p.layer === layer ? "primary" : ""], type: "button" }, label);
    b.addEventListener("click", async () => {
      await patch(`/api/people/${id}`, { layer: p.layer === layer ? 0 : layer });
      reload();
    });
    layerRow.append(b);
  }

  root.append(h("div", { class: "stack-lg" },
    h("div", { class: "person-head" },
      initial(p.name, p.layer, "portrait"),
      h("div", { class: "grow" },
        h("h1", {}, p.name),
        h("p", { class: "muted" }, [p.group, p.aliases.length ? `also “${p.aliases.join("”, “")}”` : null].filter(Boolean).join(" · ") || " "),
        h("p", { class: "faint" }, p.last_contact ? `Last in touch ${relDay(p.last_contact)}` : "No contact logged yet"))),
    h("div", { class: "row spread" }, layerRow,
      h("button", { class: "btn small", type: "button", onclick: async () => { await post(`/api/people/${id}/contact`, { initiated_by: "me" }); toast("Logged."); reload(); } }, "In touch today")),
    p.follow_ups.length ? section("Ask about", h("ul", { class: "list card flat" }, p.follow_ups.map((f) => h("li", {}, f.prompt, h("span", { class: "faint" }, ` · ${relDay(f.due_date)}`))))) : null,
    datesSection(p, reload),
    giftsSection(p, reload),
    p.family.length ? section("Family", h("ul", { class: "list card flat" }, p.family.map((f) =>
      h("li", {}, f.person_id ? h("a", { href: `#/people/${f.person_id}` }, f.text) : f.text,
        f.status === "provisional" ? h("span", { class: "tag provisional" }, " unconfirmed") : null)))) : null,
    p.events.length ? section("Events", h("ul", { class: "list card flat" }, p.events.map((e) => h("li", {}, e.title, h("span", { class: "faint" }, e.date ? ` · ${fmtDay(e.date)}` : ""))))) : null,
    p.deep ? deepSections(p, id) : (isOwner() ? h("p", { class: "faint" }, "Open Private to see notes, trust and the relationship weather.") : null),
  ));
}

function datesSection(p, reload) {
  const list = h("ul", { class: "list" });
  for (const d of p.key_dates) {
    list.append(h("li", { class: "row spread" },
      h("span", {}, h("strong", {}, d.label), " ", h("span", { class: "faint" }, `${d.recurring ? fmtDay(d.date, false) + " · yearly" : fmtDay(d.date)}`),
        d.status === "provisional" ? h("span", { class: "tag provisional" }, " unconfirmed") : null),
      h("button", { class: "btn small ghost", type: "button", "aria-label": `Remove ${d.label}`, onclick: async () => { await del(`/api/key_dates/${d.id}`); reload(); } }, "Remove")));
  }
  if (!p.key_dates.length) list.append(h("li", { class: "empty" }, "No dates yet."));
  return section("Key dates", h("div", { class: "card flat" }, list,
    h("button", { class: "linkish", type: "button", onclick: () => addDate(p.id, p.name).then((ok) => ok && reload()) }, "Add a date")));
}

function giftsSection(p, reload) {
  const list = h("ul", { class: "list" });
  for (const g of p.gestures) {
    list.append(h("li", {}, h("span", { class: "faint" }, `${fmtDay(g.date)} · ${g.kind} · `), g.what,
      g.landed ? h("span", { class: "muted" }, ` — ${g.landed}`) : null));
  }
  if (!p.gestures.length) list.append(h("li", { class: "empty" }, "Nothing logged yet."));
  return section("Gifts and gestures", h("div", { class: "card flat" }, list,
    h("button", { class: "linkish", type: "button", onclick: () => addGesture(p.id).then((ok) => ok && reload()) }, "Log a gift or gesture")));
}

function addDate(id, name) {
  return sheet((close) => {
    const kind = h("select", {}, DATE_KINDS.map((k) => h("option", { value: k }, k)));
    const label = h("input", { type: "text", value: "Birthday", required: true });
    const date = h("input", { type: "date", required: true });
    const recurring = h("input", { type: "checkbox", checked: true });
    kind.addEventListener("change", () => {
      label.value = kind.value.charAt(0).toUpperCase() + kind.value.slice(1);
      recurring.checked = ["birthday", "anniversary", "bereavement"].includes(kind.value);
    });
    const form = h("form", { class: "stack" }, h("h2", {}, `A date for ${name}`),
      field("Kind", kind), field("Label", label), field("Date", date),
      h("label", { class: "toggle" }, recurring, "Every year"),
      h("div", { class: "row spread" },
        h("button", { class: "btn ghost", type: "button", onclick: () => close(false) }, "Cancel"),
        h("button", { class: "btn primary", type: "submit" }, "Save")));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await post(`/api/people/${id}/key_dates`, { kind: kind.value, label: label.value, date: date.value, recurring: recurring.checked });
        close(true);
      } catch (ex) { toast(ex.message); }
    });
    return form;
  });
}

function addGesture(id) {
  return sheet((close) => {
    const kind = h("select", {}, GESTURE_KINDS.map((k) => h("option", { value: k }, k)));
    const what = h("input", { type: "text", required: true, placeholder: "Flowers, a photo of the old house…" });
    const date = h("input", { type: "date", value: todayIso() });
    const landed = h("input", { type: "text", placeholder: "How it landed (optional)" });
    const form = h("form", { class: "stack" }, h("h2", {}, "Gift or gesture"),
      field("Kind", kind), field("What", what), field("When", date), field("How it landed", landed),
      h("div", { class: "row spread" },
        h("button", { class: "btn ghost", type: "button", onclick: () => close(false) }, "Cancel"),
        h("button", { class: "btn primary", type: "submit" }, "Save")));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await post(`/api/people/${id}/gestures`, { kind: kind.value, what: what.value, date: date.value || null, landed: landed.value || null });
        close(true);
      } catch (ex) { toast(ex.message); }
    });
    return form;
  });
}

// ---------------------------------------------------------------- Private
function deepSections(p, id) {
  const d = p.deep;
  const wrap = h("div", { class: "stack-lg" });
  if (d.weather.length) {
    wrap.append(section("Relationship weather", h("div", { class: "card flat" }, d.weather.map((w) =>
      h("div", { class: "weather" }, miniDial(w.needle),
        h("div", { class: "grow" },
          h("a", { href: w.person_id ? `#/people/${w.person_id}` : "#/people" }, w.person),
          h("div", { class: "faint" }, `${w.weather} · `, h("span", { class: `trend-${w.trend}` }, trendWord(w.trend)),
            w.status === "provisional" ? " · provisional" : "", w.topics.length ? ` · over ${w.topics.join(", ")}` : "")),
        cited("", w.notes.slice(-3)))))));
  }
  const radarHas = Object.values(d.trust_radar).some((v) => v.mine !== null || v.theirs !== null);
  wrap.append(h("div", { class: "row" },
    h("div", { class: "card flat grow" },
      h("p", { class: "section-label" }, "As a source"),
      h("div", { class: "row" }, h("span", { class: "grade" }, d.source_grade.grade),
        h("span", {}, d.source_grade.text, h("div", { class: "faint" }, `${d.source_grade.confirmed} confirmed · ${d.source_grade.contradicted} contradicted`)))),
    d.relativism_tolerance ? h("div", { class: "card flat grow" },
      h("p", { class: "section-label" }, "Hypotheticals about ethics"),
      h("p", {}, { low: "Hears them as statements of intent", normal: "Takes them in stride", high: "Enjoys them" }[d.relativism_tolerance])) : null));
  if (radarHas) wrap.append(section("Trust by domain", h("div", { class: "card flat" }, radar(d.trust_radar),
    h("p", { class: "faint" }, "Solid: your trust in them. Dashed: theirs in you."))));
  if (d.motives.length || d.frames.length) {
    wrap.append(section("Wants, fears and frames", h("ul", { class: "list card flat" },
      d.motives.map((m) => h("li", {}, cited(`${m.kind === "want" ? "Wants" : "Fears"} ${m.category === "other" ? "" : m.category + ": "}${m.detail}`, m.source_note_ids.slice(-3)),
        m.status === "provisional" ? h("span", { class: "tag provisional" }, " provisional") : null)),
      d.frames.map((f) => h("li", {}, cited(`On ${f.label}: ${f.frame || "frame unknown"} · cares ${f.salience}/3`, f.source_note_ids.slice(-3)))))));
  }
  if (d.claims.length) {
    wrap.append(section("What they've told you", h("ul", { class: "list card flat" }, d.claims.map((c) =>
      h("li", {}, cited(c.text, [c.note_id]), h("span", { class: "faint" }, ` · ${c.status === "open" ? c.credibility_text : c.status}`))))));
  }
  if (d.notes.length) {
    wrap.append(section("Recent notes", h("ul", { class: "list card flat" }, d.notes.map((n) =>
      h("li", {}, h("span", { class: "faint" }, `${n.id} · ${fmtDay(n.created_at)}${n.input_type === "voice" ? " · voice" : ""}`),
        h("p", {}, n.raw_text))))));
  }
  const assessBtn = h("button", { class: "btn small", type: "button" }, `Private reading${d.assessment_count ? ` (${d.assessment_count})` : ""}`);
  const assessBox = h("div");
  assessBtn.addEventListener("click", async () => {
    if (assessBox.firstChild) { assessBox.replaceChildren(); return; }
    assessBox.append(await assessments(id));
  });
  wrap.append(section("Assessments", h("p", { class: "faint" }, "Behavioural patterns, kept apart from the record. Never shown in readbacks or ordinary briefs."), h("div", { class: "row" }, assessBtn), assessBox));
  wrap.append(h("div", { class: "row" }, h("button", { class: "btn danger small", type: "button", onclick: () => forget(p) }, `Forget ${p.name}`)));
  return wrap;
}

async function assessments(id) {
  const rows = await get(`/api/people/${id}/assessments`);
  if (!rows.length) return h("p", { class: "empty" }, "No patterns yet.");
  return h("ul", { class: "list card flat" }, rows.map((a) => {
    const take = h("textarea", { rows: 2, placeholder: "Your take (only you write this)", value: a.your_take });
    take.addEventListener("change", () => patch(`/api/assessments/${a.id}`, { your_take: take.value }).then(() => toast("Saved.")));
    return h("li", { class: "stack" },
      h("p", {}, h("strong", {}, a.pattern)),
      h("p", { class: "faint" }, `${a.status} · confidence ${Math.round(a.confidence * 100)}% · since ${a.first_seen}`),
      cited("For:", a.supporting, "div"),
      a.counter.length ? cited("Against:", a.counter, "div") : null,
      take,
      h("div", { class: "row" }, h("button", { class: "btn small ghost", type: "button", onclick: async () => {
        await patch(`/api/assessments/${a.id}`, { retired: a.status !== "retired" });
        toast(a.status === "retired" ? "Restored." : "Retired.");
      } }, a.status === "retired" ? "Restore" : "Retire")));
  }));
}

async function forget(p) {
  const ok = await sheet((close) => {
    const confirm = h("input", { type: "text", autocomplete: "off", placeholder: p.name });
    const go = h("button", { class: "btn danger", type: "submit", disabled: true }, "Forget for good");
    confirm.addEventListener("input", () => { go.disabled = confirm.value.trim() !== p.name; });
    const form = h("form", { class: "stack" }, h("h2", {}, `Forget ${p.name}?`),
      h("p", {}, "This deletes them, every relationship, date and gift, and redacts their name in every note. It can't be undone. Backups made before today still hold them until they rotate out."),
      field(`Type ${p.name} to confirm`, confirm),
      h("div", { class: "row spread" }, h("button", { class: "btn ghost", type: "button", onclick: () => close(false) }, "Cancel"), go));
    form.addEventListener("submit", (e) => { e.preventDefault(); close(true); });
    return form;
  });
  if (!ok) return;
  await del(`/api/people/${p.id}`);
  toast(`${p.name} has been forgotten.`);
  location.hash = "#/people";
}

function radar(data) {
  const domains = Object.keys(data);
  const cx = 150, cy = 120, r = 82;
  const pt = (i, level) => {
    const a = (Math.PI * 2 * i) / domains.length - Math.PI / 2;
    const rr = (r * ((level ?? 0) + 2)) / 4;
    return [cx + rr * Math.cos(a), cy + rr * Math.sin(a)];
  };
  const svg = s("svg", { viewBox: "0 0 300 245", class: "radar", role: "img", "aria-label": "Trust by domain" });
  for (const lvl of [-1, 0, 1, 2]) {
    svg.append(s("polygon", { class: "grid", points: domains.map((_, i) => pt(i, lvl).join(",")).join(" ") }));
  }
  domains.forEach((d, i) => {
    const [x, y] = pt(i, 2);
    svg.append(s("line", { class: "axis", x1: cx, y1: cy, x2: x, y2: y }));
    const [lx, ly] = pt(i, 2.55);
    svg.append(s("text", { x: lx, y: ly, "text-anchor": "middle", "dominant-baseline": "middle" }, d));
  });
  const poly = (key, cls) => {
    if (!domains.some((d) => data[d][key] !== null)) return;
    svg.append(s("polygon", { class: cls, points: domains.map((d, i) => pt(i, data[d][key] ?? 0).join(",")).join(" ") }));
  };
  poly("mine", "mine");
  poly("theirs", "theirs");
  return svg;
}

