// Floor view: the live fleet, incident injection, robot inspector, jobs.
import { $, $$, esc, api, act, toast, bus, cfg, mps, sparkline, countTo, money, ago, item, itemLabel, pill, TIER } from "../util.js";
import { Floor, robotColor, stopDist } from "../floor.js";
import { live } from "../live.js";

const CHAOS = [
  { key: "pallet_drop", ico: "▣", title: "Pallet falls in an aisle", desc: "Lands just ahead of a fast robot", fires: "pallet_dropped" },
  { key: "mislabel_bin", ico: "⌗", title: "Bin gets mislabeled", desc: "A robot on its way picks the wrong item", fires: "bin_mislabeled" },
  { key: "worker_in_aisle", ico: "⛔", title: "Worker closes an aisle", desc: "A robot is already routed through it", fires: "zone_restricted" },
  { key: "clear_floor", ico: "✦", title: "Clear the floor", desc: "Remove pallets, fix labels, reopen aisles", clear: true },
];

let floor = null, raf = 0, lastUi = 0, current = null, cellZones = null, kpiTimer = null, orders = [];
const armed = new Map();              // chaos key -> button
const robotEvents = new Map();        // robot id -> recent event strings

const KPIS = [
  ["orders_per_hour", "orders / hour", (v) => v],
  ["shipped", "orders shipped", (v) => v],
  ["units_shipped", "units shipped", (v) => v],
  ["value_shipped", "value shipped", (v) => money(v)],
  ["on_time_pct", "picked on time", (v) => (v == null ? "–" : `${v.toFixed(1)}%`)],
  ["open", "orders in progress", (v) => v],
  ["incidents_open", "open incidents", (v) => v],
  ["last_safety_incident", "since last safety incident", (v) => (v ? ago(v) : "none")],
];

function template() {
  return `
  <div class="kpis" id="kpis">${KPIS.map(([k, label]) => `<div class="kpi" data-k="${k}"><b data-v="0">–</b><span>${label}</span></div>`).join("")}</div>
  <div class="floor-layout">
    <div>
      <div class="stage">
        <canvas id="floor-canvas"></canvas>
        <div class="floor-tip" id="floor-tip" hidden></div>
        <div class="overlay-tl"><span class="tag" id="fl-tick">t –</span><span class="tag" id="fl-moving">– moving</span><span class="tag" id="fl-jobs">– jobs</span></div>
        <div class="overlay-tr" id="fl-toggles">
          <button class="toggle on" data-opt="trails">Trails</button>
          <button class="toggle on" data-opt="sensors">Sensors</button>
          <button class="toggle" data-opt="paths">Routes</button>
          <button class="toggle" data-opt="heat">Congestion</button>
        </div>
      </div>
      <div class="legend">
        ${["R1", "R2", "R3", "R4"].map((r) => `<span><i style="background:${robotColor(r)};box-shadow:0 0 8px ${robotColor(r)}"></i>${r}${r === "R4" ? " · heavy lift" : ""}</span>`).join("")}
        <span><i style="background:var(--pallet)"></i>dropped pallet</span>
        <span><i style="background:rgba(255,59,92,.35);border:1px solid var(--bad)"></i>closed aisle</span>
        <span><i style="background:rgba(0,229,255,.35)"></i>sensor range</span>
        <span><i style="background:rgba(255,59,92,.5)"></i>too fast to stop in range</span>
      </div>
    </div>
    <aside>
      <div class="panel">
        <div class="panel-head"><h3>Inject an incident</h3></div>
        <div class="chaos-list" id="chaos-list">
          ${CHAOS.map((c) => `<button class="chaos ${c.clear ? "clear" : ""}" data-s="${c.key}">
            <span class="ico">${c.ico}</span><span><b>${esc(c.title)}</b><span>${esc(c.desc)}</span></span><span class="go">${c.clear ? "Run" : "Inject"}</span></button>`).join("")}
        </div>
      </div>
      <div class="panel">
        <div class="panel-head"><h3>Fleet</h3><span class="hint">click a robot</span></div>
        <div class="robot-list" id="robot-list"></div>
      </div>
      <div class="panel">
        <div class="panel-head"><h3>Order book</h3>
          <label class="hint row"><input type="checkbox" id="auto-jobs"> release orders</label></div>
        <div class="order-list" id="order-list"><div class="skeleton"></div></div>
        <details class="manual"><summary class="hint">Manual pick job</summary>
          <form id="job-form">
            <input id="job-slots" placeholder="slots, e.g. C4, A2" autocomplete="off">
            <select id="job-dock"></select>
            <button type="submit" class="primary">Add</button>
          </form></details>
      </div>
    </aside>
  </div>`;
}

