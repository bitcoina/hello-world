import { get, post } from "../api.js";
import { fmtDay, h, isoOf, parseDay, relDay, swipeAway, toast } from "../ui.js";
import { go, isDeep } from "../main.js";
import { section } from "./common.js";

export async function renderToday(root) {
  const data = await get("/api/today");
  const weekEnd = parseDay(data.week_start);
  weekEnd.setDate(weekEnd.getDate() + 6);
  root.append(h("div", { class: "page-head" },
    h("h1", {}, "This week"),
    h("span", { class: "faint" }, `${fmtDay(data.week_start, false)} – ${fmtDay(isoOf(weekEnd), false)}`)));

  const body = h("div", { class: "stack-lg" });
  root.append(body);
  let anything = false;

  for (const q of data.questions || []) {
    anything = true;
    body.append(questionCard(q, () => go("#/today")));
  }

  for (const p of data.open_predictions || []) {
    anything = true;
    body.append(predictionCard(p));
  }

  if (data.monthly_reflect && isDeep()) {
    anything = true;
    body.append(h("div", { class: "card flat" },
      h("h3", {}, "Monthly reading"),
      h("p", { class: "muted" }, "A new month: look back over last month's misfires, wins and predictions."),
      h("div", { class: "row" }, h("a", { class: "btn small", href: "#/reflect" }, "Reflect"))));
  }

  if (data.dates.length) {
    anything = true;
    body.append(section("Coming up", h("ul", { class: "list card flat" }, data.dates.map((d) =>
      h("li", { class: "date-row" },
        h("span", { class: "when" }, relDay(d.date)),
        h("span", { class: "grow" },
          h("a", { href: `#/people/${d.person_id}` }, d.person), `’s ${d.label}`,
          d.years ? h("span", { class: "faint" }, ` · ${d.years}`) : null,
          d.status === "provisional" ? h("span", { class: "tag provisional" }, " unconfirmed") : null))))));
  }

  if (data.follow_ups.length) {
    anything = true;
    const list = h("ul", { class: "list card flat" });
    for (const f of data.follow_ups) {
      const li = h("li", { class: "row spread" },
        h("span", { class: "grow" }, f.prompt, h("span", { class: "faint" }, ` · ${relDay(f.due)}`)),
        h("span", { class: "row" },
          h("button", { class: "btn small", type: "button", onclick: async () => { await post(`/api/followups/${f.id}/done`); li.remove(); toast("Nice."); } }, "Asked"),
          h("button", { class: "btn small ghost", type: "button", onclick: async () => { await post(`/api/followups/${f.id}/dismiss`); li.remove(); } }, "Skip")));
      list.append(li);
    }
    body.append(section("Ask how it went", list));
  }

  if (data.nudges.length) {
    anything = true;
    const wrap = h("div", { class: "stack" });
    for (const n of data.nudges) {
      const card = h("div", { class: "card nudge" },
        h("p", { class: "kind" }, n.kind === "dormant" ? "Reconnect" : "Just because"),
        h("p", {}, n.text),
        h("div", { class: "row" },
          h("button", { class: "btn small", type: "button", onclick: () => finish("done") }, "Done"),
          h("button", { class: "btn small ghost", type: "button", onclick: () => finish("dismiss") }, "Not now"),
          n.person_id ? h("a", { class: "btn small ghost", href: `#/people/${n.person_id}` }, "Open") : null));
      async function finish(action) {
        card.classList.add("leaving");
        await post(`/api/nudges/${n.id}/${action}`);
        setTimeout(() => card.remove(), 180);
      }
      swipeAway(card, () => { post(`/api/nudges/${n.id}/dismiss`); card.remove(); });
      wrap.append(card);
    }
    body.append(section("Worth a message", wrap, h("p", { class: "faint" }, "Swipe a card away when you're done.")));
  }

  if (!anything) {
    body.append(h("p", { class: "forecast" }, "Fair and settled. Nothing needs you this week."));
  }
}

export function questionCard(q, after) {
  const card = h("div", { class: "card question" },
    h("p", { class: "section-label" }, `One question · ${q.note_id}`),
    h("p", {}, q.prompt));
  const row = h("div", { class: "row" });
  for (const o of q.options) {
    row.append(h("button", { class: "btn small", type: "button", onclick: async () => {
      await post(`/api/notes/${q.note_id}/answer`, { value: o.value });
      toast("Thanks, updated.");
      after?.();
    } }, o.label));
  }
  card.append(row);
  return card;
}

function predictionCard(p) {
  const card = h("div", { class: "card" },
    h("p", { class: "section-label" }, "How did it go?"),
    h("p", { class: "forecast" }, `“${p.description}”`));
  const row = h("div", { class: "row" });
  for (const [score, label] of [[1, "Right"], [0.5, "Partly"], [0, "Wrong"]]) {
    row.append(h("button", { class: "btn small", type: "button", onclick: async () => {
      await post(`/api/predictions/${p.id}/score`, { score });
      card.remove();
      toast("Scored.");
    } }, label));
  }
  card.append(row);
  return card;
}
