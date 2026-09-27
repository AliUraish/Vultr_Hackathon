// Boot, sign-in, router, top bar, live updates.
import { $, $$, api, bus, cfg, esc, toast, FAILURE_LABEL, countTo } from "./util.js";
import { live } from "./live.js";
import * as liveView from "./views/live3d.js";
import * as floorView from "./views/floor.js";
import * as incidentsView from "./views/incidents.js";
import * as incidentView from "./views/incident.js";
import * as suiteView from "./views/suite.js";
import * as logView from "./views/log.js";
import * as policyView from "./views/policy.js";
import * as platformView from "./views/platform.js";

const VIEWS = {
  live: { mod: liveView, nav: "live" },
  floor: { mod: floorView, nav: "floor" },
  incidents: { mod: incidentsView, nav: "incidents" },
  incident: { mod: incidentView, nav: "incidents" },
  regression: { mod: suiteView, nav: "regression" },
  log: { mod: logView, nav: "log" },
  policy: { mod: policyView, nav: "policy" },
  platform: { mod: platformView, nav: "platform" },
};
const ALIASES = { inbox: "incidents", failure: "incident" };
const mounted = new Set();
let current = null, lastPolicy = null;

async function route() {
  const [, raw = "live", arg] = location.hash.split("/");
  const name = VIEWS[ALIASES[raw] || raw] ? (ALIASES[raw] || raw) : "live";
  document.body.classList.toggle("immersive", name === "live");
  const v = VIEWS[name];
  if (current && current !== name) VIEWS[current].mod.hide && VIEWS[current].mod.hide();
  for (const k of Object.keys(VIEWS)) $(`#view-${k}`).hidden = k !== name;
  if (!mounted.has(name)) { v.mod.mount($(`#view-${name}`)); mounted.add(name); }
  for (const a of $$("#nav a")) a.classList.toggle("active", a.dataset.view === v.nav);
  const el = $(`#view-${name}`);
  if (current !== name) { el.classList.remove("view"); void el.offsetWidth; el.classList.add("view"); }
  current = name;
  try { await (name === "incident" ? v.mod.show(Number(arg)) : v.mod.show()); }
  catch (e) { toast(e.message, "bad"); }
}

async function refreshState() {
  let st;
  try { st = await api("/api/state"); } catch { return; }
  live.seed(st.frame);
  const chip = $("#policy-chip");
  chip.innerHTML = `policy v${st.policy.version}${st.policy.signed ? ' <span class="sig" title="signed ed25519">✓</span>' : ""}`;
  chip.title = (st.policy.rules.join("\n") || "no rules") + (st.policy.signed ? `\nsigned · key ${st.policy.key_id}` : "\nunsigned");
  if (st.site && !cfg.site) loadSite();
  if (lastPolicy !== null && lastPolicy !== st.policy.version) { chip.classList.remove("flash"); void chip.offsetWidth; chip.classList.add("flash"); }
  lastPolicy = st.policy.version;
  $("#sim-chip").classList.toggle("online", st.sim_online);
  $("#sim-text").textContent = st.sim_online ? `sim live · ${st.run_id}` : "sim offline";
  countTo($("#tick-num"), st.tick, { ms: 1800 });
  const badge = $("#inbox-badge");
  badge.hidden = !st.open_failures; badge.textContent = st.open_failures;
  $("#brand").classList.toggle("busy", st.open_failures > 0);
  bus.emit("state", st);
}

function wireLive() {
  bus.on("failure", (d) => {
    if (d.type) toast(`${FAILURE_LABEL[d.type] || d.type}: ${d.robot} at t${d.tick}. Recording the incident…`, "bad");
    if (current === "incidents") incidentsView.show();
    if (current === "incident") incidentView.reload();
  });
  for (const t of ["replay", "hypothesis", "investigation", "experiment"]) {
    bus.on(t, () => {
      if (current === "incident") incidentView.reload();
      if (current === "incidents") incidentsView.show();
      if (current === "regression") suiteView.refresh();
    });
  }
  bus.on("policy", (d) => toast(`Policy v${d.version} pushed to the fleet`, "ok"));
  bus.on("notice", (d) => toast(d.message || d.type, "warn"));
  bus.on("auth", showLogin);
  bus.on("site", () => { if (!cfg.site || !cfg.site.catalog) loadSite(); });
  bus.on("ledger", (b) => { const el = $("#brand"); el.classList.remove("sealed"); void el.offsetWidth; el.classList.add("sealed"); });
}

async function loadSite() {
  try { cfg.site = await api("/api/site"); } catch { return; }
  const f = cfg.site.facility;
  $("#site-chip").innerHTML = `${esc(f.company)} <span class="code">${esc(cfg.site.code)}</span>`;
  $("#site-chip").title = `${cfg.site.name}\n${f.description}\n${f.city} · ${Number(f.sq_ft).toLocaleString()} sq ft · profile by ${cfg.site.source}`;
  document.title = `Replay · ${cfg.site.code} ${cfg.site.name}`;
  bus.emit("site", cfg.site);
}

function showLogin() { $("#login").hidden = false; $("#login-user").focus(); }

let booted = false;
async function boot() {
  try { await api("/api/me"); } catch { return; }
  $("#login").hidden = true;
  if (booted) { route(); return; }
  booted = true;
  const m = await api("/api/map");
  cfg.map = m; cfg.tickHz = m.tick_hz;
  wireLive();
  live.connect();
  await refreshState();
  await loadSite();
  setInterval(refreshState, 2000);
  window.addEventListener("hashchange", route);
  window.addEventListener("keydown", (e) => {
    if (e.target.matches("input, select, textarea")) return;
    const v = VIEWS[current];
    if (v && v.mod.onKey && v.mod.onKey(e)) { e.preventDefault(); return; }
    const nav = { "1": "live", "2": "floor", "3": "incidents", "4": "regression", "5": "log", "6": "policy", "7": "platform" }[e.key];
    if (nav && !e.altKey && !e.metaKey && !e.ctrlKey) location.hash = `#/${nav}`;
  });
  route();
}

function measureTopbar() { document.documentElement.style.setProperty("--tb", `${document.querySelector(".topbar").offsetHeight}px`); }
window.addEventListener("resize", measureTopbar);
new ResizeObserver(measureTopbar).observe(document.querySelector(".topbar"));

$("#login-form").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/api/login", { body: { username: $("#login-user").value, password: $("#login-pass").value } });
    $("#login-error").textContent = "";
    boot();
  } catch (err) { $("#login-error").textContent = err.message; }
};
$("#logout").onclick = async () => { await api("/api/logout", { body: {} }).catch(() => {}); location.reload(); };

bus.on("auth", showLogin);
boot();