export function mount(root) {
  root.innerHTML = template();
  const map = cfg.map;
  cellZones = {};
  for (const [z, cells] of Object.entries(map.zones)) for (const c of cells) (cellZones[c.join(",")] ||= []).push(z);
  floor = new Floor($("#floor-canvas"), map);
  $("#job-dock").innerHTML = Object.keys(map.docks).map((d) => `<option>${d}</option>`).join("");

  for (const b of $$("#fl-toggles button")) {
    b.onclick = () => { floor.opt[b.dataset.opt] = !floor.opt[b.dataset.opt]; b.classList.toggle("on", floor.opt[b.dataset.opt]); };
  }
  for (const b of $$("#chaos-list button")) {
    b.onclick = async () => {
      const c = CHAOS.find((x) => x.key === b.dataset.s);
      const r = await act(() => api("/api/chaos", { body: { scenario: c.key } }), c.clear ? "Floor cleared" : null);
      if (r && !c.clear) { b.classList.add("armed"); armed.set(c.fires, b); toast(`${c.title}: armed, fires as soon as a robot is in position`, "warn"); }
    };
  }
  $("#floor-canvas").addEventListener("click", (e) => {
    const id = floor.hit(e.clientX, e.clientY);
    if (id) openInspector(id); else closeInspector();
  });
  $("#floor-canvas").addEventListener("mousemove", binTip);
  $("#floor-canvas").addEventListener("mouseleave", () => { $("#floor-tip").hidden = true; });
  $("#auto-jobs").onchange = (e) => act(() => api("/api/jobs/auto", { body: { enabled: e.target.checked } }));
  $("#job-form").onsubmit = (e) => {
    e.preventDefault();
    const slots = $("#job-slots").value.split(/[\s,]+/).filter(Boolean).map((s) => s.toUpperCase());
    act(() => api("/api/jobs", { body: { slots, dock: $("#job-dock").value } }), (j) => `Job ${j.id} queued`)
      .then((j) => { if (j) $("#job-slots").value = ""; });
  };
  bus.on("state", renderJobs);
  window.addEventListener("resize", () => floor && floor.layout());
}

function binTip(e) {
  const tip = $("#floor-tip"), c = $("#floor-canvas"), rect = c.getBoundingClientRect();
  if (!floor.cell) return;
  const x = Math.floor((e.clientX - rect.left) / floor.cell), y = Math.floor((e.clientY - rect.top) / floor.cell);
  const slot = floor.slotAt[`${x},${y}`], dock = floor.dockAt[`${x},${y}`];
  const it = slot && item(slot);
  const carrier = dock && cfg.site && cfg.site.carriers.find((k) => k.dock === dock);
  if (!it && !carrier) { tip.hidden = true; return; }
  tip.hidden = false;
  tip.style.left = `${e.clientX - rect.left + 14}px`; tip.style.top = `${e.clientY - rect.top + 12}px`;
  tip.innerHTML = it ? `<b>${esc(slot)}</b> <span class="mono">${esc(it.sku)}</span><br>${esc(it.name)}<br>
      <span class="hint">${esc(it.category)} · ${esc(it.cls.replace("_", " "))} · ${money(it.unit_value, 2)} / ${esc(it.uom)} · ${it.unit_weight} kg</span>`
    : `<b>${esc(dock)}</b> dock door<br>${esc(carrier.carrier)} · ${esc(carrier.service)}<br><span class="hint">cutoff ${esc(carrier.cutoff)}</span>`;
}

