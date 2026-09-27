// The live pit: the real haul fleet from VM B in three.js. Tap a truck to follow it (it keeps hauling), read its
// lidar, load and live log, and ask the copilot about it or the whole pit. Every AI decision (dispatch, right of
// way, fault recovery) shows up in the pit and on the radio; a broken-down truck pages the crew.
import { $, $$, esc, api, act, toast, bus, cfg, item, money, pill, TIER, countTo, excavator, faceName, destination, zoneLabel, SCENARIO_LABEL } from "../util.js";
import { live } from "../live.js";
import { robotColor, stopDist } from "../floor.js";
import { Copilot } from "../copilot.js";

const CHAOS = [
  ["rockfall", "◆", "Rock falls off a highwall", "in front of a fast truck"],
  ["road_damage", "◌", "Pothole opens up", "ahead of a working truck"],
  ["grade_mixup", "⌗", "Grade mix-up at a dig face", "waste tagged as ore"],
  ["blast_closure", "⛔", "Close a bench for blasting", "a truck is routed through it"],
  ["tire_fault", "◍", "Tyre failure", "on a hauling truck"],
  ["sensor_fault", "◎", "Lidar fault", "on a hauling truck"],
  ["clear_roads", "✦", "Clear the roads", "rocks, closures, fresh potholes"],
];
const STATUS_LABEL = { moving: "hauling", waiting: "holding", held: "holding", blocked: "blocked", estop: "e-stop", queued: "queued",
  loading: "loading", dumping: "tipping", grade_check: "grade check", idle: "idle", fault: "broken down", standby: "standby" };
const STATUS_KIND = { moving: "ok", waiting: "warn", held: "warn", blocked: "bad", estop: "bad", queued: "warn", loading: "info", dumping: "info",
  grade_check: "info", idle: "", fault: "bad", standby: "" };
const CASE_STEPS = { tire: ["Alarm", "Pulled over", "Diagnosed", "Fitter", "Fixed"], sensor: ["Alarm", "Pulled over", "Diagnosed", "Workshop + spare", "Fixed"] };
const CASE_AT = { detected: 0, safing: 1, diagnosing: 2, recovering: 3, in_repair: 3, dispatched: 3, repairing: 3, resolved: 4 };
const CASE_LABEL = { detected: "alarm raised", safing: "AI pulling it over", diagnosing: "AI diagnosing", recovering: "AI driving it to the workshop",
  in_repair: "repair in the workshop", dispatched: "fitter on the way", repairing: "fitter repairing", resolved: "fixed", closed: "closed" };
const BY = { copilot: ["✦ AI", "ai"], rules: ["Rules", ""], technician: ["Fitter", "tech"], robot: ["Truck", ""], fleet: ["Fleet", ""], operator: ["Controller", "op"] };
const RULE = { head_on: "fallback rule: the higher truck number yields", head_on_retry: "fallback: the lower number yields too",
  parked: "blocked by a parked truck: re-route", queue: "waited too long behind a truck: re-route", make_way: "lets the loaded truck out of the bay", ai: "traffic AI ruling" };
const HEADING = { E: "east", W: "west", N: "north", S: "south" };
const kmhOf = (v) => (v * 10 * 3.6) / 1000;

let root = null, site = null, raf = 0, ready = null, sel = null, selFace = null, panelMode = null, copilot = null;
let A = null, jobs = {}, orders = {}, rules = [], cellZones = {}, accessToSlot = {}, homes = new Set(), faces = {};
const logs = new Map(), prev = new Map(), since = new Map(), radio = [];
const drivers = new Map(), reviews = new Map();
let crews = [];
const AGENT = { driver: "AI drivers", audit: "Auditor", dispatch: "Dispatch", right_of_way: "Traffic", roads: "Road crew", supervisor: "Supervisor",
  investigator: "Investigator", diagnosis: "Diagnosis", copilot: "Copilot", hazard_planner: "Hazard drills", autopilot: "Autopilot",
  pull_over: "Recovery", diagnose: "Recovery", recover: "Recovery", site_profile: "Site profile" };
let lastUi = 0, pollTimer = null, inset = 0, insetGoal = 0, serverOffset = 0;
const cases = new Map();

// ------------------------------------------------------------------ mount

export function mount(el) {
  root = el;
  root.innerHTML = `
    <div class="l3">
      <div class="l3-stage" id="l3-stage"><div class="l3-loading"><div class="spinner"></div>Surveying the pit…</div></div>
      <div class="l3-hud tl glass" id="l3-site">
        <div class="row"><span class="live-dot"></span><b id="l3-site-name">Live pit</b></div>
        <div class="hint" id="l3-site-sub">connecting to the fleet…</div>
        <div class="l3-kpis" id="l3-kpis"></div>
      </div>
      <div class="l3-alerts" id="l3-alerts"></div>
      <div class="l3-hud tr">
        <button class="glass" id="l3-overview" title="Back to the whole pit (Esc)">⤢ Overview</button>
        <div class="l3-menu">
          <button class="glass" id="l3-inject">⚡ Inject hazard</button>
          <div class="l3-drop glass" id="l3-drop" hidden>${CHAOS.map(([k, i, t, s]) => `<button data-s="${k}"><span>${i}</span><div><b>${t}</b><small>${s}</small></div></button>`).join("")}</div>
        </div>
      </div>
      <div class="l3-hud tl2 glass" id="l3-svc" hidden>
        <div class="row between"><h3>Truck service</h3><span class="hint">faults · AI recovery · repairs</span></div>
        <ol id="l3-svc-list"></ol>
      </div>
      <div class="l3-hud bl glass" id="l3-radio">
        <div class="row between"><h3>Pit radio · AI decisions</h3><span class="hint" id="l3-ai-src">dispatch · right of way · recovery</span></div>
        <ol id="l3-radio-list"><li class="hint">Listening…</li></ol>
      </div>
      <div class="l3-hud faces" id="l3-faces"></div>
      <div class="l3-hud bc" id="l3-fleet"></div>
      <button class="l3-hud br l3-ask glass" id="l3-ask-site"><span class="orb">✦</span> Ask about the pit</button>
      <aside class="l3-panel glass" id="l3-panel" aria-hidden="true">
        <div class="l3p-head" id="l3p-head"></div>
        <div class="l3p-body">
          <div id="l3p-robot">
            <div class="l3p-now" id="l3p-now"></div>
            <div class="l3p-case" id="l3p-case"></div>
            <div class="l3p-sensors" id="l3p-sensors"></div>
            <div class="l3p-health" id="l3p-health"></div>
            <div class="l3p-load" id="l3p-load"></div>
            <div class="tabs" id="l3p-tabs"><button data-t="log" class="on">Live log</button><button data-t="ask">✦ Ask</button></div>
            <ol class="l3p-log" id="l3p-log"></ol>
          </div>
          <div class="l3p-site" id="l3p-site"></div>
          <div class="l3p-face" id="l3p-face"></div>
          <div class="l3p-chat" id="l3p-chat"></div>
        </div>
      </aside>
    </div>`;
  copilot = new Copilot($("#l3p-chat", root));
  $("#l3-overview", root).onclick = () => closePanel();
  $("#l3-inject", root).onclick = (e) => { e.stopPropagation(); $("#l3-drop", root).hidden = !$("#l3-drop", root).hidden; };
  document.addEventListener("click", () => { const d = root && $("#l3-drop", root); if (d) d.hidden = true; });
  for (const b of $$("#l3-drop button", root)) {
    b.onclick = async () => {
      const r = await act(() => api("/api/chaos", { body: { scenario: b.dataset.s } }));
      if (r) toast(b.dataset.s === "clear_roads" ? "Roads cleared" : `${b.querySelector("b").textContent}: armed, fires when a truck is in position`, "warn");
    };
  }
  $("#l3-ask-site", root).onclick = () => openSite();
  for (const b of $$("#l3p-tabs button", root)) b.onclick = () => setTab(b.dataset.t);
  bus.on("state", (st) => {
    jobs = Object.fromEntries(st.jobs.map((j) => [j.id, j]));
    rules = st.policy.rules || [];
    if (st.ai) $("#l3-ai-src", root).textContent = st.ai.enabled ? `${st.ai.provider === "vultr" ? "Vultr" : st.ai.provider} · ${st.ai.model}` : "rules only (no model)";
  });
  bus.on("site", () => applySite());
  bus.on("service", (c) => { upsertCase(c); });
  bus.on("decision", (d) => onDecision(d));
  bus.on("usage", (u) => renderUsage(u));
  bus.on("driver", (p) => { drivers.set(p.robot, p); if (panelMode === "robot" && sel === p.robot) push(p.robot, "✦", `AI driver: ${p.kmh} km/h for ${p.hold_s} s · ${p.reason}`, "ai"); });
  bus.on("review", (r) => { reviews.set(r.id, r); renderRadio(); });
  bus.on("supervisor", (sv) => renderSupervisor(sv));
  bus.on("crews", (list) => { crews = list || []; if (site) site.setTechs(techs(), serverOffset); });
  bus.on("alert", (a) => toast(`⚠ ${a.message}`, "bad", 9000));
  indexMap();
}

