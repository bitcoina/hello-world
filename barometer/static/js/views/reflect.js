import { post } from "../api.js";
import { field, h, s, spinner, todayIso } from "../ui.js";
import { cited, mirrorCard } from "./common.js";

export async function renderReflect(root) {
  const end = h("input", { type: "date", value: todayIso() });
  const start = h("input", { type: "date" });
  const d0 = new Date();
  d0.setDate(d0.getDate() - 30);
  start.value = `${d0.getFullYear()}-${String(d0.getMonth() + 1).padStart(2, "0")}-${String(d0.getDate()).padStart(2, "0")}`;
  const out = h("div");
  const runBtn = h("button", { class: "btn primary", type: "button" }, "Reflect");
  async function run() {
    runBtn.disabled = true;
    out.replaceChildren(h("div", { class: "card" }, spinner("Looking back…")));
    try {
      out.replaceChildren(reflectOutput(await post("/api/analyse", { mode: "reflect", start: start.value, end: end.value })));
    } catch (e) {
      out.replaceChildren(h("p", { class: "muted" }, e.message));
    } finally {
      runBtn.disabled = false;
    }
  }
  runBtn.addEventListener("click", run);
  root.append(h("div", { class: "page-head" }, h("h1", {}, "Reflect")),
    h("div", { class: "stack" }, h("div", { class: "row" }, field("From", start), field("To", end), runBtn), out));
  run();
}

export function reflectOutput(res) {
  const st = res.stats;
  const wrap = h("div", { class: "stack-lg" });
  wrap.append(h("div", { class: "row" },
    h("div", { class: "card flat grow" }, h("p", { class: "section-label" }, "Prediction accuracy"),
      h("p", { class: "stat" }, st.accuracy === null ? "—" : `${Math.round(st.accuracy * 100)}%`),
      h("p", { class: "faint" }, `${st.scored} scored in this period`)),
    h("div", { class: "card flat grow" }, h("p", { class: "section-label" }, "This period"),
      h("p", {}, [plural(st.counts.misfire, "misfire"), plural(st.counts.win, "win"), plural(st.counts.prediction, "prediction")].join(" · ")),
      st.mirror?.shown ? h("p", { class: "faint" }, `Mirror: ${st.mirror.shown} shown, ${st.mirror.useful} useful, ${st.mirror.not_useful} not`) : null)));

  if (st.series.length) wrap.append(h("section", {}, h("p", { class: "section-label" }, "Accuracy over time"), h("div", { class: "card flat" }, chart(st.series))));

  for (const sec of res.sections || []) {
    wrap.append(h("section", {}, h("p", { class: "section-label" }, sec.title),
      h("ul", { class: "list card flat" }, sec.items.map((i) => h("li", {}, cited(i.text, i.cites))))));
  }

  if (st.observations.length) {
    wrap.append(h("section", {}, h("p", { class: "section-label" }, "Misfires and wins"),
      h("ul", { class: "list card flat" }, st.observations.map((o) =>
        h("li", {}, h("span", { class: "tag" }, o.kind), " ",
          cited(o.description, o.note_id ? [o.note_id] : []))))));
  }

  if (st.ledger.length) {
    wrap.append(h("section", {}, h("p", { class: "section-label" }, "Who gets in touch first"),
      h("ul", { class: "list card flat" }, st.ledger.map((l) => {
        const total = l.me + l.them || 1;
        const me = h("span", { class: "me" });
        const them = h("span", { class: "them" });
        me.style.width = `${(100 * l.me) / total}%`;
        them.style.width = `${(100 * l.them) / total}%`;
        return h("li", { class: "stack" },
          h("div", { class: "row spread" }, h("a", { href: `#/people/${l.person_id}` }, l.person),
            h("span", { class: "faint num" }, `you ${l.me} · them ${l.them}`)),
          h("div", { class: "bar", role: "img", "aria-label": `You ${l.me}, them ${l.them}` }, me, them),
          l.risk ? h("p", { class: "faint" }, "They almost always reach out first. Worth a call that's your idea.") : null);
      }))));
  }

  if (st.bias_check.length) {
    wrap.append(h("section", {}, h("p", { class: "section-label" }, "Bias check"),
      h("p", { class: "faint" }, "Patterns with evidence against them. Growing counter-evidence means retire it, so a hunch doesn't harden into a grudge."),
      h("ul", { class: "list card flat" }, st.bias_check.map((b) => h("li", {},
        h("p", {}, h("strong", {}, b.person), `: ${b.pattern}`),
        cited(`${b.status} · for`, b.supporting, "div"), cited("against", b.counter, "div"))))));
  }

  if (res.mirror) wrap.append(mirrorCard(res.mirror));
  if (!st.series.length && !(res.sections || []).length && !st.observations.length && !st.ledger.length) {
    wrap.append(h("p", { class: "empty" }, "Nothing to look back on yet. Misfires, wins and predictions come from your notes and saved briefs."));
  }
  return wrap;
}

function plural(n, word) {
  n = n || 0;
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function chart(series) {
  const W = 320, H = 150, pad = 28;
  const n = series.length;
  const x = (i) => pad + (n === 1 ? (W - 2 * pad) / 2 : (i * (W - 2 * pad)) / (n - 1));
  const y = (v) => H - pad - v * (H - 2 * pad);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img", "aria-label": "Prediction accuracy by month" });
  for (const v of [0, 0.5, 1]) {
    svg.append(s("line", { class: "grid", x1: pad, x2: W - pad, y1: y(v), y2: y(v) }),
      s("text", { x: 4, y: y(v) + 4 }, `${v * 100}%`));
  }
  svg.append(s("polyline", { class: "line", points: series.map((p, i) => `${x(i)},${y(p.accuracy)}`).join(" ") }));
  series.forEach((p, i) => {
    svg.append(s("circle", { class: "dot", cx: x(i), cy: y(p.accuracy), r: 4 }, s("title", {}, `${p.month}: ${Math.round(p.accuracy * 100)}% of ${p.n}`)));
    if (n <= 8 || i % Math.ceil(n / 8) === 0) svg.append(s("text", { x: x(i), y: H - 8, "text-anchor": "middle" }, p.month.slice(2)));
  });
  return svg;
}
