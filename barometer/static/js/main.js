import { ApiError, bus, get, post } from "./api.js";
import { clear, field, h, onLongPress, sheet, toast } from "./ui.js";
import { renderToday } from "./views/today.js";
import { renderCapture } from "./views/capture.js";
import { renderPeople, renderPerson } from "./views/people.js";
import { renderBrief } from "./views/brief.js";
import { renderMap } from "./views/map.js";
import { renderReflect } from "./views/reflect.js";

export const state = { session: null };

const view = document.getElementById("view");
const tabbar = document.getElementById("tabbar");
const lockBtn = document.getElementById("lock");
const logo = document.getElementById("logo");
let cleanup = null;
let lastActivity = Date.now();

export const isOwner = () => state.session?.role === "owner";
export const isDeep = () => Boolean(state.session?.deep);

function setTier() {
  const deep = isDeep();
  document.documentElement.dataset.tier = deep ? "deep" : "almanac";
  document.querySelector('meta[name="theme-color"]').setAttribute("content", deep ? "#161b1f" : "#f4eee2");
  lockBtn.hidden = !isOwner();
  lockBtn.setAttribute("aria-pressed", String(deep));
  lockBtn.querySelector(".lock-label").textContent = deep ? "Private open" : "Private";
  lockBtn.setAttribute("aria-label", deep ? "Close Private" : "Open Private");
  for (const a of tabbar.querySelectorAll("a")) {
    a.hidden = (a.hasAttribute("data-owner") && !isOwner()) || (a.hasAttribute("data-deep") && !deep);
  }
  // The logo needle points to fair in the Almanac and swings towards change in Private.
  document.getElementById("logo-needle").style.transform = deep ? "rotate(-95deg)" : "rotate(0deg)";
}

export async function refreshSession() {
  state.session = await get("/api/session");
  setTier();
  return state.session;
}

