// Shared helpers: DOM, API, toasts, event bus, formatting.

export const $ = (s, el = document) => el.querySelector(s);
export const $$ = (s, el = document) => [...el.querySelectorAll(s)];
export const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

export const cfg = { tickHz: 10, map: null, site: null };

// The mine profile (materials per dig face, excavators, destinations, trucks, crew); null until provisioned.
export const item = (slotOrSku) => {
  const cat = cfg.site && cfg.site.catalog;
  if (!cat) return null;
  const slot = String(slotOrSku || "").replace(/^(SKU|MAT)-/, "");
  return cat[slot] || null;
};
export const itemLabel = (slotOrSku) => { const it = item(slotOrSku); return it ? `${it.name} (${it.sku})` : String(slotOrSku || ""); };
export const excavator = (slot) => ((cfg.site && cfg.site.excavators) || []).find((e) => e.slot === slot) || { id: `Face ${slot}`, slot };
export const faceName = (slot) => `${excavator(slot).id} · face ${slot}`;
export const destination = (dock) => ((cfg.site && cfg.site.carriers) || []).find((c) => c.dock === dock)?.carrier || { DK1: "Primary crusher", DK2: "ROM stockpile", DK3: "Waste dump", DK4: "Sand stockpile" }[dock] || dock;
export const MATERIAL_COLOR = { ore: 0x8c5a3a, lowgrade: 0x9b7b58, waste: 0x6f6a62, sand: 0xd4b27a };
export const zoneLabel = (z) => ({ crest_road: "crest haul road", middle_road: "middle haul road", pit_floor: "pit floor road",
  ramp_west: "west ramp", ramp_east: "east ramp", bench_upper_w: "upper bench west", bench_upper_e: "upper bench east",
  bench_lower_w: "lower bench west", bench_lower_e: "lower bench east", cuts: "one-lane cut", park: "truck park",
  workshop: "workshop", dump_area: "tipping area", benches: "bench roads", haul_roads: "haul roads", ramps: "ramps",
  bench_upper: "upper bench", bench_lower: "lower bench" }[z] || String(z || "").replace(/_/g, " "));