function indexMap() {
  cellZones = {}; accessToSlot = {};
  for (const [z, cells] of Object.entries(cfg.map.zones)) for (const c of cells) (cellZones[c.join(",")] ||= []).push(z);
  for (const [slot, s] of Object.entries(cfg.map.slots)) accessToSlot[s.access.join(",")] = slot;
  homes = new Set((cfg.map.homes || []).map((h) => h.join(",")));
}

async function ensureScene() {
  if (site) return site;
  if (!ready) {
    ready = import("../three/scene.js").then(({ Site3D }) => {
      const stage = $("#l3-stage", root);
      stage.querySelector(".l3-loading").remove();
      const accents = Object.fromEntries([...cfg.map.robots, ...(cfg.map.spares || [])].map((r) => [r.id, robotColor(r.id)]));
      site = new Site3D(stage, cfg.map, { accents, onPick: (id) => (id ? openRobot(id) : null), onHover: () => {},
        onAsk: (id) => { if (sel !== id) openRobot(id); setTab("ask"); }, onFace: (slot) => openFace(slot) });
      applySite();
      return site;
    }).catch((e) => { toast(`3D view failed to load: ${e.message}`, "bad"); throw e; });
  }
  return ready;
}

export async function show() {
  await ensureScene();
  document.body.classList.toggle("panel-open", !!panelMode);
  clearInterval(pollTimer);
  poll();
  pollTimer = setInterval(poll, 3000);
  try { for (const d of await api("/api/decisions")) { if (d.review) reviews.set(d.id, d.review); onDecision(d, true); } } catch { /* live feed fills it */ }
  try {
    const ai = await api("/api/ai");
    for (const p of (ai.drivers && ai.drivers.latest) || []) drivers.set(p.robot, p);
    crews = ai.crews || [];
    if (ai.supervisor) renderSupervisor(ai.supervisor);
    if (ai.usage) renderUsage(ai.usage);
  } catch { /* the live feed fills it */ }
  cancelAnimationFrame(raf);
  const loop = (now) => {
    const s = live.advance(now);
    if (s && s.a) {
      A = s.a;
      for (const f of s.crossed) ingest(f, now);
      site.update(s.a, s.b, s.alpha, now, { label: (r) => STATUS_LABEL[r.st] || r.st });
      if (now - lastUi > 200) { lastUi = now; renderUi(); }
    } else site.update(null, null, 0, now);
    inset += (insetGoal - inset) * 0.12;
    if (Math.abs(insetGoal - inset) < 0.5) inset = insetGoal;
    applyInset();
    raf = requestAnimationFrame(loop);
  };
  raf = requestAnimationFrame(loop);
}

export function hide() { cancelAnimationFrame(raf); clearInterval(pollTimer); document.body.classList.remove("panel-open"); }

export function onKey(e) {
  if (e.key === "Escape" && panelMode) { closePanel(); return true; }
  return false;
}

async function poll() {
  try {
    const [o, k, sv, fc] = await Promise.all([api("/api/orders?limit=60"), api("/api/kpis"), api("/api/service"), api("/api/faces")]);
    orders = Object.fromEntries(o.map((x) => [x.id, x]));
    faces = fc || {};
    const seen = new Set(sv.map((c) => c.id));
    for (const id of [...cases.keys()]) if (!seen.has(id)) cases.delete(id);
    for (const c of sv) upsertCase(c, false);
    renderCases();
    renderAlerts();
    renderKpis(k);
    if (site && cfg.site) for (const c of cfg.site.carriers) site.setDockInfo(c.dock, c.carrier, k.docks?.[c.dock]?.units ?? 0);
  } catch { /* next poll */ }
}

function applySite() {
  if (!cfg.site) return;
  const f = cfg.site.facility;
  $("#l3-site-name", root).textContent = cfg.site.name;
  $("#l3-site-sub", root).textContent = `${f.company} · ${f.commodity} · ${f.location}`;
  if (site) for (const c of cfg.site.carriers) site.setDockInfo(c.dock, c.carrier, null);
}

function renderKpis(k) {
  const el = $("#l3-kpis", root);
  if (!el.children.length) {
    el.innerHTML = [["orders_per_hour", "loads / h"], ["units_shipped", "tonnes moved"], ["fleet_busy", "trucks hauling"], ["ai_decisions_hour", "AI decisions / h"], ["incidents_open", "incidents"]]
      .map(([key, label]) => `<div data-k="${key}"><b data-v="0">0</b><span>${label}</span></div>`).join("");
  }
  for (const d of el.children) countTo(d.querySelector("b"), Number(k[d.dataset.k] || 0), { ms: 700, fmt: (v) => Math.round(v).toLocaleString() });
  el.querySelector('[data-k="incidents_open"]').classList.toggle("alert", k.incidents_open > 0);
}

// ------------------------------------------------------------------ describing the fleet

const cellOf = (r) => [Math.floor(r.x / cfg.map.cell_mm), Math.floor(r.y / cfg.map.cell_mm)];
const cname = (c) => (c ? `c${c[0]}_${c[1]}` : "–");
const zoneOf = (c) => (cellZones[c.join(",")] || []).filter((z) => !["haul_roads", "ramps", "benches", "bench_upper", "bench_lower"].includes(z));
const matName = (sku) => { const it = item(sku); return it ? it.name : sku; };
const clock = () => new Date().toLocaleTimeString([], { hour12: false });

function jobInfo(r) {
  const j = r.job && jobs[r.job];
  const o = j && j.order_id && orders[j.order_id];
  return { j, o };
}