const routes = [
  [/^#\/today$/, renderToday],
  [/^#\/capture$/, renderCapture],
  [/^#\/people$/, renderPeople],
  [/^#\/people\/(\d+)$/, renderPerson],
  [/^#\/brief$/, renderBrief],
  [/^#\/map$/, renderMap],
  [/^#\/reflect$/, renderReflect],
];

export function go(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

let routeSeq = 0;

async function route() {
  const seq = ++routeSeq;
  if (cleanup) {
    try { cleanup(); } catch { /* view already gone */ }
    cleanup = null;
  }
  clear(view);
  view.classList.toggle("wide", location.hash === "#/map");
  if (!state.session?.authenticated) {
    tabbar.hidden = true;
    renderLogin();
    return;
  }
  tabbar.hidden = false;
  let hash = location.hash || (isOwner() ? "#/capture" : "#/today");
  const deepOnly = ["#/brief", "#/map", "#/reflect"];
  if (deepOnly.includes(hash) && !isDeep()) hash = "#/today";
  if (hash === "#/capture" && !isOwner()) hash = "#/today";
  if (hash !== location.hash) history.replaceState(null, "", hash);
  for (const [re, render] of routes) {
    const m = hash.match(re);
    if (m) {
      const tab = hash.split("/")[1];
      for (const a of tabbar.querySelectorAll("a")) {
        if (a.dataset.tab === tab) a.setAttribute("aria-current", "page");
        else a.removeAttribute("aria-current");
      }
      // Each render gets its own container. If another navigation starts while this one
      // is still loading, this container has already been detached and is thrown away.
      const container = h("div");
      view.replaceChildren(container);
      try {
        const done = (await render(container, ...m.slice(1))) || null;
        if (seq !== routeSeq) {
          if (done) done();
          return;
        }
        cleanup = done;
      } catch (err) {
        if (seq === routeSeq && !(err instanceof ApiError && (err.status === 401 || err.status === 423))) {
          container.append(h("p", { class: "empty" }, err.message || "Something went wrong."));
        }
      }
      if (seq === routeSeq) view.focus({ preventScroll: true });
      return;
    }
  }
  go(isOwner() ? "#/capture" : "#/today");
}

function renderLogin() {
  const user = h("input", { type: "text", autocomplete: "username", required: true, autocapitalize: "none" });
  const pass = h("input", { type: "password", autocomplete: "current-password", required: true });
  const err = h("p", { class: "faint", role: "alert" });
  const form = h("form", { class: "stack" },
    field("Name", user), field("Passphrase", pass), err,
    h("button", { class: "btn primary", type: "submit" }, "Sign in"));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.textContent = "";
    try {
      await post("/api/login", { username: user.value, password: pass.value });
      pass.value = "";
      await refreshSession();
      go(isOwner() ? "#/capture" : "#/today");
    } catch (ex) {
      err.textContent = ex.message;
    }
  });
  const dial = logo.querySelector("svg").cloneNode(true);
  view.append(h("div", { class: "login" }, dial, h("h1", {}, "Barometer"),
    h("p", { class: "muted" }, "Read the pressure before the weather changes."), form));
  user.focus();
}

export async function openPrivate() {
  if (!isOwner()) return false;
  if (!state.session.deep_initialised) {
    toast("Private isn't set up yet. Run `barometer init` on the Pi.");
    return false;
  }
  const ok = await sheet((close) => {
    const pass = h("input", { type: "password", autocomplete: "off", required: true });
    const err = h("p", { class: "faint", role: "alert" });
    const form = h("form", { class: "stack" },
      h("h2", {}, "Open Private"),
      h("p", { class: "muted" }, "Notes, the political record and your private reading. It stays open until you close it or leave it idle for ten minutes."),
      field("Private passphrase", pass), err,
      h("div", { class: "row spread" },
        h("button", { type: "button", class: "btn ghost", onclick: () => close(false) }, "Cancel"),
        h("button", { type: "submit", class: "btn primary" }, "Open")));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      err.textContent = "Opening…";
      try {
        const res = await post("/api/deep/unlock", { passphrase: pass.value });
        pass.value = "";
        if (res.sealed_waiting) toast(`Filing ${res.sealed_waiting} sealed note${res.sealed_waiting > 1 ? "s" : ""}…`);
        close(true);
      } catch (ex) {
        err.textContent = ex.message;
        pass.select();
      }
    });
    return form;
  });
  if (ok) {
    await refreshSession();
    route();
  }
  return ok;
}

async function closePrivate() {
  await post("/api/deep/lock");
  await refreshSession();
  go(isOwner() ? "#/capture" : "#/today");
}

async function panic() {
  // Double-tap the logo: close Private, wipe the screen, land on an empty note.
  clear(view);
  try { await post("/api/panic"); } catch { /* signed out is fine too */ }
  await refreshSession().catch(() => {});
  if (document.getElementById("sheet").open) document.getElementById("sheet").close();
  go(isOwner() ? "#/capture" : "#/today");
}

let lastTap = 0;
logo.addEventListener("click", () => {
  const now = Date.now();
  if (now - lastTap < 350) {
    lastTap = 0;
    panic();
  } else {
    lastTap = now;
  }
});
onLongPress(logo, 700, () => { if (state.session?.authenticated && !isDeep()) openPrivate(); });
lockBtn.addEventListener("click", () => (isDeep() ? closePrivate() : openPrivate()));

bus.addEventListener("signedout", async () => {
  state.session = { authenticated: false };
  setTier();
  route();
});
bus.addEventListener("locked", async () => {
  if (isDeep()) {
    await refreshSession();
    toast("Private closed.");
    route();
  }
});
bus.addEventListener("activity", () => { lastActivity = Date.now(); });
for (const ev of ["pointerdown", "keydown"]) document.addEventListener(ev, () => { lastActivity = Date.now(); }, { passive: true });

// Mirror the server's idle logout so nothing lingers on screen.
setInterval(async () => {
  const idle = (state.session?.idle_seconds || 600) * 1000;
  if (state.session?.authenticated && Date.now() - lastActivity > idle) {
    clear(view);
    await refreshSession().catch(() => {});
    route();
  }
}, 15000);

window.addEventListener("hashchange", route);
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});

refreshSession()
  .catch(() => ({ authenticated: false }))
  .finally(route);
