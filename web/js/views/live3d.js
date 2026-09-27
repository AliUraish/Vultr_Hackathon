// Live 3D floor: the real fleet from VM B, rendered in three.js. Tap a trailer to follow it (it keeps working),
// read its live log and sensors, and ask the copilot about it or about the whole site.
import { $, $$, esc, api, act, toast, bus, cfg, item, money, pill, TIER, countTo, barcodeSVG, eanText } from "../util.js";
import { live } from "../live.js";
import { robotColor, stopDist } from "../floor.js";
import { Copilot } from "../copilot.js";

const CHAOS = [
  ["pallet_drop", "▣", "Drop a pallet in an aisle"], ["mislabel_bin", "⌗", "Mislabel a bin"],
  ["worker_in_aisle", "⛔", "Worker closes an aisle"], ["tire_fault", "◍", "Flat tire on a working trailer"],
  ["sensor_fault", "◎", "Lidar fault on a working trailer"], ["clear_floor", "✦", "Clear the floor"],
];
const STATUS_LABEL = { moving: "moving", waiting: "holding", held: "holding", blocked: "blocked", estop: "e-stop",
  picking: "picking", dropping: "unloading", scanning: "scanning", idle: "idle", fault: "fault", standby: "standby" };
const STATUS_KIND = { moving: "ok", waiting: "warn", held: "warn", blocked: "bad", estop: "bad", picking: "info", dropping: "info", scanning: "info",
  idle: "", fault: "bad", standby: "" };
const CASE_STEPS = { tire: ["Alarm", "Pulled over", "Diagnosed", "Technician", "Fixed"], sensor: ["Alarm", "Pulled over", "Diagnosed", "Garage + spare", "Fixed"] };
const CASE_AT = { detected: 0, safing: 1, diagnosing: 2, recovering: 3, in_repair: 3, dispatched: 3, repairing: 3, resolved: 4 };
const CASE_LABEL = { detected: "alarm raised", safing: "pulling over", diagnosing: "diagnosing", recovering: "AI driving to garage",
  in_repair: "repair in bay", dispatched: "technician on the way", repairing: "technician repairing", resolved: "fixed", closed: "closed" };
const BY = { copilot: ["✦ Copilot", "ai"], rules: ["Rules", ""], technician: ["Technician", "tech"], robot: ["Robot", ""], fleet: ["Fleet", ""], operator: ["Operator", "op"] };
const RULE = { head_on: "head-on standoff: the higher id yields", head_on_retry: "still blocked: the lower id yields too",
  parked: "blocked by a parked robot: re-route around it", queue: "queued too long behind a robot: re-route" };
const HEADING = { E: "east", W: "west", N: "north", S: "south" };

let root = null, site = null, raf = 0, ready = null, sel = null, panelMode = null, copilot = null;
let A = null, jobs = {}, orders = {}, rules = [], cellZones = {}, accessToSlot = {}, homes = new Set();
const logs = new Map(), prev = new Map(), since = new Map(), radio = [];
let lastUi = 0, pollTimer = null, inset = 0, insetGoal = 0, serverOffset = 0;
const cases = new Map();   // service cases by id

// ------------------------------------------------------------------ mount