function goalText(r) {
  const { j, o } = jobInfo(r);
  const g = r.g && r.g.join(",");
  const slot = g && accessToSlot[g];
  if (r.op === "goto" && slot) return `Driving empty to ${faceName(slot)} to load ${matName(`MAT-${slot}`)}`;
  const dock = g && Object.entries(cfg.map.docks).find(([, d]) => d.join(",") === g)?.[0];
  if (r.op === "goto" && dock) return `Hauling ${r.c ? matName((r.cs || [])[0]) : "a load"} to ${destination(dock)}${o ? ` · ${o.customer}` : ""}`;
  if (r.op === "goto" && g && homes.has(g)) return "Driving back to the truck park";
  if (r.op === "goto") return j ? "Pulling aside to let a truck pass" : "Repositioning";
  return j ? `Working ${j.id}` : "Idle";
}

function caseOf(rid) { return [...cases.values()].filter((c) => c.robot_id === rid).sort((a, b) => b.id - a.id)[0] || null; }

function nowText(r) {
  const st = r.st, c = caseOf(r.id);
  if (r.sv === "remote") return `Under AI remote control: driving to ${cname(r.g)} at limp speed${r.f ? ` (${r.f.type === "tire" ? "tyre" : "lidar"} fault)` : ""}`;
  if (st === "fault") return c && c.status !== "resolved" ? `Broken down: ${c.component || c.fault || r.f?.type || "fault"} · ${CASE_LABEL[c.status] || c.status}` : "Broken down: hardware fault";
  if (st === "standby") return "Standby spare, parked in the workshop";
  if (r.hl) return `Easing through pothole ${r.hl} at ${kmhOf(r.v).toFixed(0)} km/h`;
  if (st === "moving") return goalText(r);
  if (st === "waiting") return r.w ? `Holding: ${r.w} has the road segment ahead` : "Holding for traffic";
  if (st === "queued") return `Queued behind ${r.w || "a truck"} for the excavator`;
  if (st === "held") return "Holding outside a road closed for blasting";
  if (st === "blocked") return "Stopped: a fallen rock blocks the route";
  if (st === "estop") return "Emergency stop";
  const slot = r.g && accessToSlot[r.g.join(",")];
  if (st === "loading") return `Being loaded by ${excavator(slot).id}: ${matName(`MAT-${slot}`)}`;
  if (st === "grade_check") return `Grade check at face ${slot} before loading`;
  if (st === "dumping") { const dock = r.g && Object.entries(cfg.map.docks).find(([, d]) => d.join(",") === r.g.join(","))?.[0]; return `Tipping at ${destination(dock)}`; }
  const { j } = jobInfo(r);
  return j ? `Waiting to start ${j.id}` : `Idle at ${cname(cellOf(r))}`;
}

function sensorsOf(r) {
  const c = cellOf(r), zones = zoneOf(c), cm = cfg.map.cell_mm, ph = cfg.map.physics || {};
  const dirs = { E: [1, 0], W: [-1, 0], S: [0, 1], N: [0, -1] }[r.d] || [1, 0];
  let near = null;
  const rockReach = (ph.robot_half || 5000) + (ph.rock_half || 3000);
  for (const p of (A && A.rocks) || []) {
    const px = p[0] * cm + cm / 2, py = p[1] * cm + cm / 2;
    const ahead = (px - r.x) * dirs[0] + (py - r.y) * dirs[1], side = Math.abs((px - r.x) * dirs[1] - (py - r.y) * dirs[0]);
    if (ahead > 0 && side < rockReach) { const gap = ahead - rockReach; if (!near || gap < near.gap) near = { gap, what: "fallen rock" }; }
  }
  for (const o of (A && A.robots) || []) {
    if (o.id === r.id) continue;
    const ahead = (o.x - r.x) * dirs[0] + (o.y - r.y) * dirs[1], side = Math.abs((o.x - r.x) * dirs[1] - (o.y - r.y) * dirs[0]);
    if (ahead > 0 && side < (ph.robot_half || 5000) * 2) { const gap = ahead - (ph.robot_half || 5000) * 2; if (!near || gap < near.gap) near = { gap, what: o.id }; }
  }
  const lidarM = ((r.h && r.h.range) || (ph.lidar_m || 60) * 1000) / 1000;
  const holes = ((A && A.holes) || []).filter((h) => h[3]).map((h) => ({ id: h[4], cell: [h[0], h[1]], depth: h[2],
    d: Math.hypot(h[0] * cm + cm / 2 - r.x, h[1] * cm + cm / 2 - r.y) / 1000,
    onRoute: (r.p || []).some((p) => p[0] === h[0] && p[1] === h[1]) })).sort((a, b) => a.d - b.d);
  const caps = rules.map((x) => x.match(/^speed_cap\((\w+),\s*([\d.]+)\)$/)).filter((m) => m && (cellZones[c.join(",")] || []).includes(m[1])).map((m) => Number(m[2]));
  const limit = Math.min(r.c ? ph.max_loaded_kmh || 32 : ph.max_kmh || 43, ...caps);
  const stop = (stopDist(r.v) + (ph.clearance_m || 3) * 1000) / 1000;
  return { c, zones, near, limit, stop, kmh: kmhOf(r.v), lidarM, holes };
}

// ------------------------------------------------------------------ events -> logs, radio, effects

function logFor(id) { if (!logs.has(id)) logs.set(id, []); return logs.get(id); }

function push(id, icon, text, kind = "", t = A ? A.t : 0) {
  const list = logFor(id);
  list.unshift({ icon, text, kind, time: clock(), t });
  if (list.length > 120) list.pop();
  if (panelMode === "robot" && sel === id) renderLog(true);
}

function toRadio(text, kind, robots, sub = "", id = null) {
  radio.unshift({ text, kind, robots, sub, time: clock(), id });
  if (radio.length > 2) radio.pop();
  renderRadio();
}