async function refreshKpis() {
  try {
    const [k, o] = await Promise.all([api("/api/kpis"), api("/api/orders?limit=14")]);
    for (const [key, , fmt] of KPIS) {
      const el = $(`#kpis [data-k="${key}"] b`);
      if (!el) continue;
      const v = k[key];
      if (typeof v === "number" && !["on_time_pct", "value_shipped"].includes(key)) countTo(el, v, { ms: 700 });
      else el.textContent = fmt(v);
      if (key === "value_shipped" && typeof v === "number") countTo(el, v, { ms: 900, fmt: (x) => money(x) });
    }
    $(`#kpis [data-k="incidents_open"]`).classList.toggle("alert", k.incidents_open > 0);
    orders = o;
    renderOrders();
  } catch { /* next poll */ }
}

const OSTAT = { released: ["released", ""], picking: ["picking", "info live"], shipped: ["shipped", "ok"],
  short_shipped: ["wrong item shipped", "bad"], exception: ["exception", "warn"] };

function renderOrders() {
  const el = $("#order-list");
  if (!el) return;
  el.innerHTML = orders.length ? orders.map((o) => `
    <div class="order ${o.status}">
      <div class="row between"><span class="mono">${esc(o.id)}</span>${pill(...(OSTAT[o.status] || [o.status, ""]))}</div>
      <div class="row between"><span>${esc(o.customer)} ${o.tier !== "standard" ? pill(o.tier, TIER[o.tier]) : ""}${o.priority === "expedite" ? " " + pill("expedite", "warn") : ""}</span><b>${money(o.value)}</b></div>
      <div class="lines">${o.lines.map((l) => `${l.qty} × ${esc(l.name)} <span class="dim">${esc(l.slot)}</span>`).join("<br>")}</div>
      <div class="hint">${esc(o.carrier)} → ${esc(o.dock)}${o.job_id ? ` · job ${esc(o.job_id)}` : ""}</div>
    </div>`).join("") : `<div class="empty">No orders yet: the site is being provisioned.</div>`;
}

export function show() {
  clearInterval(kpiTimer);
  refreshKpis();
  kpiTimer = setInterval(refreshKpis, 3000);
  cancelAnimationFrame(raf);
  const loop = (now) => {
    const s = live.advance(now);
    if (s && s.a) {
      for (const f of s.crossed) {
        floor.accumulate(f);
        for (const e of f.ev || []) {
          floor.trigger(e, f, now);
          if (armed.has(e.type)) { armed.get(e.type).classList.remove("armed"); armed.delete(e.type); }
          if (e.robot) {
            const list = robotEvents.get(e.robot) || [];
            list.unshift(`t${f.t} ${e.type}${e.slot ? ` ${e.slot}` : ""}${e.job ? ` ${e.job}` : ""}`);
            robotEvents.set(e.robot, list.slice(0, 12));
          }
        }
      }
      current = s.a;
      floor.draw(s.a, s.b, s.alpha, now);
      if (now - lastUi > 250) { lastUi = now; renderSide(s.a); }
    } else {
      floor.draw(null, null, 0, now, { empty: "waiting for the live sim…" });
    }
    raf = requestAnimationFrame(loop);
  };
  raf = requestAnimationFrame(loop);
}

export function hide() { cancelAnimationFrame(raf); clearInterval(kpiTimer); closeInspector(); }

function renderSide(f) {
  $("#fl-tick").innerHTML = `t <b>${f.t}</b> · ${(f.t / cfg.tickHz).toFixed(0)}s`;
  $("#fl-moving").innerHTML = `<b>${f.robots.filter((r) => r.st === "moving").length}</b> moving`;
  $("#robot-list").innerHTML = f.robots.map((r) => `
    <div class="robot-row ${floor.selected === r.id ? "sel" : ""}" data-id="${r.id}">
      <span class="av" style="background:${robotColor(r.id)};box-shadow:0 0 12px ${robotColor(r.id)}66">${r.id}</span>
      <div><div class="st ${r.st}">${esc(r.st)}</div><div class="meta">${esc(jobLabel(r.job))}${r.c ? " · carrying" : ""}</div></div>
      <div style="text-align:right"><div class="mono">${mps(r.v)} m/s</div>
        <div class="speedbar"><i style="width:${Math.min(100, r.v / 120 * 100)}%;background:${robotColor(r.id)}"></i></div></div>
    </div>`).join("");
  for (const el of $$("#robot-list .robot-row")) el.onclick = () => openInspector(el.dataset.id);
  if (floor.selected) renderInspector();
}