export const money = (v, digits = 0) => (v == null ? "–" : `$${Number(v).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })}`);
export const ago = (ts) => {
  if (!ts) return "–";
  const s = Math.max(0, (Date.now() - Date.parse(ts)) / 1000);
  return s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`;
};
export const TIER = { platinum: "info", gold: "warn", standard: "" };

export function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

// Tiny pub/sub so views react to live updates without knowing about the socket.
const handlers = new Map();
export const bus = {
  on(type, fn) { if (!handlers.has(type)) handlers.set(type, new Set()); handlers.get(type).add(fn); return () => handlers.get(type).delete(fn); },
  emit(type, data) { for (const fn of handlers.get(type) || []) { try { fn(data); } catch (e) { console.error(e); } } },
};

export async function api(path, opts = {}) {
  const init = { credentials: "same-origin", method: opts.method || (opts.body !== undefined ? "POST" : "GET"), headers: {} };
  if (opts.body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.body); }
  const res = await fetch(path, init);
  if (res.status === 401 && path !== "/api/login") { bus.emit("auth", null); throw new Error("sign in required"); }
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try { const j = await res.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch { /* keep */ }
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

export function toast(msg, kind = "", ms = 5500) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $("#toasts").append(el);
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 320); }, ms);
}

export async function act(fn, ok) {
  try { const r = await fn(); if (ok) toast(typeof ok === "function" ? ok(r) : ok, "ok"); return r; }
  catch (e) { toast(e.message, "bad"); return null; }
}

export const kmh = (v) => (v * cfg.tickHz * 3.6 / 1000);
export const mps = (v) => kmh(v).toFixed(0);   // shown as km/h across the app
export const secs = (t) => (t / cfg.tickHz).toFixed(1);
export const short = (h) => (h ? h.slice(0, 10) : "–");
export const pill = (text, kind = "") => `<span class="pill ${kind}">${esc(text)}</span>`;

export const STATUS = {
  open: ["recording", "info live"], reproducing: ["replaying", "info live"], diagnosing: ["AI investigating", "info live"],
  trials: ["testing fixes", "info live"], awaiting_approval: ["needs approval", "warn"], fixed: ["fixed", "ok"],
  no_fix: ["no fix found", "bad"], not_reproducible: ["not reproducible", "bad"], dismissed: ["dismissed", ""], lost: ["lost", "bad"],
};
export const statusPill = (s) => pill(...(STATUS[s] || [s, ""]));
export const FAILURE_LABEL = { collision: "Collision", stall: "Truck stalled", task_overdue: "Late load", wrong_item: "Wrong material", zone_breach: "Blast zone breach" };
export const SCENARIO_LABEL = { rockfall: "rock fell off a highwall", grade_mixup: "dig face grade mix-up", blast_closure: "road closed for blasting",
  road_damage: "pothole opened", tire_fault: "tyre failure", sensor_fault: "lidar fault" };
export const OUTCOME = { avoided: "ok", reproduced: "bad", regressed: "warn" };
export const outcomePill = (o) => pill(o || "running…", o ? OUTCOME[o] || "" : "info live");

// Count a number up to its new value.
export function countTo(el, to, { ms = 600, fmt = (v) => Math.round(v).toString() } = {}) {
  const from = Number(el.dataset.v ?? 0);
  el.dataset.v = to;
  if (from === to) { el.textContent = fmt(to); return; }
  const t0 = performance.now();
  const step = (now) => {
    const k = Math.min(1, (now - t0) / ms), e = 1 - (1 - k) ** 3;
    el.textContent = fmt(from + (to - from) * e);
    if (k < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

export function sparkline(canvas, values, { color = cssVar("--accent"), max = null, fill = true } = {}) {
  const dpr = window.devicePixelRatio || 1, w = canvas.clientWidth, h = canvas.clientHeight;
  if (canvas.width !== w * dpr) { canvas.width = w * dpr; canvas.height = h * dpr; }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  if (values.length < 2) return;
  const top = max ?? Math.max(...values, 1);
  const x = (i) => (i / (values.length - 1)) * w, y = (v) => h - 4 - (v / top) * (h - 10);
  ctx.beginPath();
  values.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
  ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.shadowColor = color; ctx.shadowBlur = 8; ctx.stroke(); ctx.shadowBlur = 0;
  if (fill) {
    ctx.lineTo(w, h); ctx.lineTo(0, h); ctx.closePath();
    const g = ctx.createLinearGradient(0, 0, 0, h); g.addColorStop(0, color + "55"); g.addColorStop(1, color + "00");
    ctx.fillStyle = g; ctx.fill();
  }
}

export function summarize(e) {
  const p = e.payload || {};
  switch (e.type) {
    case "failure": return `${p.type} · ${p.robot} at t${p.tick} ${p.detail ? JSON.stringify(p.detail) : ""}`;
    case "failure.status": return `${p.from} → ${p.to}${p.note ? ` · ${p.note}` : ""}`;
    case "capsule.cut": return `capsule #${p.capsule} ticks ${p.start}–${p.end} from snapshot t${p.snapshot_tick}, ${Math.round(p.bytes / 1024)} KB, sha256 ${short(p.hash)}`;
    case "ai.dispatch": return `✦ ${p.robot} → ${faceName(p.face)} (${p.road_m} m): ${p.reason || ""}`;
    case "ai.traffic": case "traffic.rule": return `${e.type === "ai.traffic" ? "✦ " : ""}${p.first} goes first, ${p.yield} ${p.how === "reroute" ? "re-routes" : "pulls aside"}: ${p.reason || ""}`;
    case "service.alert": return `paged the crew: ${p.message}`;
    case "service.step": return `${p.robot} ${p.status}: ${p.title || ""}${p.by ? ` (${p.by})` : ""}`;
    case "sim.pothole_detected": return `${p.robot}'s lidar found pothole ${p.id} at c${p.cell[0]}_${p.cell[1]} (${(p.depth / 1000).toFixed(1)} m deep, ${Math.round(p.dist / 1000)} m out)`;
    case "sim.yield": return `${p.robot} yields to ${p.to} (${p.rule === "ai" ? "traffic AI" : p.rule})${p.reason ? `: ${p.reason}` : ""}`;
    case "replay.queued": return `replay #${p.replay} ${p.kind}${p.rules ? ` with [${p.rules.join(", ")}]` : " with recorded policy"}`;
    case "replay.done": return `replay #${p.replay} ${p.kind} → ${p.outcome || p.status}${p.matches_live ? " · trajectory hash = live" : ""} · ${p.duration_ms} ms on ${p.worker}`;
    case "diagnosis": return `${p.source}: ${(p.hypotheses || []).map((h) => h.fix).join(" | ")}`;
    case "hypothesis.status": return `${p.fix}: ${p.from} → ${p.to}`;
    case "approval": return `${p.by} approved ${p.fix} → policy v${p.policy_version}`;
    case "policy.version": return `v${p.version}: ${(p.rules || []).join(", ") || "base policy"}`;
    case "dispatch": return `${p.job} → ${p.robot} (${p.road_m ?? "?"} m, rules)`;
    case "investigation.step": return `step ${p.step} ${p.tool}: ${p.why || ""}`;
    case "experiment.done": return `experiment #${p.experiment} ${p.kind}: ${p.summary || ""}`;
    case "job.created": return `${p.job}${p.order ? ` ${p.order} · ${p.tonnes ?? ""} t for ${p.customer} · $${Number(p.value).toLocaleString()}` : ""}: ${(p.lines || []).map((l) => faceName(l.slot)).join(", ")} → ${destination(p.dock)}`;
    case "site.created": return `${p.company} · ${p.name} (${p.code}), ${p.commodity || ""}, ${p.faces} dig faces, profile by ${p.source}`;
    case "audit.verified": return `${p.by}: ${p.ok ? "intact" : `${p.problem_count} problem(s)`} · ${p.blocks} blocks · ${p.events} events · ${p.ms} ms`;
    case "investigation.done": return `${p.source}: ${(p.fixes || []).join(", ") || "no fix"} · ${p.steps} steps · ${p.simulations} sims`;
    default: return JSON.stringify(p).slice(0, 200);
  }
}