function describe(e, id) {
  switch (e.type) {
    case "pick": return ["▣", `Loaded ${matName(e.sku)} at face ${e.slot}`, "info"];
    case "scan": return e.ok === false ? ["⚠", `Grade check at face ${e.slot}: tag doesn't match`, "bad"] : ["⌁", `Grade check at face ${e.slot}: OK`, "info"];
    case "scan_mismatch": return ["⚠", `Grade mismatch at face ${e.slot}: load refused, grade control notified`, "bad"];
    case "dock_scan": return e.ok ? ["⇥", `Tipped at ${destination(e.dock)}: material check OK`, "ok"]
      : ["✕", `Wrong material at ${destination(e.dock)}: expected ${(e.expected || []).map(matName).join(", ")}, got ${(e.actual || []).map(matName).join(", ")}`, "bad"];
    case "job_done": return ["✓", `Load ${e.job} complete`, "ok"];
    case "job_exception": return ["⚑", `Load ${e.job} handed to a person (${String(e.reason).replace("_", " ")})`, "warn"];
    case "wait": return e.robot === id ? ["❚❚", `Hold: ${e.on} has ${cname(e.cell)}${e.other_moving ? " (it's moving)" : " (it's stopped)"}`, "warn"]
      : ["⟵", `${e.robot} is holding for me: I have ${cname(e.cell)}`, "dim"];
    case "queue": return ["⋯", `Queued behind ${e.behind} for the excavator`, "warn"];
    case "standoff": return ["⇄", `Head-on with ${e.robot === id ? e.with : e.robot}: both holding, the traffic AI decides`, "bad"];
    case "yield":
      return e.robot === id ? ["↺", `${e.rule === "ai" ? "✦ Traffic AI: " : ""}I yield to ${e.to} (${e.rule === "ai" ? e.reason || "AI ruling" : RULE[e.rule] || e.rule})`, e.rule === "ai" ? "ai" : "arbiter"]
        : ["→", `${e.rule === "ai" ? "✦ Traffic AI: " : ""}${e.robot} yields to me${e.reason ? ` (${e.reason})` : ""}`, e.rule === "ai" ? "ai" : "arbiter"];
    case "resume": return ["▶", `Moving again after ${(e.after / 10).toFixed(1)} s${e.from ? ` (${e.from} cleared)` : ""}`, "ok"];
    case "contact": return ["✸", `COLLISION${e.with ? ` with ${e.with.startsWith("K") ? "a fallen rock" : e.with}` : ""} at ${kmhOf(e.v || 0).toFixed(0)} km/h`, "bad"];
    case "struck": return ["✸", "Hit by a falling rock", "bad"];
    case "pothole_detected": return e.robot === id ? ["◌", `Lidar found pothole ${e.id} ${Math.round(e.dist / 1000)} m out (${(e.depth / 1000).toFixed(1)} m deep): fleet map updated`, "warn"] : null;
    case "pothole_enter": return ["◡", `Into pothole ${e.id} at ${kmhOf(e.v).toFixed(0)} km/h`, e.v > 250 ? "bad" : "warn"];
    case "pothole_exit": return ["◠", "Climbed out of the pothole", "dim"];
    case "pothole_strike": return ["✸", `Pothole strike at ${kmhOf(e.v).toFixed(0)} km/h: the lidar missed it`, "bad"];
    case "fault_alarm": return ["⚠", `ALARM ${e.code}: safety stop at ${cname(e.cell)}, crew paged`, "bad"];
    case "job_released": return ["↩", `Load ${e.job} handed back to the fleet${(e.held || []).length ? " (material still on board)" : ""}`, "warn"];
    case "service_move": return ["✦", `AI remote control: driving to ${cname(e.cell)}`, "ai"];
    case "service_arrived": return ["■", `Parked at ${cname(e.cell)} under remote control`, "ai"];
    case "deployed": return ["▲", "Deployed from the workshop into service", "ok"];
    case "standby": return ["◻", `Parked as standby at ${cname(e.cell)}`, ""];
    case "repaired": return ["✓", `Fixed${e.by ? `, marked done by ${e.by}` : ""}`, "ok"];
    case "zone_enter": return e.restricted ? ["⛔", `Entered ${zoneLabel(e.zone)} while CLOSED for blasting`, "bad"] : null;
    default: return null;
  }
}

function ingest(f, now) {
  for (const e of f.ev || []) {
    site.event(e, f, now);
    const involved = new Set([e.robot, e.to, e.with, e.on, e.behind].filter(Boolean));
    for (const id of involved) { const d = describe(e, id); if (d && (id === e.robot || ["yield", "standoff", "wait"].includes(e.type))) push(id, ...d, f.t); }
    if (e.type === "standoff") toRadio(`Head-on: ${e.robot} ⇄ ${e.with} at ${cname(e.cell)}`, "bad", [e.robot, e.with], "asking the traffic AI…");
    else if (e.type === "yield" && e.rule !== "ai" && e.rule !== "make_way") toRadio(`Fallback rule: ${e.robot} yields → ${e.to}`, "arbiter", [e.robot, e.to], RULE[e.rule] || e.rule);
    else if (e.type === "rock_fell") toRadio(`Rock down at ${cname(e.cell)}: trucks re-plan around it`, "bad", []);
    else if (e.type === "pothole_detected") toRadio(`${e.robot} lidar: pothole at ${cname(e.cell)}, ${(e.depth / 1000).toFixed(1)} m deep`, "warn", [e.robot], "fleet road map updated · trucks slow or swerve");
    else if (e.type === "pothole_formed") toRadio(`Road damage: a pothole opened at ${cname(e.cell)}`, "warn", []);
    else if (e.type === "zone_restricted") toRadio(`${zoneLabel(e.zone)} closed for blasting`, "bad", []);
    else if (e.type === "contact") toRadio(`Collision: ${e.robot}${e.with ? ` × ${e.with.startsWith("K") ? "rock" : e.with}` : ""}`, "bad", [e.robot]);
    else if (e.type === "fault_alarm") toRadio(`${e.robot} is not working (${e.code})`, "bad", [e.robot], "service case opened · crew paged");
    else if (e.type === "deployed") toRadio(`${e.robot} deployed from the workshop`, "ok", [e.robot]);
    else if (e.type === "repaired") toRadio(`${e.robot} fixed${e.by ? ` by ${e.by}` : ""}`, "ok", [e.robot]);
  }
  for (const r of f.robots) {
    const p = prev.get(r.id);
    if (!p) { prev.set(r.id, { st: r.st, job: r.job, d: r.d }); since.set(r.id, performance.now()); continue; }
    if (r.job && r.job !== p.job) {
      const j = jobs[r.job], o = j && j.order_id && orders[j.order_id], slot = j && j.lines[0] && j.lines[0].slot;
      push(r.id, "✚", `Assigned ${r.job}${slot ? `: load at ${faceName(slot)} → ${destination(j.dock)}` : ""}${o ? ` · ${o.lines[0]?.qty ?? ""} t for ${o.customer}` : ""}`, "assign", f.t);
    }
    if (r.st !== p.st) {
      since.set(r.id, performance.now());
      if (r.st === "moving" && !["waiting", "queued"].includes(p.st)) push(r.id, "➜", goalText(r), "", f.t);
      else if (["loading", "grade_check", "dumping"].includes(r.st)) push(r.id, "◉", nowText(r), "info", f.t);
      else if (r.st === "idle" && p.st !== "idle") push(r.id, "○", `Idle at ${cname(cellOf(r))}`, "", f.t);
      else if (r.st === "estop") push(r.id, "■", "Emergency stop", "bad", f.t);
      else if (r.st === "held") push(r.id, "❚❚", "Holding outside a road closed for blasting", "warn", f.t);
    }
    prev.set(r.id, { st: r.st, job: r.job, d: r.d });
  }
}

function onDecision(d, backfill = false) {
  if (!d) return;
  if (!backfill && site) site.decision(d, performance.now());
  if (d.kind === "dispatch" && d.phase === "ask") {
    if (!backfill && (d.faces || []).length) toRadio(`${d.faces.map((s) => excavator(s).id).join(", ")} ${d.faces.length > 1 ? "are" : "is"} alone: asking the dispatch AI`, "ai", [], `${(d.trucks || []).length} free truck(s)`);
    return;
  }
  if (d.kind === "dispatch" && d.phase === "done") {
    toRadio(`${d.by === "ai" ? "✦ Dispatch AI" : "Rules"}: ${d.robot} → ${faceName(d.face)}`, d.by === "ai" ? "ai" : "info", [d.robot], d.reason || `${d.road_m} m by road`, d.id);
    if (!backfill) push(d.robot, d.by === "ai" ? "✦" : "✚", `${d.by === "ai" ? "Dispatch AI" : "Rules"} sent me to ${faceName(d.face)} (${d.road_m} m): ${d.reason || ""}`, d.by === "ai" ? "ai" : "assign");
  } else if (d.kind === "traffic" && d.phase === "done") {
    toRadio(`${d.by === "ai" ? "✦ Traffic AI" : "Traffic rules"}: ${d.first} goes first, ${d.yield} ${d.how === "reroute" ? "re-routes" : "pulls aside"}`, d.by === "ai" ? "ai" : "arbiter", [d.first, d.yield],
      `${d.reason || ""}${d.ms ? ` · ${(d.ms / 1000).toFixed(1)} s` : ""}`, d.id);
  } else if (d.kind === "traffic" && d.phase === "ask" && !backfill) {
    // the standoff line on the radio already says the AI is being asked
  } else if (d.kind === "service" && d.phase === "done") {
    toRadio(`${d.by === "ai" ? "✦ AI" : "Rules"}: ${d.title}`, d.by === "ai" ? "ai" : "warn", [d.robot], d.reason || "", d.id);
  } else if (d.kind === "service" && d.phase === "alert") {
    toRadio(`⚠ ${d.title}: crew paged`, "bad", [d.robot], d.reason || "", d.id);
  } else if (d.kind === "roads") {
    toRadio(`✦ Road crew AI: ${d.crew} → ${{ sand: "sand drift", bump: "bumps", pothole: "pothole" }[d.defect] || d.defect} ${d.feature} at ${cname(d.cell)}`, "ai", [], `${d.reason || ""} · ETA ${d.eta_s} s`, d.id);
  } else if (d.kind === "hazard") {
    toRadio(`✦ Safety drill AI: injecting ${(SCENARIO_LABEL[d.scenario] || d.scenario)}`, "warn", [], d.reason || "", d.id);
  } else if (d.kind === "autopilot") {
    toRadio(`✦ Safety officer AI: ${d.fix} ${d.verdict}`, d.verdict === "approved" ? "ok" : "warn", [], d.reason || "", d.id);
  } else if (d.kind === "supervisor") {
    toRadio(`✦ Supervisor: ${d.headline}`, "ai", [], (d.actions || [])[0] || "", d.id);
  }
}