export function mount(el) {
  root = el;
  root.innerHTML = `
    <div class="l3">
      <div class="l3-stage" id="l3-stage"><div class="l3-loading"><div class="spinner"></div>Building the floor…</div></div>
      <div class="l3-hud tl glass" id="l3-site">
        <div class="row"><span class="live-dot"></span><b id="l3-site-name">Live floor</b></div>
        <div class="hint" id="l3-site-sub">connecting to the fleet…</div>
        <div class="l3-kpis" id="l3-kpis"></div>
      </div>
      <div class="l3-hud tr">
        <div class="seg glass" id="l3-layers">
          <button data-l="routes" class="on" title="Planned routes">Routes</button><button data-l="claims" class="on" title="Cells each robot has reserved">Claims</button>
          <button data-l="sensors" class="on" title="Forward sensor and stopping distance">Sensors</button><button data-l="labels" class="on">Labels</button>
        </div>
        <button class="glass" id="l3-overview" title="Back to the whole floor (Esc)">⤢ Overview</button>
        <div class="l3-menu">
          <button class="glass" id="l3-inject">⚡ Inject</button>
          <div class="l3-drop glass" id="l3-drop" hidden>${CHAOS.map(([k, i, t]) => `<button data-s="${k}"><span>${i}</span>${t}</button>`).join("")}</div>
        </div>
      </div>
      <div class="l3-hud tl2 glass" id="l3-svc" hidden>
        <div class="row between"><h3>Service</h3><span class="hint">faults · recovery · repairs</span></div>
        <ol id="l3-svc-list"></ol>
      </div>
      <div class="l3-hud bl glass" id="l3-radio">
        <div class="row between"><h3>Fleet radio</h3><span class="hint">reservations · holds · arbiter</span></div>
        <ol id="l3-radio-list"><li class="hint">Listening…</li></ol>
      </div>
      <div class="l3-hud bc" id="l3-fleet"></div>
      <button class="l3-hud br l3-ask glass" id="l3-ask-site"><span class="orb">✦</span> Ask about the site</button>
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
          <div class="l3p-chat" id="l3p-chat"></div>
        </div>
      </aside>
    </div>`;
  copilot = new Copilot($("#l3p-chat", root));
  for (const b of $$("#l3-layers button", root)) b.onclick = () => { b.classList.toggle("on"); if (site) site.layers[b.dataset.l] = b.classList.contains("on"); };
  $("#l3-overview", root).onclick = () => closePanel();
  $("#l3-inject", root).onclick = (e) => { e.stopPropagation(); $("#l3-drop", root).hidden = !$("#l3-drop", root).hidden; };
  document.addEventListener("click", () => { const d = root && $("#l3-drop", root); if (d) d.hidden = true; });
  for (const b of $$("#l3-drop button", root)) {
    b.onclick = async () => {
      const r = await act(() => api("/api/chaos", { body: { scenario: b.dataset.s } }));
      if (r) toast(b.dataset.s === "clear_floor" ? "Floor cleared" : `${b.textContent.trim()}: armed, fires when a robot is in position`, "warn");
    };
  }
  $("#l3-ask-site", root).onclick = () => openSite();
  for (const b of $$("#l3p-tabs button", root)) b.onclick = () => setTab(b.dataset.t);
  bus.on("state", (st) => {
    jobs = Object.fromEntries(st.jobs.map((j) => [j.id, j]));
    rules = st.policy.rules || [];
  });
  bus.on("site", () => applySite());
  bus.on("service", (c) => { upsertCase(c); });
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
      const accents = Object.fromEntries(cfg.map.robots.map((r) => [r.id, robotColor(r.id)]));
      site = new Site3D(stage, cfg.map, { accents, onPick: (id) => (id ? openRobot(id) : null), onHover: () => {},
        onAsk: (id) => { if (sel !== id) openRobot(id); setTab("ask"); } });
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
  cancelAnimationFrame(raf);
  const loop = (now) => {
    const s = live.advance(now);
    if (s && s.a) {
      A = s.a;
      for (const f of s.crossed) ingest(f, now);
      site.update(s.a, s.b, s.alpha, now);
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
    const [o, k, sv] = await Promise.all([api("/api/orders?limit=60"), api("/api/kpis"), api("/api/service")]);
    orders = Object.fromEntries(o.map((x) => [x.id, x]));
    const seen = new Set(sv.map((c) => c.id));
    for (const id of [...cases.keys()]) if (!seen.has(id)) cases.delete(id);
    for (const c of sv) upsertCase(c, false);
    renderCases();
    renderKpis(k);
    if (site && cfg.site) for (const c of cfg.site.carriers) site.setDockInfo(c.dock, `${c.carrier}`, k.docks?.[c.dock]?.units ?? 0);
  } catch { /* next poll */ }
}

function applySite() {
  if (!cfg.site) return;
  const f = cfg.site.facility;
  $("#l3-site-name", root).textContent = `${cfg.site.name}`;
  $("#l3-site-sub", root).textContent = `${f.company} · ${cfg.site.code} · ${f.city}`;
  if (site) for (const c of cfg.site.carriers) site.setDockInfo(c.dock, c.carrier, null);
}

function renderKpis(k) {
  const el = $("#l3-kpis", root);
  if (!el.children.length) {
    el.innerHTML = [["orders_per_hour", "orders/h"], ["shipped", "shipped"], ["open", "in progress"], ["incidents_open", "incidents"]]
      .map(([key, label]) => `<div data-k="${key}"><b data-v="0">0</b><span>${label}</span></div>`).join("");
  }
  for (const d of el.children) countTo(d.querySelector("b"), Number(k[d.dataset.k] || 0), { ms: 700 });
  el.querySelector('[data-k="incidents_open"]').classList.toggle("alert", k.incidents_open > 0);
}

// ------------------------------------------------------------------ describing the fleet

const cellOf = (r) => [Math.floor(r.x / 1000), Math.floor(r.y / 1000)];
const cname = (c) => (c ? `c${c[0]}_${c[1]}` : "–");
const zoneOf = (c) => (cellZones[c.join(",")] || []).filter((z) => z !== "racks");
const zoneName = (z) => z.replace("aisle_", "aisle ").replace("_", " ");
const itemName = (sku) => { const it = item(sku); return it ? it.name : sku; };
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
  if (r.op === "goto" && slot) return `Driving to ${slot} to pick ${itemName(`SKU-${slot}`)}${o ? ` for ${o.id}` : ""}`;
  if (r.op === "goto" && g && Object.values(cfg.map.docks).some((d) => d.join(",") === g)) {
    const dock = Object.entries(cfg.map.docks).find(([, d]) => d.join(",") === g)[0];
    return `Delivering ${r.c} item${r.c === 1 ? "" : "s"} to ${dock.replace("DK", "Dock ")}${o ? ` · ${o.customer}` : ""}`;
  }
  if (r.op === "goto" && g && homes.has(g)) return "Returning to its charge bay";
  if (r.op === "goto") return j ? `Stepping aside to clear the lane` : `Repositioning`;
  return j ? `Working ${j.id}` : "Idle";
}

