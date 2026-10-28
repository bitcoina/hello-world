// Small DOM helpers. Everything is built with createElement/textContent, never innerHTML,
// so note text can't inject markup.

const SVG_NS = "http://www.w3.org/2000/svg";

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

function setAttrs(el, attrs) {
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith("on") && typeof value === "function") {
      el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "class") {
      el.setAttribute("class", Array.isArray(value) ? value.filter(Boolean).join(" ") : value);
    } else if (key === "dataset") {
      Object.assign(el.dataset, value);
    } else if (key === "value" && "value" in el) {
      el.value = value;
    } else if (value === true) {
      el.setAttribute(key, "");
    } else {
      el.setAttribute(key, String(value));
    }
  }
}

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  setAttrs(el, attrs);
  append(el, children);
  return el;
}

export function s(tag, attrs, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  setAttrs(el, attrs);
  append(el, children);
  return el;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

let toastTimer;
export function toast(message) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3200);
}

/** Open the shared <dialog>. `build(close)` returns its content; resolves with close(value). */
export function sheet(build) {
  const dlg = document.getElementById("sheet");
  return new Promise((resolve) => {
    const close = (value) => {
      dlg.close();
      clear(dlg);
      resolve(value);
    };
    clear(dlg);
    append(dlg, [build(close)]);
    dlg.onclose = () => resolve(undefined);
    dlg.onclick = (e) => { if (e.target === dlg) close(undefined); };
    dlg.showModal();
    const first = dlg.querySelector("input, textarea, select, button.primary");
    if (first) first.focus();
  });
}

export function initial(name, layer, cls = "") {
  const letter = (name || "?").trim().charAt(0).toUpperCase() || "?";
  return h("span", { class: ["initial", cls], "data-layer": layer || null, "aria-hidden": "true" }, letter);
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

export function parseDay(iso) {
  if (!iso) return null;
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return new Date(y, m - 1, d);
}

export function fmtDay(iso, withDay = true) {
  const d = parseDay(iso);
  if (!d) return "";
  return `${withDay ? DAYS[d.getDay()] + " " : ""}${d.getDate()} ${MONTHS[d.getMonth()]}`;
}

export function relDay(iso) {
  const d = parseDay(iso);
  if (!d) return "";
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const diff = Math.round((d - today) / 86400000);
  if (diff === 0) return "today";
  if (diff === 1) return "tomorrow";
  if (diff === -1) return "yesterday";
  if (diff > 1 && diff < 7) return `in ${diff} days`;
  if (diff < -1 && diff > -60) return `${-diff} days ago`;
  return fmtDay(iso);
}

export function isoOf(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

export function todayIso() {
  return isoOf(new Date());
}

/** A small barometer dial. needle runs from -1 (stormy) to +1 (fair). The needle eases in. */
export function miniDial(needle) {
  const angle = Math.max(-1, Math.min(1, needle || 0)) * 70;
  const needleEl = s("line", { x1: 27, y1: 30, x2: 27, y2: 9, class: "needle" });
  const svg = s("svg", { viewBox: "0 0 54 34", class: "mini-dial", "aria-hidden": "true" },
    s("path", { d: "M5 30 A22 22 0 0 1 49 30", class: "arc" }),
    s("path", { d: "M5 30 A22 22 0 0 1 12 14", class: "arc arc-storm" }),
    s("path", { d: "M42 14 A22 22 0 0 1 49 30", class: "arc arc-fair" }),
    needleEl,
    s("circle", { cx: 27, cy: 30, r: 3, class: "hub" }),
  );
  needleEl.style.transform = "rotate(0deg)";
  requestAnimationFrame(() => requestAnimationFrame(() => { needleEl.style.transform = `rotate(${angle}deg)`; }));
  return svg;
}

export function trendWord(trend) {
  return { warming: "warming", cooling: "cooling", stable: "steady" }[trend] || trend;
}

export function spinner(text) {
  return h("span", { class: "reading" }, h("span", { class: "spinner", "aria-hidden": "true" }), text);
}

export function field(label, input) {
  return h("label", { class: "field" }, h("span", {}, label), input);
}

export function seg(options, value, onChange) {
  const wrap = h("div", { class: "seg", role: "group" });
  for (const [val, label] of options) {
    const b = h("button", { type: "button", "aria-pressed": String(val === value) }, label);
    b.addEventListener("click", () => {
      for (const other of wrap.children) other.setAttribute("aria-pressed", "false");
      b.setAttribute("aria-pressed", "true");
      onChange(val);
    });
    wrap.append(b);
  }
  return wrap;
}

/** Long-press detector that also works with a mouse. */
export function onLongPress(el, ms, fn) {
  let timer = null;
  const start = () => {
    el.classList.add("pressing");
    timer = setTimeout(() => { timer = null; el.classList.remove("pressing"); fn(); }, ms);
  };
  const cancel = () => {
    el.classList.remove("pressing");
    if (timer) clearTimeout(timer);
    timer = null;
  };
  el.addEventListener("pointerdown", start);
  for (const ev of ["pointerup", "pointerleave", "pointercancel"]) el.addEventListener(ev, cancel);
  el.addEventListener("contextmenu", (e) => e.preventDefault());
}

export function swipeAway(el, onAway) {
  let x0 = null;
  let dx = 0;
  el.addEventListener("pointerdown", (e) => {
    if (e.target.closest("button, a")) return;
    x0 = e.clientX;
    dx = 0;
    el.setPointerCapture(e.pointerId);
  });
  el.addEventListener("pointermove", (e) => {
    if (x0 === null) return;
    dx = e.clientX - x0;
    if (Math.abs(dx) > 6) el.style.transform = `translateX(${dx}px)`;
  });
  const end = () => {
    if (x0 === null) return;
    x0 = null;
    if (Math.abs(dx) > 90) {
      el.classList.add("leaving");
      setTimeout(onAway, 180);
    } else {
      el.style.transform = "";
    }
  };
  el.addEventListener("pointerup", end);
  el.addEventListener("pointercancel", end);
}