// ------------------------------------------------------------------ HUD

function renderUi() {
  if (!A) return;
  renderFleet();
  renderFaces();
  site.setTechs(techs(), serverOffset);
  if (panelMode === "robot") renderRobot();
  if (panelMode === "face") renderFace();
}

function renderFleet() {
  const el = $("#l3-fleet", root);
  if (el.children.length !== A.robots.length) {
    el.innerHTML = A.robots.map((r) => `<button class="fchip glass" data-id="${r.id}" style="--acc:${robotColor(r.id)}">
      <span class="sw"></span><b>${r.id}</b><span class="st"></span><span class="v"></span><span class="adv" title="The AI driver's current speed limit"></span></button>`).join("");
    for (const b of $$("#l3-fleet button", root)) b.onclick = () => openRobot(b.dataset.id);
  }
  for (const r of A.robots) {
    const b = el.querySelector(`[data-id="${r.id}"]`);
    b.classList.toggle("on", sel === r.id);
    b.classList.toggle("loaded", r.c > 0);
    const st = b.querySelector(".st");
    st.className = `st ${STATUS_KIND[r.st] || ""}`;
    st.textContent = r.st === "waiting" && r.w ? `hold · ${r.w}` : STATUS_LABEL[r.st] || r.st;
    b.querySelector(".v").textContent = `${kmhOf(r.v).toFixed(0)}`;
    const dp = drivers.get(r.id);
    b.querySelector(".adv").textContent = dp && Date.now() / 1000 - dp.at < 30 ? `✦ ${dp.kmh}` : "";
    if (dp) b.title = `AI driver: ${dp.kmh} km/h · ${dp.reason}`;
  }
}

function renderFaces() {
  const el = $("#l3-faces", root);
  const slots = Object.keys(cfg.map.slots).sort();
  if (el.children.length !== slots.length) {
    el.innerHTML = slots.map((s) => `<button class="face glass" data-s="${s}"><span class="ex"></span><span class="what"></span></button>`).join("");
    for (const b of $$("#l3-faces button", root)) b.onclick = () => openFace(b.dataset.s);
  }
  for (const s of slots) {
    const b = el.querySelector(`[data-s="${s}"]`), f = faces[s] || { trucks: [], alone_s: 0 };
    const at = A.robots.find((r) => (r.st === "loading" || r.st === "grade_check") && accessToSlot[cellOf(r).join(",")] === s);
    const alone = !at && !(f.trucks || []).length && f.alone_s >= 3;
    const ex = excavator(s);
    b.querySelector(".ex").innerHTML = `<b>${esc(ex.id)}</b><small>${esc(matName(`MAT-${s}`))}</small>`;
    b.querySelector(".what").textContent = at ? `loading ${at.id}` : alone ? `alone ${f.alone_s} s · needs a truck` : (f.trucks || []).length ? `${f.trucks.join(", ")} on the way` : "digging";
    b.dataset.kind = at ? "loading" : alone ? "alone" : "ok";
    b.classList.toggle("on", selFace === s);
    if (site) {
      const e = site.excavators.get(s);
      if (e) e.alone = alone;
      site.setExcavatorLabel(s, ex.id, at ? `loading ${at.id}` : alone ? `alone ${f.alone_s} s` : (f.trucks || []).length ? `${f.trucks[0]} coming` : "digging", at ? "loading" : alone ? "alone" : "ok");
    }
  }
}

function renderRadio() {
  const el = $("#l3-radio-list", root);
  if (!el) return;
  el.innerHTML = radio.map((m, i) => {
    const rv = m.id != null ? reviews.get(m.id) : null;
    const audit = rv ? `<i class="audit ${rv.verdict}" title="The auditor model reviewed this decision">${rv.verdict === "sound" ? "✓" : "⚠"} auditor: ${esc(rv.note || rv.verdict)}</i>`
      : m.id != null && m.kind === "ai" ? `<i class="audit pending">◌ auditor reviewing…</i>` : "";
    return `<li class="${m.kind} ${i === 0 ? "new" : ""}"><span class="t">${m.time}</span><span><b>${esc(m.text)}</b>${m.sub ? `<em>${esc(m.sub)}</em>` : ""}${audit}</span></li>`;
  }).join("") || `<li class="hint">Listening…</li>`;
}

function renderUsage(u) {
  if (!root || !u || !$("#l3-calls", root)) return;
  const big = (n) => (n >= 1e9 ? `${(n / 1e9).toFixed(2)}B` : n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e4 ? `${Math.round(n / 1000)}k` : Math.round(n).toLocaleString());
  $("#l3-calls", root).textContent = big(u.calls_total);
  $("#l3-cpm", root).textContent = big(u.calls_per_min);
  $("#l3-tpm", root).textContent = big(u.tokens_per_min);
  $("#l3-tok", root).textContent = big(u.tokens_total);
  const by = {};
  for (const [k, v] of Object.entries(u.by || {})) { const n = AGENT[k] || k; by[n] = (by[n] || 0) + v.calls; }
  const top = Object.entries(by).sort((a, b) => b[1] - a[1]).slice(0, 7), max = top.length ? top[0][1] || 1 : 1;
  $("#l3-agents", root).innerHTML = top.map(([n, calls]) => `<div><span>${esc(n)}</span><i><u style="width:${Math.max(3, calls / max * 100)}%"></u></i><b>${big(calls)}</b></div>`).join("");
}

function renderSupervisor(sv) {
  const el = root && $("#l3-super", root);   // the supervisor's calls also go out on the pit radio
  if (!el || !sv || !sv.headline) return;
  el.hidden = false;
  el.innerHTML = `<span class="lbl">✦ Shift supervisor AI</span><b>${esc(sv.headline)}</b>${(sv.risks || []).slice(0, 2).map((r) => `<em>⚠ ${esc(r)}</em>`).join("")}`;
}

// ------------------------------------------------------------------ service cases, alerts, fitters