function caseOf(rid) {
  return [...cases.values()].filter((c) => c.robot_id === rid).sort((a, b) => b.id - a.id)[0] || null;
}

function nowText(r) {
  const st = r.st;
  const c = caseOf(r.id);
  if (r.sv === "remote") return `Under AI remote control: driving to ${cname(r.g)} at limp speed${r.f ? ` (${r.f.type} fault)` : ""}`;
  if (st === "fault") return c && c.status !== "resolved" ? `Stopped with a ${c.fault || r.f?.type || ""} fault · ${CASE_LABEL[c.status] || c.status}` : "Stopped: hardware fault";
  if (st === "standby") return "Standby in the garage, ready to deploy";
  if (st === "moving") return goalText(r);
  if (st === "waiting") return r.w ? `Holding: ${r.w} has the next cell reserved` : "Holding for traffic";
  if (st === "held") return "Holding outside a closed aisle";
  if (st === "blocked") return "Stopped: obstacle ahead";
  if (st === "estop") return "Emergency stop";
  const { j, o } = jobInfo(r);
  const slot = r.g && accessToSlot[r.g.join(",")];
  if (st === "picking") return `Picking ${slot ? itemName(`SKU-${slot}`) : "an item"} from ${slot || "the rack"}`;
  if (st === "scanning") return `Scanning the label on bin ${slot || ""} before picking`;
  if (st === "dropping") return `Unloading at the dock${o ? ` for ${o.customer}` : ""}`;
  return j ? `Waiting to start ${j.id}` : `Idle at ${cname(cellOf(r))}`;
}

function sensorsOf(r) {
  const c = cellOf(r), zones = zoneOf(c);
  const dirs = { E: [1, 0], W: [-1, 0], S: [0, 1], N: [0, -1] }[r.d] || [1, 0];
  let near = null;
  for (const p of (A && A.pallets) || []) {
    const px = p[0] * 1000 + 500, py = p[1] * 1000 + 500;
    const ahead = (px - r.x) * dirs[0] + (py - r.y) * dirs[1], side = Math.abs((px - r.x) * dirs[1] - (py - r.y) * dirs[0]);
    if (ahead > 0 && side < 700) { const gap = ahead - 700; if (!near || gap < near.gap) near = { gap, what: "pallet" }; }
  }
  for (const o of (A && A.robots) || []) {
    if (o.id === r.id) continue;
    const ahead = (o.x - r.x) * dirs[0] + (o.y - r.y) * dirs[1], side = Math.abs((o.x - r.x) * dirs[1] - (o.y - r.y) * dirs[0]);
    if (ahead > 0 && side < 600) { const gap = ahead - 600; if (!near || gap < near.gap) near = { gap, what: o.id }; }
  }
  const caps = rules.map((x) => x.match(/^speed_cap\((\w+),\s*([\d.]+)\)$/)).filter((m) => m && zones.includes(m[1])).map((m) => Number(m[2]));
  const racks = (cellZones[c.join(",")] || []).includes("racks");
  const capRacks = rules.map((x) => x.match(/^speed_cap\(racks,\s*([\d.]+)\)$/)).filter(Boolean).map((m) => Number(m[1]));
  const limit = Math.min(1.2, ...caps, ...(racks ? capRacks : []));
  const stop = (stopDist(r.v) + 100) / 1000;
  return { c, zones, near, limit, stop, mps: (r.v * 10) / 1000 };
}

// ------------------------------------------------------------------ events -> logs, radio, effects

function logFor(id) { if (!logs.has(id)) logs.set(id, []); return logs.get(id); }

function push(id, icon, text, kind = "", t = A ? A.t : 0) {
  const list = logFor(id);
  list.unshift({ icon, text, kind, time: clock(), t });
  if (list.length > 120) list.pop();
  if (panelMode === "robot" && sel === id) renderLog(true);
}

function toRadio(text, kind, robots) {
  radio.unshift({ text, kind, robots, time: clock() });
  if (radio.length > 5) radio.pop();
  renderRadio();
}

