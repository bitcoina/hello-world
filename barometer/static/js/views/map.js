import { get } from "../api.js";
import { h, miniDial, s, trendWord } from "../ui.js";
import { go } from "../main.js";

export async function renderMap(root) {
  const events = await get("/api/events");
  const filter = h("select", { "aria-label": "Filter by event" }, h("option", { value: "" }, "Everyone"),
    events.map((e) => h("option", { value: e.id }, `${e.title}${e.date ? " · " + e.date : ""}`)));
  const holder = h("div", { class: "map" });
  const side = h("aside", { class: "card flat stack" }, h("p", { class: "faint" }, "Tap a person to centre them."));
  root.append(
    h("div", { class: "page-head" }, h("h1", {}, "Map"), filter),
    h("div", { class: "map-wrap" }, holder, side),
    h("div", { class: "legend", "aria-hidden": "true" },
      h("span", {}, h("i", { class: "l-warm" }), "warming"),
      h("span", {}, h("i", { class: "l-cool" }), "cooling"),
      h("span", {}, h("i", { class: "l-stable" }), "steady"),
      h("span", {}, h("i", { class: "l-gap" }), "trust gap"),
      h("span", {}, h("i", { class: "l-tri" }), "low pressure (unbalanced trio)"),
      h("span", {}, "Thicker = stronger")));

  let cy = null;
  const css = getComputedStyle(document.documentElement);
  const color = (name) => css.getPropertyValue(name).trim();

  async function draw() {
    if (cy) cy.destroy();
    holder.replaceChildren();
    const data = await get(`/api/map${filter.value ? `?event_id=${filter.value}` : ""}`);
    if (!data.nodes.length) {
      holder.append(h("p", { class: "empty" }, "The map fills in as notes log what happens between people."));
      return;
    }
    if (!window.cytoscape) {
      holder.append(h("p", { class: "empty" }, "Map library didn't load."));
      return;
    }
    const container = h("div", { class: "cy" });
    const overlay = s("svg", { class: "triads" });
    holder.append(container, overlay);
    const trendColour = { warming: color("--warm"), cooling: color("--cool"), stable: color("--ink-3") };
    cy = window.cytoscape({
      container,
      elements: [
        ...data.nodes.map((n) => ({ data: { id: String(n.id), label: n.label, self: n.self ? 1 : 0, layer: n.layer || 0 } })),
        ...data.edges.map((e) => ({
          data: {
            id: e.id, source: String(e.source), target: String(e.target), kind: e.kind,
            width: e.width || 1.2, colour: e.kind === "relationship" ? trendColour[e.trend] : color("--ink-3"),
            neg: e.value < 0 ? 1 : 0, weather: e.weather, trend: e.trend, notes: e.notes,
          },
        })),
      ],
      style: [
        { selector: "node", style: {
          "background-color": color("--surface-2"), "border-width": 2, "border-color": color("--accent"),
          label: "data(label)", color: color("--ink"), "font-family": "Inter, system-ui, sans-serif", "font-size": 12,
          "text-valign": "bottom", "text-margin-y": 6, width: 30, height: 30,
          "text-outline-color": color("--bg"), "text-outline-width": 3,
        } },
        { selector: "node[self = 1]", style: { "background-color": color("--accent"), width: 34, height: 34 } },
        { selector: "node[layer = 5]", style: { "border-width": 5 } },
        { selector: "node[layer = 15]", style: { "border-width": 4 } },
        { selector: "edge", style: { width: "data(width)", "line-color": "data(colour)", "curve-style": "bezier", opacity: 0.85 } },
        { selector: "edge[neg = 1]", style: { "line-style": "solid", opacity: 0.95 } },
        { selector: 'edge[kind = "trust-gap"]', style: { "line-style": "dashed", "line-dash-pattern": [6, 4], width: 1.5, "line-color": color("--ink-2"), "curve-style": "unbundled-bezier", "control-point-distances": [18] } },
        { selector: 'edge[kind = "friendship"]', style: { width: 1, "line-color": color("--rule-strong"), opacity: 0.6 } },
        { selector: ".faded", style: { opacity: 0.18 } },
      ],
      layout: {
        name: "cose", animate: false, randomize: false, padding: 40,
        nodeRepulsion: () => 450000, idealEdgeLength: () => 120, nodeOverlap: 40, componentSpacing: 140, gravity: 0.4,
      },
      minZoom: 0.3, maxZoom: 2.5,
    });

    // Softly shade unbalanced triads by drawing triangles under the graph's coordinates.
    const shade = () => {
      overlay.replaceChildren();
      for (const t of data.triads) {
        const pts = t.map((id) => cy.getElementById(String(id))).filter((n) => n.nonempty()).map((n) => n.renderedPosition());
        if (pts.length === 3) overlay.append(s("polygon", { points: pts.map((p) => `${p.x},${p.y}`).join(" ") }));
      }
    };
    cy.on("render", shade);
    shade();

    cy.on("tap", "node", (evt) => {
      const node = evt.target;
      cy.elements().addClass("faded");
      node.closedNeighborhood().removeClass("faded");
      cy.animate({ center: { eles: node }, zoom: Math.max(cy.zoom(), 1.1) }, { duration: 400 });
      showPerson(node, data);
    });
    cy.on("tap", (evt) => {
      if (evt.target === cy) {
        cy.elements().removeClass("faded");
        side.replaceChildren(h("p", { class: "faint" }, "Tap a person to centre them."));
      }
    });
  }

  function showPerson(node, data) {
    const id = Number(node.id());
    const me = data.nodes.find((n) => n.id === id);
    const rows = data.edges.filter((e) => e.kind === "relationship" && (e.source === id || e.target === id))
      .sort((a, b) => Math.abs(b.value) - Math.abs(a.value));
    side.replaceChildren(
      h("h2", {}, me.label),
      me.grade ? h("p", { class: "faint" }, `Source grade ${me.grade}`) : null,
      rows.length ? h("div", {}, rows.map((e) => {
        const other = data.nodes.find((n) => n.id === (e.source === id ? e.target : e.source));
        return h("div", { class: "weather" }, miniDial(Math.tanh(e.value / 3)),
          h("div", { class: "grow" }, other ? other.label : "?",
            h("div", { class: "faint" }, `${e.weather} · `, h("span", { class: `trend-${e.trend}` }, trendWord(e.trend)))));
      })) : h("p", { class: "faint" }, "No derived relationships yet."),
      me.self ? null : h("button", { class: "btn small", type: "button", onclick: () => go(`#/people/${id}`) }, "Open"));
  }

  filter.addEventListener("change", draw);
  await draw();
  const onResize = () => cy && cy.resize();
  window.addEventListener("resize", onResize);
  return () => {
    window.removeEventListener("resize", onResize);
    if (cy) cy.destroy();
  };
}