function upsertCase(c, render = true) {
  serverOffset = Date.now() / 1000 - Number(c.server_now || Date.now() / 1000);
  const old = cases.get(c.id);
  cases.set(c.id, c);
  if (!old || old.status !== c.status) {
    const step = (c.steps || []).at(-1);
    if (old && step) push(c.robot_id, step.by === "copilot" ? "✦" : "●", `${step.title}${step.detail ? ` · ${step.detail}` : ""}`, step.by === "copilot" ? "ai" : c.status === "resolved" ? "ok" : "warn");
  }
  if (render) { renderCases(); renderAlerts(); }
}

function renderCases() {
  const list = [...cases.values()].sort((a, b) => b.id - a.id).slice(0, 4);
  const box = $("#l3-svc", root);
  box.hidden = !list.length;
  const html = list.map((c) => {
    const at = CASE_AT[c.status] ?? 0, steps = CASE_STEPS[c.fault] || CASE_STEPS.tire, last = (c.steps || []).at(-1) || {};
    return `<li class="${c.status}" data-robot="${c.robot_id}">
      <div class="row between"><span><b style="color:${robotColor(c.robot_id)}">${c.robot_id}</b> · ${esc(c.component || (c.code || "").split(" ").slice(1).join(" ") || "fault")}</span>
        ${pill(CASE_LABEL[c.status] || c.status, c.status === "resolved" ? "ok" : ["recovering", "safing", "diagnosing"].includes(c.status) ? "ai live" : "bad live")}</div>
      <div class="dots5">${steps.map((t, i) => `<i class="${i < at || c.status === "resolved" ? "done" : i === at ? "now" : ""}" title="${t}"></i>`).join("")}</div>
      <div class="hint">${last.by === "copilot" ? "✦ " : ""}${esc(last.title || "")}</div></li>`;
  }).join("");
  const ol = $("#l3-svc-list", root);
  if (ol.dataset.html === html) return;     // rebuilding every UI tick would restart the entry animation forever
  ol.dataset.html = html;
  ol.innerHTML = html;
  for (const li of $$("#l3-svc-list li", root)) li.onclick = () => openRobot(li.dataset.robot);
}

function renderAlerts() {
  // a broken-down truck pages the crew: the banner stays until the truck is fixed
  const open = [...cases.values()].filter((c) => !["resolved", "closed"].includes(c.status)).sort((a, b) => a.id - b.id);
  const el = $("#l3-alerts", root);
  const sig = open.map((c) => `${c.id}:${c.status}`).join("|");
  if (el.dataset.sig === sig) return;
  el.dataset.sig = sig;
  el.innerHTML = open.map((c) => {
    const t = c.technician, last = (c.steps || []).at(-1) || {};
    const fault = c.fault === "tire" ? "tyre failure" : c.fault === "sensor" ? "lidar fault" : (c.code || "").split(" ").slice(1).join(" ") || "hardware fault";
    const canFix = ["dispatched", "repairing", "in_repair"].includes(c.status);
    return `<div class="alert-card" data-case="${c.id}">
      <span class="siren"></span>
      <div class="msg"><b>${esc(c.robot_id)} is not working: ${esc(c.component || fault)}. Please fix it.</b>
        <span>${last.by === "copilot" ? "✦ AI: " : ""}${esc(last.title || "")}${t && t.name ? ` · ${esc(t.name)} (${esc(t.role)})` : ""}</span></div>
      <button class="ghost-btn" data-show="${c.robot_id}">Show truck</button>
      ${canFix ? `<button class="primary" data-done="${c.id}">Mark fixed</button>` : `<span class="pill ai live">${esc(CASE_LABEL[c.status] || c.status)}</span>`}
    </div>`;
  }).join("");
  for (const b of $$("[data-show]", el)) b.onclick = () => openRobot(b.dataset.show);
  for (const b of $$("[data-done]", el)) b.onclick = () => markDone(Number(b.dataset.done));
}

function techs() {
  return [...cases.values()].filter((c) => c.technician && ["dispatched", "repairing", "in_repair"].includes(c.status)).map((c) => ({
    id: c.id, robot: c.robot_id, ...c.technician, task: c.fault === "sensor" ? "recalibrating the lidar" : `changing the ${c.component || "tyre"}`,
  })).concat(crews);
}

async function markDone(id) {
  await act(() => api(`/api/service/${id}/done`, { body: {} }), "Marked fixed: the truck returns to service");
}

// ------------------------------------------------------------------ panel

function applyInset() {
  if (!site) return;
  const cam = site.camera, w = site.host.clientWidth, h = site.host.clientHeight;
  if (inset < 1) { if (cam.view && cam.view.enabled) cam.clearViewOffset(); return; }
  cam.setViewOffset(w, h, inset / 2, 0, w, h);
}

function openPanel(mode) {
  panelMode = mode;
  const p = $("#l3-panel", root);
  p.classList.add("open"); p.setAttribute("aria-hidden", "false");
  p.dataset.mode = mode;
  $("#l3-ask-site", root).classList.add("hide");
  document.body.classList.add("panel-open");
  insetGoal = window.innerWidth > 900 ? p.offsetWidth + 16 : 0;
}

function closePanel() {
  panelMode = null; sel = null; selFace = null;
  const p = $("#l3-panel", root);
  p.classList.remove("open"); p.setAttribute("aria-hidden", "true");
  $("#l3-ask-site", root).classList.remove("hide");
  document.body.classList.remove("panel-open");
  insetGoal = 0;
  if (site) site.overview();
  renderUi();
}

async function openRobot(id) {
  const first = sel !== id;
  sel = id; selFace = null;
  openPanel("robot");
  site.focus(id);
  copilot.setScope("robot", id);
  setTab("log");
  renderRobot(true);
  renderLog(false);
  if (first && !logFor(id).some((x) => x.backfill)) backfill(id);
}

function openFace(slot) {
  sel = null; selFace = slot;
  openPanel("face");
  site.focusFace(slot);
  copilot.setScope("site", null);
  renderFace(true);
}

function openSite() {
  sel = null; selFace = null;
  openPanel("site");
  site.overview();
  copilot.setScope("site", null);
  renderSiteHead();
  setTimeout(() => copilot.focus(), 350);
}

function setTab(t) {
  for (const b of $$("#l3p-tabs button", root)) b.classList.toggle("on", b.dataset.t === t);
  $("#l3-panel", root).dataset.tab = t;
  if (t === "ask") setTimeout(() => copilot.focus(), 200);
}

async function backfill(id) {
  try {
    const rows = await api(`/api/robots/${id}/events?limit=40`);
    const list = logFor(id);
    const old = [];
    for (const r of rows) {
      const e = { ...(r.payload || {}), type: r.type.replace(/^sim\./, "") };
      let d = null;
      if (r.type === "ai.dispatch") d = ["✦", `Dispatch AI sent me to ${faceName(e.face)} (${e.road_m} m): ${e.reason || ""}`, "ai"];
      else if (r.type === "dispatch") d = ["✚", `Rules sent me to ${faceName(e.face)} (${e.road_m ?? "?"} m)`, "assign"];
      else if (r.type === "ai.traffic" || r.type === "traffic.rule") d = ["↺", `${r.type === "ai.traffic" ? "✦ Traffic AI" : "Rules"}: ${e.first} first, ${e.yield} ${e.how === "reroute" ? "re-routes" : "pulls aside"}: ${e.reason || ""}`, r.type === "ai.traffic" ? "ai" : "arbiter"];
      else d = describe(e, id);
      if (d) old.push({ icon: d[0], text: d[1], kind: `${d[2]} old`, time: `t${r.tick}`, t: r.tick, backfill: true });
    }
    list.push(...old.slice(0, 40));
    if (sel === id) renderLog(false);
  } catch { /* live log still fills */ }
}