function describe(e, id) {
  switch (e.type) {
    case "pick": return ["▣", `Picked ${itemName(e.sku)} from ${e.slot}`, "info"];
    case "scan": return e.ok === false ? ["⚠", `Scan at ${e.slot}: label doesn't match`, "bad"] : ["⌁", `Scanned bin ${e.slot}: label matches`, "info"];
    case "scan_mismatch": return ["⚠", `Label mismatch at ${e.slot}: job handed to a person`, "bad"];
    case "dock_scan": return e.ok ? ["⇥", `Delivered ${(e.actual || []).length} item(s) to ${e.dock.replace("DK", "Dock ")}: dock scan OK`, "ok"]
      : ["✕", `Wrong item at ${e.dock}: expected ${(e.expected || []).map(itemName).join(", ")}, got ${(e.actual || []).map(itemName).join(", ")}`, "bad"];
    case "job_done": return ["✓", `Job ${e.job} complete`, "ok"];
    case "job_exception": return ["⚑", `Job ${e.job} handed to a person (${e.reason})`, "warn"];
    case "wait": return e.robot === id ? ["❚❚", `Hold: ${e.on} has ${cname(e.cell)} reserved${e.other_moving ? " (it's moving)" : " (it's stopped)"}`, "warn"]
      : ["⟵", `${e.robot} is holding for me: I have ${cname(e.cell)}`, "dim"];
    case "standoff": return ["⇄", `Standoff with ${e.robot === id ? e.with : e.robot}: both holding, arbiter deciding`, "bad"];
    case "yield":
      return e.robot === id ? ["↺", `Arbiter: I yield to ${e.to} after ${(e.waited / 10).toFixed(1)} s (${RULE[e.rule] || e.rule}), re-routing`, "arbiter"]
        : ["→", `Arbiter: ${e.robot} yields to me (${RULE[e.rule] || e.rule}), I keep the lane`, "arbiter"];
    case "resume": return ["▶", `Resumed after ${(e.after / 10).toFixed(1)} s${e.from ? ` (${e.from} cleared)` : ""}`, "ok"];
    case "contact": return ["✸", `COLLISION${e.with ? ` with ${e.with}` : ""} at ${((e.v || 0) * 10 / 1000).toFixed(1)} m/s`, "bad"];
    case "struck": return ["✸", `Struck by ${e.by || "another robot"}`, "bad"];
    case "fault_alarm": return ["⚠", `ALARM ${e.code}: safety stop at ${cname(e.cell)}`, "bad"];
    case "job_released": return ["↩", `Job ${e.job} handed back to the fleet${(e.held || []).length ? `; ${(e.held || []).length} item(s) held on board` : ""}`, "warn"];
    case "service_move": return ["✦", `AI remote control: driving to ${cname(e.cell)}`, "ai"];
    case "service_arrived": return ["■", `Parked at ${cname(e.cell)} under remote control`, "ai"];
    case "deployed": return ["▲", `Deployed from the garage into service`, "ok"];
    case "standby": return ["◻", `Parked as standby at ${cname(e.cell)}`, ""];
    case "repaired": return ["✓", `Repaired${e.by ? `, marked done by ${e.by}` : ""}${(e.returned || []).length ? `; held items back to stock` : ""}`, "ok"];
    case "zone_enter": return e.restricted ? ["⛔", `Entered CLOSED ${zoneName(e.zone)}`, "bad"] : null;
    default: return null;
  }
}