let jobsById = {};
function jobLabel(jid) {
  if (!jid) return "no job";
  const j = jobsById[jid], o = j && j.order_id && orders.find((x) => x.id === j.order_id);
  return o ? `${jid} · ${o.id} · ${o.customer}` : jid;
}

function renderJobs(st) {
  if (!$("#order-list")) return;
  $("#auto-jobs").checked = st.auto_jobs;
  jobsById = Object.fromEntries(st.jobs.map((j) => [j.id, j]));
  $("#fl-jobs").innerHTML = `<b>${st.jobs.length}</b> open jobs`;
}

// ------------------------------------------------------------------ inspector
function openInspector(id) {
  floor.selected = id;
  $("#inspector").classList.add("open");
  renderInspector();
}

function closeInspector() {
  if (floor) floor.selected = null;
  $("#inspector").classList.remove("open");
}

function renderInspector() {
  const id = floor.selected;
  const r = current && current.robots.find((x) => x.id === id);
  if (!r) return;
  const el = $("#inspector");
  if (el.dataset.robot !== id) {
    el.dataset.robot = id;
    el.innerHTML = `
      <button class="close icon" id="insp-close">✕</button>
      <div class="who"><span class="av" style="background:${robotColor(id)};box-shadow:0 0 14px ${robotColor(id)}">${id}</span>
        <div><b>${id}</b> <span class="hint">${id === "R4" ? "heavy lift" : "standard"}</span></div></div>
      <div class="big" id="insp-speed">0.0 <small>m/s</small></div>
      <canvas id="insp-spark"></canvas>
      <div class="kv" id="insp-kv"></div>
      <div class="kv asset" id="insp-asset"></div>
      <h3 style="margin-bottom:6px">Recent events</h3>
      <div class="evs" id="insp-evs"></div>`;
    $("#insp-close").onclick = closeInspector;
  }
  const cell = [Math.floor(r.x / cfg.map.cell_mm), Math.floor(r.y / cfg.map.cell_mm)];
  const zones = (cellZones[cell.join(",")] || []).filter((z) => z !== "racks").join(", ") || "open floor";
  const stop = stopDist(r.v) + 100;
  $("#insp-speed").innerHTML = `${mps(r.v)} <small>m/s</small>`;
  $("#insp-kv").innerHTML = `
    <span>status</span><span class="st ${r.st}">${esc(r.st)}</span>
    <span>job</span><span class="mono">${esc(jobLabel(r.job))}</span>
    <span>carrying</span><span>${r.c ? `${r.c} item${r.c > 1 ? "s" : ""}` : "nothing"}</span>
    <span>where</span><span class="mono">c${cell[0]}_${cell[1]} · ${esc(zones)}</span>
    <span>stopping</span><span style="color:${stop > 800 ? "var(--bad)" : "var(--ok)"}">${(stop / 1000).toFixed(2)} m of 0.80 m sensed</span>
    <span>route</span><span class="mono">${r.p && r.p.length ? `${r.p.length}+ cells` : "–"}</span>`;
  const a = cfg.site && cfg.site.robots.find((x) => x.id === id);
  $("#insp-asset").innerHTML = a ? `
    <span>model</span><span>${esc(a.model)}</span><span>serial</span><span class="mono">${esc(a.serial)}</span>
    <span>firmware</span><span class="mono">${esc(a.firmware)}</span><span>in service</span><span>${esc(a.commissioned)}</span>
    <span>payload</span><span>${a.payload_kg} kg</span>` : "";
  sparkline($("#insp-spark"), live.robotHistory.get(id) || [], { color: robotColor(id), max: 120 });
  $("#insp-evs").innerHTML = (robotEvents.get(id) || []).map((s) => `<div>${esc(s)}</div>`).join("") || `<div class="dim">none yet</div>`;
}

export function onKey(e) {
  if (e.key === "Escape") closeInspector();
  if (/^[1-4]$/.test(e.key) && e.altKey) openInspector(`R${e.key}`);
}