function renderSiteHead() {
  const f = cfg.site ? cfg.site.facility : null;
  $("#l3p-head", root).innerHTML = `
    <div class="who"><span class="bot-av site">✦</span><div><b>Pit copilot</b><div class="hint">${f ? `${esc(cfg.site.name)} · ${esc(f.company)}` : ""}</div></div></div>
    <button class="l3p-close" id="l3p-close" title="Close the panel (Esc)" aria-label="Close"><span>✕</span>Close</button>`;
  $("#l3p-close", root).onclick = closePanel;
  const robots = (A ? A.robots : []).map((r) => `<button class="mini" data-id="${r.id}" style="--acc:${robotColor(r.id)}"><span class="sw"></span>${r.id} · ${STATUS_LABEL[r.st] || r.st}</button>`).join("");
  $("#l3p-site", root).innerHTML = `<div class="hint">Ask about traffic, production, potholes, the AI's decisions or incidents across the pit. Or open a truck:</div><div class="minis">${robots}</div>`;
  for (const b of $$("#l3p-site .mini", root)) b.onclick = () => openRobot(b.dataset.id);
}

function renderFace(force) {
  const s = selFace; if (!s) return;
  const ex = excavator(s), it = item(`MAT-${s}`), f = faces[s] || { trucks: [], alone_s: 0 };
  const head = $("#l3p-head", root);
  if (force || head.dataset.id !== `face:${s}`) {
    head.dataset.id = `face:${s}`;
    head.innerHTML = `
      <div class="who"><span class="bot-av exc"><em>${esc(s)}</em></span>
        <div><b>${esc(ex.id)} · ${esc(ex.model || "Hydraulic excavator")}</b><div class="hint mono">face ${esc(s)} · bucket ${ex.bucket_m3 || "–"} m³</div></div></div>
      <div class="row"><button class="l3p-close" id="l3p-close" title="Close the panel (Esc)" aria-label="Close"><span>✕</span>Close</button></div>`;
    $("#l3p-close", root).onclick = closePanel;
  }
  const at = A && A.robots.find((r) => (r.st === "loading" || r.st === "grade_check") && accessToSlot[cellOf(r).join(",")] === s);
  const alone = !at && !(f.trucks || []).length && f.alone_s >= 3;
  const dests = destination(cfg.map.destination[cfg.map.slots[s].cls]);
  const recent = radio.filter((m) => m.text.includes(ex.id) || m.text.includes(`face ${s}`)).slice(0, 5);
  $("#l3p-face", root).innerHTML = `
    <div class="l3p-now ${alone ? "alone" : ""}"><span class="lbl">Now</span><span class="txt">${at ? `Loading ${at.id} pass by pass` : alone ? `Alone for ${f.alone_s} s: the dispatch AI is sending a truck` : (f.trucks || []).length ? `Digging while ${f.trucks.join(", ")} ${f.trucks.length > 1 ? "are" : "is"} on the way` : "Digging the face"}</span></div>
    <div class="l3p-sensors">
      ${tile("Material", esc(it ? it.name : cfg.map.slots[s].cls), it ? `${esc(it.sku)} · ${esc(it.grade || "")}` : "")}
      ${tile("Goes to", esc(dests), `${it ? `${Math.round(it.unit_weight)} t per load` : ""}`)}
      ${tile("Value", it ? `${money(it.unit_value)}<small> /t</small>` : "–", it ? `${money(it.unit_value * it.unit_weight)} per load` : "")}
      ${tile("Trucks", `${(f.trucks || []).length + (at ? 1 : 0)}`, at ? `${at.id} under the bucket` : (f.trucks || []).join(", ") || "none assigned")}
    </div>
    <div class="l3p-load"><span class="lbl">Recent decisions for this face</span>
      ${recent.length ? `<ul class="decs">${recent.map((m) => `<li class="${m.kind}"><b>${esc(m.text)}</b>${m.sub ? `<span>${esc(m.sub)}</span>` : ""}</li>`).join("")}</ul>` : `<div class="hint">None yet this session.</div>`}</div>`;
}

function renderRobot(force) {
  const r = A && A.robots.find((x) => x.id === sel);
  if (!r) return;
  const asset = cfg.site && cfg.site.robots.find((a) => a.id === r.id);
  const head = $("#l3p-head", root);
  if (force || head.dataset.id !== r.id) {
    head.dataset.id = r.id;
    head.innerHTML = `
      <div class="who"><span class="bot-av" style="--acc:${robotColor(r.id)}"><i></i><em>${r.id}</em></span>
        <div><b>${r.id}${asset ? ` · ${esc(asset.model)}` : ""}</b><div class="hint mono">${asset ? `${esc(asset.serial)} · fw ${esc(asset.firmware)} · ${asset.payload_t} t payload` : ""}</div></div></div>
      <div class="row"><span id="l3p-st"></span><button class="l3p-close" id="l3p-close" title="Close the panel (Esc)" aria-label="Close"><span>✕</span>Close</button></div>
      <div class="following"><span class="live-dot"></span>Following live · the truck keeps hauling <button class="linkish" id="l3p-askmark">✦ Ask about ${r.id}</button></div>`;
    $("#l3p-close", root).onclick = closePanel;
    $("#l3p-askmark", root).onclick = () => setTab("ask");
  }
  $("#l3p-st", root).innerHTML = pill(STATUS_LABEL[r.st] || r.st, `${STATUS_KIND[r.st] || ""} ${r.st === "moving" ? "live" : ""}`);
  const now = $("#l3p-now", root), txt = nowText(r);
  if (now.dataset.txt !== txt) { now.dataset.txt = txt; now.innerHTML = `<span class="lbl">Now</span><span class="txt">${esc(txt)}</span>`; now.classList.remove("swap"); void now.offsetWidth; now.classList.add("swap"); }
  const s = sensorsOf(r), inState = ((performance.now() - (since.get(r.id) || performance.now())) / 1000).toFixed(0);
  const unsafe = s.kmh > 1 && s.stop > s.lidarM;
  const obs = s.near && s.near.gap < 150000 ? `${Math.max(0, s.near.gap / 1000).toFixed(0)} m` : "clear";
  const obsSub = s.near && s.near.gap < 150000 ? `${s.near.what} ahead` : `nothing within 150 m`;
  const hole = s.holes[0];
  renderCase(r);
  renderHealth(r);
  $("#l3p-sensors", root).innerHTML = [
    tile("Speed", `${s.kmh.toFixed(0)}<small> km/h</small>`, `limit ${s.limit.toFixed(0)} km/h${r.c ? " loaded" : ""}`, bar(s.kmh / 45, s.kmh > s.limit + .5 ? "bad" : "ok", s.limit / 45)),
    tile("Heading", `<span class="compass" style="--r:${{ E: 90, S: 180, W: 270, N: 0 }[r.d] || 0}deg">➤</span>${HEADING[r.d] || "–"}`, `${cname(s.c)} · ${esc((s.zones.map(zoneLabel)[0]) || "road")}`),
    tile("Lidar", `${s.lidarM.toFixed(0)}<small> m</small>`, r.h && r.h.lidar < 70 ? `degraded · ${r.h.lidar}% returns` : `${r.h ? r.h.lidar : "–"}% returns`, bar(s.lidarM / 60, s.lidarM < 60 ? "bad" : "info")),
    tile("Obstacle ahead", obs, obsSub, bar(s.near ? 1 - Math.min(1, Math.max(0, s.near.gap) / 60000) : 0, s.near && s.near.gap < s.stop * 1000 ? "bad" : "info")),
    tile("Stopping distance", `${s.stop.toFixed(0)}<small> m</small>`, `lidar sees ${s.lidarM.toFixed(0)} m`, bar(s.stop / 70, unsafe ? "bad" : "ok", s.lidarM / 70)),
    tile("Potholes", hole ? `${hole.d.toFixed(0)}<small> m</small>` : "–", hole ? `${hole.onRoute ? "on route" : "nearby"} · ${(hole.depth / 1000).toFixed(1)} m deep` : "none mapped nearby", hole ? bar(1 - Math.min(1, hole.d / 120), hole.onRoute ? "bad" : "info") : ""),
    tile("Odometer", `${((r.o || 0) / 1e6).toFixed(2)}<small> km</small>`, "this shift"),
    tile(`${STATUS_LABEL[r.st] || r.st} for`, `${inState}<small> s</small>`, r.w ? `waiting on ${r.w}` : "live"),
  ].join("");
  renderLoad(r);
}

