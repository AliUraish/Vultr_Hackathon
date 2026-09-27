// Shared helpers: DOM, API, toasts, event bus, formatting.

export const $ = (s, el = document) => el.querySelector(s);
export const $$ = (s, el = document) => [...el.querySelectorAll(s)];
export const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

export const cfg = { tickHz: 10, map: null, site: null };

// The enterprise profile (catalog, customers, carriers, assets); null until the site is provisioned.
export const item = (slotOrSku) => {
  const cat = cfg.site && cfg.site.catalog;
  if (!cat) return null;
  const slot = String(slotOrSku || "").replace(/^SKU-/, "");
  return cat[slot] || null;
};
export const itemLabel = (slotOrSku) => { const it = item(slotOrSku); return it ? `${it.name} (${it.sku})` : String(slotOrSku || ""); };
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

export const mps = (v) => (v * cfg.tickHz / 1000).toFixed(1);
export const secs = (t) => (t / cfg.tickHz).toFixed(1);
export const short = (h) => (h ? h.slice(0, 10) : "–");
export const pill = (text, kind = "") => `<span class="pill ${kind}">${esc(text)}</span>`;

export const STATUS = {
  open: ["capturing", "info live"], reproducing: ["replaying", "info live"], diagnosing: ["investigating", "info live"],
  trials: ["testing fixes", "info live"], awaiting_approval: ["needs approval", "warn"], fixed: ["fixed", "ok"],
  no_fix: ["no fix found", "bad"], not_reproducible: ["not reproducible", "bad"], dismissed: ["dismissed", ""], lost: ["lost", "bad"],
};
export const statusPill = (s) => pill(...(STATUS[s] || [s, ""]));
export const FAILURE_LABEL = { collision: "Collision", stall: "Stall", task_overdue: "Task overdue", wrong_item: "Wrong item", zone_breach: "Zone breach" };
export const SCENARIO_LABEL = { pallet_drop: "pallet fell in an aisle", mislabel_bin: "mislabeled bin", worker_in_aisle: "worker closed an aisle" };
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
    case "replay.queued": return `replay #${p.replay} ${p.kind}${p.rules ? ` with [${p.rules.join(", ")}]` : " with recorded policy"}`;
    case "replay.done": return `replay #${p.replay} ${p.kind} → ${p.outcome || p.status}${p.matches_live ? " · trajectory hash = live" : ""} · ${p.duration_ms} ms on ${p.worker}`;
    case "diagnosis": return `${p.source}: ${(p.hypotheses || []).map((h) => h.fix).join(" | ")}`;
    case "hypothesis.status": return `${p.fix}: ${p.from} → ${p.to}`;
    case "approval": return `${p.by} approved ${p.fix} → policy v${p.policy_version}`;
    case "policy.version": return `v${p.version}: ${(p.rules || []).join(", ") || "base policy"}`;
    case "dispatch": return `${p.job} → ${p.robot} (${p.distance_cells} cells away)`;
    case "investigation.step": return `step ${p.step} ${p.tool}: ${p.why || ""}`;
    case "experiment.done": return `experiment #${p.experiment} ${p.kind}: ${p.summary || ""}`;
    case "job.created": return `${p.job}${p.order ? ` for ${p.order} · ${p.customer} · $${p.value}` : ""}: ${(p.lines || []).map((l) => itemLabel(l.slot)).join(", ")} → ${p.dock}`;
    case "site.created": return `${p.company} · ${p.name} (${p.code}), ${p.skus} SKUs, ${p.customers} customers, profile by ${p.source}`;
    case "audit.verified": return `${p.by}: ${p.ok ? "intact" : `${p.problem_count} problem(s)`} · ${p.blocks} blocks · ${p.events} events · ${p.ms} ms`;
    case "investigation.done": return `${p.source}: ${(p.fixes || []).join(", ") || "no fix"} · ${p.steps} steps · ${p.simulations} sims`;
    default: return JSON.stringify(p).slice(0, 200);
  }
}

// EAN-13 barcode as inline SVG (real encoding: L/G/R patterns, parity from the first digit).
const EAN_L = ["0001101", "0011001", "0010011", "0111101", "0100011", "0110001", "0101111", "0111011", "0110111", "0001011"];
const EAN_G = ["0100111", "0110011", "0011011", "0100001", "0011101", "0111001", "0000101", "0010001", "0001001", "0010111"];
const EAN_R = ["1110010", "1100110", "1101100", "1000010", "1011100", "1001110", "1010000", "1000100", "1001000", "1110100"];
const EAN_P = ["LLLLLL", "LLGLGG", "LLGGLG", "LLGGGL", "LGLLGG", "LGGLLG", "LGGGLL", "LGLGLG", "LGLGGL", "LGGLGL"];
export function barcodeSVG(code, { h = 26, module = 1.3, color = "#111" } = {}) {
  if (!/^\d{13}$/.test(code || "")) return "";
  const d = code.split("").map(Number), par = EAN_P[d[0]];
  let bits = "101";
  for (let i = 1; i <= 6; i++) bits += (par[i - 1] === "L" ? EAN_L : EAN_G)[d[i]];
  bits += "01010";
  for (let i = 7; i <= 12; i++) bits += EAN_R[d[i]];
  bits += "101";
  const guards = new Set([0, 1, 2, 45, 46, 47, 48, 49, 92, 93, 94]);
  let x = 0, rects = "";
  for (let i = 0; i < bits.length; i++) {
    if (bits[i] === "1") rects += `<rect x="${(x + 7) * module}" y="0" width="${module}" height="${guards.has(i) ? h : h - 5}"/>`;
    x++;
  }
  const w = (95 + 14) * module;
  return `<svg class="ean" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" fill="${color}" aria-label="EAN-13 ${code}"><rect width="${w}" height="${h}" fill="#fff"/>${rects}</svg>`;
}
export const eanText = (c) => (c ? `${c[0]} ${c.slice(1, 7)} ${c.slice(7)}` : "");
