import { get, post } from "../api.js";
import { h, toast } from "../ui.js";

const noteCache = new Map();

async function noteText(id) {
  if (!noteCache.has(id)) noteCache.set(id, get(`/api/notes/${encodeURIComponent(id)}`));
  return noteCache.get(id);
}

/** Text followed by clickable note citations; a tap reveals the note inline. */
export function cited(text, cites = [], tag = "span") {
  const holder = h(tag, {}, text);
  const bodies = h("div");
  for (const id of cites) {
    const btn = h("button", { type: "button", class: "cite", "aria-expanded": "false", title: `Show note ${id}` }, id);
    let body = null;
    btn.addEventListener("click", async () => {
      if (body) {
        body.remove();
        body = null;
        btn.setAttribute("aria-expanded", "false");
        return;
      }
      btn.setAttribute("aria-expanded", "true");
      body = h("div", { class: "cite-body" }, "…");
      bodies.append(body);
      try {
        const n = await noteText(id);
        body.textContent = `${n.created_at.slice(0, 10)} · ${n.text}`;
      } catch (e) {
        body.textContent = e.message;
      }
    });
    holder.append(" ", btn);
  }
  holder.append(bodies);
  return holder;
}

export function section(title, ...children) {
  return h("section", {}, h("p", { class: "section-label" }, title), ...children);
}

export function weatherWord(w) {
  return w || "Settled";
}

export function clearNoteCache() {
  noteCache.clear();
}

export function mirrorCard(m, { compact = false } = {}) {
  // Compact: the line is already at the end of a readback, so only offer the rating.
  const card = h("div", { class: compact ? "question mirror" : "card mirror" }, h("p", { class: "who" }, compact ? "Was the Mirror useful?" : "Mirror"),
    compact ? null : h("p", {}, m.text));
  const row = h("div", { class: "row" },
    h("button", { class: "btn small", type: "button", onclick: () => rate(true) }, "Useful"),
    h("button", { class: "btn small", type: "button", onclick: () => rate(false) }, "Not useful"),
    h("button", { class: "btn small ghost", type: "button", onclick: dismiss }, "Dismiss"));
  async function rate(useful) {
    await post(`/api/mirror/${m.id}/rate`, { useful });
    row.replaceWith(h("p", { class: "faint" }, "Thanks. That tunes how often the Mirror speaks."));
  }
  async function dismiss() {
    const r = await post(`/api/mirror/${m.id}/dismiss`);
    card.remove();
    if (r.muted) toast("Muted for 30 days.");
  }
  card.append(row);
  return card;
}