function renderCase(r) {
  const el = $("#l3p-case", root), c = caseOf(r.id);
  if (!c) { el.innerHTML = ""; el.dataset.sig = ""; return; }
  const sig = `${c.id}|${c.status}|${(c.steps || []).length}`;
  if (el.dataset.sig === sig) return;
  el.dataset.sig = sig;
  const at = CASE_AT[c.status] ?? 0, names = CASE_STEPS[c.fault] || CASE_STEPS.tire;
  const t = c.technician;
  el.innerHTML = `<div class="row between"><span class="lbl">Service case #${c.id}${c.fault ? ` · ${c.fault === "tire" ? "tyre" : "lidar"} fault` : ""}</span>
      ${pill(CASE_LABEL[c.status] || c.status, c.status === "resolved" ? "ok" : "bad live")}</div>
    <ol class="cstep">${names.map((n, i) => `<li class="${i < at || c.status === "resolved" ? "done" : i === at ? "now" : ""}"><i></i><span>${n}</span></li>`).join("")}</ol>
    <ul class="ctl">${(c.steps || []).slice().reverse().map((st) => {
      const [who, cls] = BY[st.by] || [st.by, ""];
      return `<li><span class="by ${cls}">${esc(who)}</span><div><b>${esc(st.title || "")}</b>${st.detail ? `<span>${esc(st.detail)}</span>` : ""}
        ${(st.evidence || []).length ? `<em>${st.evidence.map(esc).join(" · ")}</em>` : ""}</div>
        <span class="tm">${new Date(st.at * 1000).toLocaleTimeString([], { hour12: false })}</span></li>`;
    }).join("")}</ul>
    ${t && ["dispatched", "repairing", "in_repair"].includes(c.status) ? `<div class="row between tech-row"><span>🔧 <b>${esc(t.name)}</b> · ${esc(t.role)}</span>
      <button class="primary" id="l3p-done">Mark fixed</button></div>` : ""}`;
  const b = $("#l3p-done", el);
  if (b) b.onclick = () => markDone(c.id);
}

const TYRES = ["FL", "FR", "RL1", "RL2", "RR1", "RR2"];
function renderHealth(r) {
  const h = r.h;
  const el = $("#l3p-health", root);
  if (!h) { el.innerHTML = ""; return; }
  const low = (p) => p < 60;
  const tire = (w) => `<span class="tire t${w} ${low(h.tires[w]) ? "bad" : ""}" title="${w}"><b>${h.tires[w] ?? "–"}</b><small>psi</small></span>`;
  const row = (label, v, bad, unit = "") => `<div class="hrow ${bad ? "bad" : ""}"><i></i><span>${label}</span><b>${v}${unit}</b></div>`;
  el.innerHTML = `<div class="row between"><span class="lbl">Health</span><span class="hint">${r.f ? `fault: ${r.f.type === "tire" ? "tyre" : "lidar"}` : "all nominal"}</span></div>
    <div class="hgrid">
      <div class="chassis six">${TYRES.map(tire).join("")}<span class="body"><i class="lidar ${h.lidar < 70 ? "bad" : ""}"></i></span></div>
      <div class="hrows">${row("Wheel slip", h.slip, h.slip > 12, "%")}${row("Drive imbalance", h.imbalance, h.imbalance > 20, "%")}
        ${row("Vibration", h.vib, h.vib > 0.4, " g")}${row("Lidar returns", h.lidar, h.lidar < 70, "%")}${row("Lidar range", (h.range / 1000).toFixed(0), h.range < 60000, " m")}</div>
    </div>`;
}

const tile = (label, value, sub, extra = "") => `<div class="stile"><span class="lbl">${label}</span><b>${value}</b><span class="sub">${sub || ""}</span>${extra}</div>`;
const bar = (k, kind, mark = null) => `<div class="mbar ${kind}"><i style="width:${Math.round(Math.min(1, Math.max(0, k)) * 100)}%"></i>${mark != null ? `<u style="left:${Math.round(Math.min(1, mark) * 100)}%"></u>` : ""}</div>`;

function renderLoad(r) {
  const { j, o } = jobInfo(r);
  const slot = j && j.lines[0] && j.lines[0].slot;
  const it = slot && item(`MAT-${slot}`);
  const el = $("#l3p-load", root);
  const tonnes = o && o.lines && o.lines[0] ? o.lines[0].qty : it ? Math.round(it.unit_weight) : null;
  const html = `
    <div class="row between"><span class="lbl">Load</span><span class="hint">${r.c ? "on board" : j ? "empty · heading to the face" : ""}</span></div>
    ${j ? `<div class="loadline"><span class="heap ${r.c ? "full" : ""}" style="--mat:${it ? matColor(slot) : "#8c5a3a"}"></span>
      <div><b>${esc(it ? it.name : slot)}</b><span class="mono">${tonnes ?? "–"} t · ${esc(it ? it.grade || it.sku : "")}</span></div></div>
      <div class="jobline"><span class="mono">${esc(j.id)}</span><span>${esc(faceName(slot))}</span><span>→ ${esc(destination(j.dock))}</span>
        ${o ? `<b>${esc(o.customer)}</b>${pill(o.tier, TIER[o.tier] || "")}<span>${money(o.value)}</span>` : ""}</div>`
    : `<div class="hint">No load assigned${r.st === "standby" ? ": standby spare" : ""}</div>`}`;
  if (el.dataset.sig !== html) { el.dataset.sig = html; el.innerHTML = html; }
}

function matColor(slot) {
  const cls = cfg.map.slots[slot] && cfg.map.slots[slot].cls;
  return { ore: "#7e4b2f", lowgrade: "#9a7650", waste: "#6c6862", sand: "#d6b27a" }[cls] || "#8c5a3a";
}

function renderLog(fresh) {
  const el = $("#l3p-log", root);
  const list = logFor(sel);
  el.innerHTML = list.slice(0, 80).map((x, i) => `<li class="${x.kind} ${fresh && i === 0 ? "new" : ""}"><span class="ic">${x.icon}</span><span class="tx">${esc(x.text)}</span><span class="tm">${x.time}</span></li>`).join("")
    || `<li class="hint empty">Watching ${esc(sel)}… events appear as they happen.</li>`;
}