function ingest(f, now) {
  for (const e of f.ev || []) {
    site.event(e, f, now);
    const involved = new Set([e.robot, e.to, e.with, e.on].filter(Boolean));
    for (const id of involved) { const d = describe(e, id); if (d && (id === e.robot || ["yield", "standoff", "wait"].includes(e.type))) push(id, ...d, f.t); }
    if (e.type === "wait") toRadio(`${e.robot} holds for ${e.on} · ${cname(e.cell)} reserved`, "warn", [e.robot, e.on]);
    else if (e.type === "standoff") toRadio(`Standoff ${e.robot} ⇄ ${e.with} at ${cname(e.cell)}`, "bad", [e.robot, e.with]);
    else if (e.type === "yield") toRadio(`Arbiter: ${e.robot} yields → ${e.to} · ${RULE[e.rule] || e.rule}`, "arbiter", [e.robot, e.to]);
    else if (e.type === "pallet_dropped") toRadio(`Pallet down at ${cname(e.cell)}: robots re-plan around it`, "bad", []);
    else if (e.type === "zone_restricted") toRadio(`${zoneName(e.zone)} closed by a worker`, "bad", []);
    else if (e.type === "contact") toRadio(`Collision: ${e.robot}${e.with ? ` × ${e.with}` : ""}`, "bad", [e.robot]);
    else if (e.type === "fault_alarm") toRadio(`${e.robot} ALARM ${e.code} · service case opened`, "bad", [e.robot]);
    else if (e.type === "service_move") toRadio(`Copilot takes remote control of ${e.robot} → ${cname(e.cell)}`, "ai", [e.robot]);
    else if (e.type === "deployed") toRadio(`${e.robot} deployed from the garage`, "ok", [e.robot]);
    else if (e.type === "repaired") toRadio(`${e.robot} repaired${e.by ? ` by ${e.by}` : ""}`, "ok", [e.robot]);
  }
  // state transitions the sim doesn't emit as events
  for (const r of f.robots) {
    const p = prev.get(r.id);
    if (!p) { prev.set(r.id, { st: r.st, job: r.job, d: r.d }); since.set(r.id, performance.now()); continue; }
    if (r.job && r.job !== p.job) {
      const j = jobs[r.job], o = j && j.order_id && orders[j.order_id];
      const picks = j ? j.lines.map((l) => `${itemName(l.sku)} (${l.slot})`).join(", ") : "";
      push(r.id, "✚", `Assigned ${r.job}${o ? ` · ${o.id} for ${o.customer}` : ""}${picks ? `: pick ${picks} → ${j.dock.replace("DK", "Dock ")}` : ""}`, "assign", f.t);
      toRadio(`${r.id} takes ${r.job}${o ? ` · ${o.customer}` : ""}`, "info", [r.id]);
    }
    if (r.st !== p.st) {
      since.set(r.id, performance.now());
      if (r.st === "moving" && p.st !== "waiting") push(r.id, "➜", goalText(r), "", f.t);
      else if (["picking", "scanning", "dropping"].includes(r.st)) push(r.id, "◉", nowText(r), "info", f.t);
      else if (r.st === "idle" && p.st !== "idle") push(r.id, "○", `Idle at ${cname(cellOf(r))}`, "", f.t);
      else if (r.st === "estop") push(r.id, "■", "Emergency stop", "bad", f.t);
      else if (r.st === "held") push(r.id, "❚❚", "Holding outside a closed aisle", "warn", f.t);
    } else if (r.st === "moving" && r.d !== p.d) {
      push(r.id, "↱", `Turned ${HEADING[r.d]} at ${cname(cellOf(r))}`, "dim", f.t);
    }
    prev.set(r.id, { st: r.st, job: r.job, d: r.d });
  }
}

// ------------------------------------------------------------------ HUD

function renderUi() {
  if (!A) return;
  renderFleet();
  site.setTargets(targets());
  site.setTechs(techs(), serverOffset);
  if (panelMode === "robot") renderRobot();
}

function renderFleet() {
  // built once, then updated in place: replacing the buttons every frame would swallow clicks
  const el = $("#l3-fleet", root);
  if (el.children.length !== A.robots.length) {
    el.innerHTML = A.robots.map((r) => `<button class="fchip glass" data-id="${r.id}" style="--acc:${robotColor(r.id)}">
      <span class="sw"></span><b>${r.id}</b><span class="st"></span><span class="v"></span></button>`).join("");
    for (const b of $$("#l3-fleet button", root)) b.onclick = () => openRobot(b.dataset.id);
  }
  for (const r of A.robots) {
    const b = el.querySelector(`[data-id="${r.id}"]`);
    b.classList.toggle("on", sel === r.id);
    const st = b.querySelector(".st");
    st.className = `st ${STATUS_KIND[r.st] || ""}`;
    st.textContent = r.st === "waiting" && r.w ? `hold · ${r.w}` : STATUS_LABEL[r.st] || r.st;
    b.querySelector(".v").textContent = ((r.v * 10) / 1000).toFixed(1);
  }
}

function renderRadio() {
  const el = $("#l3-radio-list", root);
  if (!el) return;
  el.innerHTML = radio.map((m, i) => `<li class="${m.kind} ${i === 0 ? "new" : ""}"><span class="t">${m.time}</span><span>${esc(m.text)}</span></li>`).join("") || `<li class="hint">Listening…</li>`;
}

// ------------------------------------------------------------------ service cases, technicians, pick targets

function upsertCase(c, render = true) {
  serverOffset = Date.now() / 1000 - Number(c.server_now || Date.now() / 1000);
  const old = cases.get(c.id);
  cases.set(c.id, c);
  if (!old || old.status !== c.status) {
    const step = (c.steps || []).at(-1);
    if (old && step) push(c.robot_id, step.by === "copilot" ? "✦" : "●", `${step.title}${step.detail ? ` · ${step.detail}` : ""}`, step.by === "copilot" ? "ai" : c.status === "resolved" ? "ok" : "warn");
  }
  if (render) renderCases();
}

function renderCases() {
  const list = [...cases.values()].sort((a, b) => b.id - a.id).slice(0, 4);
  const box = $("#l3-svc", root);
  box.hidden = !list.length;
  $("#l3-svc-list", root).innerHTML = list.map((c) => {
    const at = CASE_AT[c.status] ?? 0, steps = CASE_STEPS[c.fault] || CASE_STEPS.tire, last = (c.steps || []).at(-1) || {};
    return `<li class="${c.status}" data-robot="${c.robot_id}">
      <div class="row between"><span><b style="color:${robotColor(c.robot_id)}">${c.robot_id}</b> · ${esc(c.component || (c.code || "").split(" ").slice(1).join(" ") || "fault")}</span>
        ${pill(CASE_LABEL[c.status] || c.status, c.status === "resolved" ? "ok" : c.status === "recovering" ? "ai live" : "bad live")}</div>
      <div class="dots5">${steps.map((t, i) => `<i class="${i < at || c.status === "resolved" ? "done" : i === at ? "now" : ""}" title="${t}"></i>`).join("")}</div>
      <div class="hint">${last.by === "copilot" ? "✦ " : ""}${esc(last.title || "")}</div></li>`;
  }).join("");
  for (const li of $$("#l3-svc-list li", root)) li.onclick = () => openRobot(li.dataset.robot);
}

function techs() {
  return [...cases.values()].filter((c) => c.technician && ["dispatched", "repairing", "in_repair"].includes(c.status)).map((c) => ({
    id: c.id, robot: c.robot_id, ...c.technician, task: c.fault === "sensor" ? "recalibrating lidar" : `replacing ${c.component || "tire"}`,
  }));
}

function targets() {
  const out = [];
  for (const r of A.robots) {
    const j = r.job && jobs[r.job];
    if (!j || r.f || !["moving", "waiting", "picking", "scanning", "held"].includes(r.st)) continue;
    const line = j.lines[r.c];
    if (!line) continue;
    const it = item(line.sku), code = it ? it.barcode : "";
    const focus = sel === r.id;
    out.push({ robot: r.id, slot: line.slot, focus,
      html: `<span class="who" style="background:${robotColor(r.id)}">${r.id}</span>${barcodeSVG(code, { h: focus ? 24 : 16, module: focus ? 1.1 : 0.7 })}
        <span class="txt"><b>${esc(it ? it.sku : line.slot)}</b>${focus ? `<span>${esc(it ? it.name : "")}</span>` : ""}<span class="num">${eanText(code)}</span></span>` });
  }
  return out;
}

async function markDone(id) {
  await act(() => api(`/api/service/${id}/done`, { body: {} }), "Marked done: the robot returns to service");
}

// ------------------------------------------------------------------ panel

function applyInset() {
  if (!site) return;
  const cam = site.camera, w = site.host.clientWidth, h = site.host.clientHeight;
  if (inset < 1) { if (cam.view && cam.view.enabled) cam.clearViewOffset(); return; }
  cam.setViewOffset(w, h, inset / 2, 0, w, h);   // keep the followed robot centred in the space left of the panel
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
  panelMode = null; sel = null;
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
  sel = id;
  openPanel("robot");
  site.focus(id);
  copilot.setScope("robot", id);
  setTab("log");
  renderRobot(true);
  renderLog(false);
  if (first && !logFor(id).some((x) => x.backfill)) backfill(id);
}

function openSite() {
  sel = null;
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
      const d = r.type === "dispatch" ? ["✚", `Dispatched ${e.job} (${e.distance_cells} cells away)`, "assign"] : describe(e, id);
      if (d) old.push({ icon: d[0], text: d[1], kind: `${d[2]} old`, time: `t${r.tick}`, t: r.tick, backfill: true });
    }
    list.push(...old.slice(0, 40));
    if (sel === id) renderLog(false);
  } catch { /* live log still fills */ }
}

function renderSiteHead() {
  const f = cfg.site ? cfg.site.facility : null;
  $("#l3p-head", root).innerHTML = `
    <div class="who"><span class="bot-av site">✦</span><div><b>Site copilot</b><div class="hint">${f ? `${esc(f.company)} · ${esc(cfg.site.code)}` : ""}</div></div></div>
    <button class="icon ghost" id="l3p-close" title="Close (Esc)">✕</button>`;
  $("#l3p-close", root).onclick = closePanel;
  const robots = (A ? A.robots : []).map((r) => `<button class="mini" data-id="${r.id}" style="--acc:${robotColor(r.id)}"><span class="sw"></span>${r.id} · ${STATUS_LABEL[r.st] || r.st}</button>`).join("");
  $("#l3p-site", root).innerHTML = `<div class="hint">Ask about traffic, throughput, standoffs, orders or incidents across the floor. Or open a robot:</div><div class="minis">${robots}</div>`;
  for (const b of $$("#l3p-site .mini", root)) b.onclick = () => openRobot(b.dataset.id);
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
        <div><b>${r.id}${asset ? ` · ${esc(asset.model)}` : ""}</b><div class="hint mono">${asset ? `${esc(asset.serial)} · fw ${esc(asset.firmware)} · ${asset.payload_kg} kg payload` : ""}</div></div></div>
      <div class="row"><span id="l3p-st"></span><button class="icon ghost" id="l3p-close" title="Close (Esc)">✕</button></div>
      <div class="following"><span class="live-dot"></span>Following live · the robot keeps working <button class="linkish" id="l3p-askmark">✦ Ask about ${r.id}</button></div>`;
    $("#l3p-close", root).onclick = closePanel;
    $("#l3p-askmark", root).onclick = () => setTab("ask");
  }
  $("#l3p-st", root).innerHTML = pill(STATUS_LABEL[r.st] || r.st, `${STATUS_KIND[r.st] || ""} ${r.st === "moving" ? "live" : ""}`);
  const now = $("#l3p-now", root), txt = nowText(r);
  if (now.dataset.txt !== txt) { now.dataset.txt = txt; now.innerHTML = `<span class="lbl">Now</span><span class="txt">${esc(txt)}</span>`; now.classList.remove("swap"); void now.offsetWidth; now.classList.add("swap"); }
  const s = sensorsOf(r), inState = ((performance.now() - (since.get(r.id) || performance.now())) / 1000).toFixed(1);
  const range = 0.8, unsafe = s.mps > 0.05 && s.stop > range;
  const lidar = s.near && s.near.gap < 3000 ? `${Math.max(0, s.near.gap / 1000).toFixed(2)} m` : "clear";
  const lidarSub = s.near && s.near.gap < 3000 ? `${s.near.what === "pallet" ? "pallet" : `robot ${s.near.what}`} ahead` : "nothing within 3 m";
  renderCase(r);
  renderHealth(r);
  $("#l3p-sensors", root).innerHTML = [
    tile("Speed", `${s.mps.toFixed(2)}<small> m/s</small>`, `limit ${s.limit.toFixed(1)} m/s`, bar(s.mps / 1.2, s.mps > s.limit + 0.01 ? "bad" : "ok", s.limit / 1.2)),
    tile("Heading", `<span class="compass" style="--r:${{ E: 90, S: 180, W: 270, N: 0 }[r.d] || 0}deg">➤</span>${HEADING[r.d] || "–"}`, cname(s.c)),
    tile("Forward sensor", lidar, lidarSub, bar(s.near ? 1 - Math.min(1, Math.max(0, s.near.gap) / 3000) : 0, s.near && s.near.gap < 800 ? "bad" : "info")),
    tile("Stopping distance", `${s.stop.toFixed(2)}<small> m</small>`, `sensor range ${range.toFixed(1)} m`, bar(s.stop / 1.5, unsafe ? "bad" : "ok", range / 1.5)),
    tile("Zone", esc(s.zones.map(zoneName).join(", ") || "open floor"), s.limit < 1.2 ? `speed cap ${s.limit} m/s` : "no speed cap"),
    tile("Claims", (r.r || []).length ? `${(r.r || []).length}<small> cells</small>` : "–", (r.r || []).map(cname).join(" ") || "none held"),
    tile("Odometer", `${((r.o || 0) / 1000).toFixed(1)}<small> m</small>`, "this run"),
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
  el.innerHTML = `<div class="row between"><span class="lbl">Service case #${c.id}${c.fault ? ` · ${c.fault} fault` : ""}</span>
      ${pill(CASE_LABEL[c.status] || c.status, c.status === "resolved" ? "ok" : "bad live")}</div>
    <ol class="cstep">${names.map((n, i) => `<li class="${i < at || c.status === "resolved" ? "done" : i === at ? "now" : ""}"><i></i><span>${n}</span></li>`).join("")}</ol>
    <ul class="ctl">${(c.steps || []).slice().reverse().map((st) => {
      const [who, cls] = BY[st.by] || [st.by, ""];
      return `<li><span class="by ${cls}">${esc(who)}</span><div><b>${esc(st.title || "")}</b>${st.detail ? `<span>${esc(st.detail)}</span>` : ""}
        ${(st.evidence || []).length ? `<em>${st.evidence.map(esc).join(" · ")}</em>` : ""}</div>
        <span class="tm">${new Date(st.at * 1000).toLocaleTimeString([], { hour12: false })}</span></li>`;
    }).join("")}</ul>
    ${t && ["dispatched", "repairing", "in_repair"].includes(c.status) ? `<div class="row between tech-row"><span>👷 <b>${esc(t.name)}</b> · ${esc(t.role)}</span>
      <button class="primary" id="l3p-done">Mark done</button></div>` : ""}`;
  const b = $("#l3p-done", el);
  if (b) b.onclick = () => markDone(c.id);
}

function renderHealth(r) {
  const h = r.h;
  const el = $("#l3p-health", root);
  if (!h) { el.innerHTML = ""; return; }
  const low = (p) => p < 60;
  const tire = (w) => `<span class="tire ${w} ${low(h.tires[w]) ? "bad" : ""}"><b>${h.tires[w]}</b><small>psi</small></span>`;
  const row = (label, v, bad, unit = "") => `<div class="hrow ${bad ? "bad" : ""}"><i></i><span>${label}</span><b>${v}${unit}</b></div>`;
  el.innerHTML = `<div class="row between"><span class="lbl">Health</span><span class="hint">${r.f ? `fault: ${esc(r.f.type)}` : "all nominal"}</span></div>
    <div class="hgrid">
      <div class="chassis">${tire("FL")}${tire("FR")}${tire("RL")}${tire("RR")}<span class="body"><i class="lidar ${h.lidar < 70 ? "bad" : ""}"></i></span></div>
      <div class="hrows">${row("Wheel slip", h.slip, h.slip > 12, "%")}${row("Drive imbalance", h.imbalance, h.imbalance > 20, "%")}
        ${row("Vibration", h.vib, h.vib > 0.4, " g")}${row("Lidar returns", h.lidar, h.lidar < 70, "%")}${row("Lidar range", (h.range / 1000).toFixed(2), h.range < 800, " m")}</div>
    </div>`;
}

const tile = (label, value, sub, extra = "") => `<div class="stile"><span class="lbl">${label}</span><b>${value}</b><span class="sub">${sub || ""}</span>${extra}</div>`;
const bar = (k, kind, mark = null) => `<div class="mbar ${kind}"><i style="width:${Math.round(Math.min(1, Math.max(0, k)) * 100)}%"></i>${mark != null ? `<u style="left:${Math.round(mark * 100)}%"></u>` : ""}</div>`;

function renderLoad(r) {
  const { j, o } = jobInfo(r);
  const carrier = j && cfg.site && cfg.site.carriers.find((c) => c.dock === j.dock);
  const boxes = (r.cs || []).map((sku) => { const it = item(sku); return `<li><span class="box"></span><span>${esc(it ? it.name : sku)}<small class="mono dim">${esc(it ? it.sku : "")} · ${eanText(it ? it.barcode : "")}</small></span>${it ? barcodeSVG(it.barcode, { h: 20, module: 0.8 }) : ""}</li>`; }).join("");
  const el = $("#l3p-load", root);
  const html = `
    <div class="row between"><span class="lbl">On board</span><span class="hint">${j ? `${(r.cs || []).length} of ${j.lines.length}` : (r.cs || []).length ? "held on board · job handed back" : ""}</span></div>
    ${boxes ? `<ul class="cargo">${boxes}</ul>` : `<div class="hint">Empty tray</div>`}
    ${nextPick(r, j)}
    ${j ? `<div class="jobline"><span class="mono">${esc(j.id)}</span>${o ? `<span>${esc(o.id)}</span><b>${esc(o.customer)}</b>${pill(o.tier, TIER[o.tier] || "")}<span>${money(o.value, Number(o.value) < 100 ? 2 : 0)}</span>` : "<span class='hint'>manual job</span>"}</div>
      <div class="hint">picks ${j.lines.map((l) => `${esc(itemName(l.sku))} (${l.slot})`).join(", ")} → ${esc(j.dock.replace("DK", "Dock "))}${carrier ? ` · ${esc(carrier.carrier)}, cutoff ${esc(carrier.cutoff)}` : ""}</div>` : `<div class="hint">No job assigned</div>`}`;
  if (el.dataset.sig !== html) { el.dataset.sig = html; el.innerHTML = html; }
}

function nextPick(r, j) {
  const line = j && j.lines[r.c];
  if (!line || r.f) return "";
  const it = item(line.sku);
  return `<div class="nextpick"><span class="lbl">Next pick</span><div class="np">${it ? barcodeSVG(it.barcode, { h: 26, module: 1 }) : ""}
    <div><b>${esc(it ? it.name : line.sku)}</b><span class="mono">${esc(it ? it.sku : "")} · bin ${esc(line.slot)} · EAN ${eanText(it ? it.barcode : "")}</span></div></div></div>`;
}

function renderLog(fresh) {
  const el = $("#l3p-log", root);
  const list = logFor(sel);
  el.innerHTML = list.slice(0, 80).map((x, i) => `<li class="${x.kind} ${fresh && i === 0 ? "new" : ""}"><span class="ic">${x.icon}</span><span class="tx">${esc(x.text)}</span><span class="tm">${x.time}</span></li>`).join("")
    || `<li class="hint empty">Watching ${esc(sel)}… events appear as they happen.</li>`;
}
