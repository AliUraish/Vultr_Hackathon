"use strict";
// Replay fleet-ops web app. No build step: plain DOM + canvas, talks to the control plane on VM A.

const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const S = {
  map: null, tickHz: 10, slotAt: {}, dockAt: {},
  view: null, frameQ: [], lastFrame: null, state: null,
  fv: null,              // failure view state
  log: { before: null, timer: null },
};

// ------------------------------------------------------------------ api + ui helpers

async function api(path, opts = {}) {
  const init = { credentials: "same-origin", method: opts.method || (opts.body ? "POST" : "GET"), headers: {} };
  if (opts.body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.body); }
  const res = await fetch(path, init);
  if (res.status === 401) { showLogin(); throw new Error("sign in required"); }
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try { const j = await res.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch { /* keep status text */ }
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), 6000);
}

async function act(fn, ok) {
  try { const r = await fn(); if (ok) toast(typeof ok === "function" ? ok(r) : ok, "ok"); return r; }
  catch (e) { toast(e.message, "bad"); return null; }
}

function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
const mps = (v) => (v * S.tickHz / 1000).toFixed(1);
const secs = (ticks) => (ticks / S.tickHz).toFixed(1);
const short = (h) => (h ? h.slice(0, 10) : "–");

const STATUS = {
  open: ["capturing", "info"], reproducing: ["replaying", "info"], diagnosing: ["diagnosing", "info"],
  trials: ["testing fixes", "info"], awaiting_approval: ["needs approval", "warn"], fixed: ["fixed", "ok"],
  no_fix: ["no fix found", "bad"], not_reproducible: ["not reproducible", "bad"], dismissed: ["dismissed", ""],
  lost: ["lost", "bad"],
};
const pill = (text, kind = "") => `<span class="pill ${kind}">${esc(text)}</span>`;
const statusPill = (s) => pill(...(STATUS[s] || [s, ""]));
const OUTCOME = { avoided: "ok", reproduced: "bad", regressed: "warn" };
const outcomePill = (o) => pill(o || "running…", OUTCOME[o] || "info");
const SCENARIO_LABEL = { pallet_drop: "pallet fell in an aisle", mislabel_bin: "mislabeled bin", worker_in_aisle: "worker closed an aisle" };
const FAILURE_LABEL = { collision: "Collision", stall: "Stall", task_overdue: "Task overdue", wrong_item: "Wrong item", zone_breach: "Zone breach" };

// ------------------------------------------------------------------ floor rendering

const ROBOT_COLOR = () => ({ R1: css("--r1"), R2: css("--r2"), R3: css("--r3"), R4: css("--r4") });

function sizeCanvas(canvas) {
  const m = S.map, dpr = window.devicePixelRatio || 1;
  const w = Math.max(canvas.clientWidth, 200), cell = w / m.width, h = cell * m.height;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr); canvas.style.height = `${h}px`;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, cell, w, h };
}

function drawFloor(canvas, frame, opts = {}) {
  if (!S.map) return;
  const m = S.map, { ctx, cell, w, h } = sizeCanvas(canvas), mm = cell / m.cell_mm;
  const col = { wall: css("--wall"), rack: css("--rack"), edge: css("--rack-edge"), dock: css("--dock"),
    home: css("--home"), floor: css("--floor"), grid: css("--grid"), text: css("--muted") };
  ctx.clearRect(0, 0, w, h);
  for (let y = 0; y < m.height; y++) {
    for (let x = 0; x < m.width; x++) {
      const ch = m.layout[y][x];
      ctx.fillStyle = ch === "#" ? col.wall : /[A-F]/.test(ch) ? col.rack : /\d/.test(ch) ? col.dock : ch === "H" ? col.home : col.floor;
      ctx.fillRect(x * cell, y * cell, cell, cell);
      ctx.strokeStyle = /[A-F]/.test(ch) ? col.edge : col.grid;
      ctx.lineWidth = 1;
      ctx.strokeRect(x * cell + 0.5, y * cell + 0.5, cell - 1, cell - 1);
    }
  }
  if (cell >= 20) {
    ctx.fillStyle = col.text; ctx.font = `${Math.max(8, cell * 0.28)}px ui-monospace, monospace`;
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    for (const [k, id] of Object.entries(S.slotAt)) { const [x, y] = k.split(",").map(Number); ctx.fillText(id, (x + .5) * cell, (y + .5) * cell); }
    ctx.fillStyle = css("--accent");
    for (const [k, id] of Object.entries(S.dockAt)) { const [x, y] = k.split(",").map(Number); ctx.fillText(id, (x + .5) * cell, (y + .5) * cell); }
  }
  if (!frame) {
    ctx.fillStyle = col.text; ctx.textAlign = "center"; ctx.font = "14px system-ui";
    ctx.fillText(opts.empty || "no data", w / 2, h / 2);
    return;
  }
  // closed aisles with a worker in them
  for (const z of frame.zones || []) {
    const cells = m.zones[z] || [];
    ctx.fillStyle = css("--zone");
    for (const [x, y] of cells) ctx.fillRect(x * cell, y * cell, cell, cell);
    if (cells.length) {
      const [x, y] = cells[Math.floor(cells.length / 2)];
      drawWorker(ctx, (x + .5) * cell, (y + .5) * cell, cell);
    }
  }
  // dropped pallets
  for (const [x, y] of frame.pallets || []) {
    const s = cell * 0.8, px = (x + .5) * cell - s / 2, py = (y + .5) * cell - s / 2;
    ctx.fillStyle = css("--pallet"); ctx.fillRect(px, py, s, s);
    ctx.strokeStyle = "rgba(0,0,0,.45)"; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(px, py); ctx.lineTo(px + s, py + s); ctx.moveTo(px + s, py); ctx.lineTo(px, py + s); ctx.stroke();
  }
  const colors = ROBOT_COLOR();
  // planned paths
  if (opts.paths) {
    ctx.setLineDash([4, 4]); ctx.lineWidth = 1.5;
    for (const r of frame.robots) {
      if (!r.p || !r.p.length) continue;
      ctx.strokeStyle = colors[r.id] + "99"; ctx.beginPath(); ctx.moveTo(r.x * mm, r.y * mm);
      for (const [x, y] of r.p) ctx.lineTo((x + .5) * cell, (y + .5) * cell);
      ctx.stroke();
    }
    ctx.setLineDash([]);
  }
  // robots
  const hits = new Set((frame.ev || []).filter((e) => e.type === "contact" || e.type === "struck").map((e) => e.robot));
  for (const r of frame.robots) {
    const cx = r.x * mm, cy = r.y * mm, s = cell * 0.6;
    if (hits.has(r.id)) {
      ctx.fillStyle = "rgba(248,81,73,.35)"; ctx.beginPath(); ctx.arc(cx, cy, cell * 0.9, 0, Math.PI * 2); ctx.fill();
    }
    ctx.fillStyle = colors[r.id] || "#ccc";
    roundRect(ctx, cx - s / 2, cy - s / 2, s, s, s * 0.25); ctx.fill();
    const ring = r.st === "estop" ? css("--bad") : (r.st === "waiting" || r.st === "blocked" || r.st === "held") ? css("--warn") : null;
    if (ring) { ctx.strokeStyle = ring; ctx.lineWidth = 3; roundRect(ctx, cx - s / 2 - 2, cy - s / 2 - 2, s + 4, s + 4, s * 0.3); ctx.stroke(); }
    const d = { E: [1, 0], W: [-1, 0], S: [0, 1], N: [0, -1] }[r.d];
    if (d) {
      ctx.fillStyle = "rgba(0,0,0,.55)"; ctx.beginPath();
      const tx = cx + d[0] * s * 0.48, ty = cy + d[1] * s * 0.48, pxv = -d[1] * s * 0.18, pyv = d[0] * s * 0.18;
      ctx.moveTo(tx, ty); ctx.lineTo(cx + d[0] * s * 0.2 + pxv, cy + d[1] * s * 0.2 + pyv);
      ctx.lineTo(cx + d[0] * s * 0.2 - pxv, cy + d[1] * s * 0.2 - pyv); ctx.fill();
    }
    if (r.c) { ctx.fillStyle = "#fff"; ctx.fillRect(cx + s * 0.18, cy - s * 0.48, s * 0.28, s * 0.28); }
    ctx.fillStyle = "#08121a"; ctx.font = `bold ${Math.max(9, s * 0.4)}px system-ui`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText(r.id.slice(1), cx, cy + 1);
  }
  if (opts.label) {
    ctx.fillStyle = "rgba(13,17,23,.75)"; ctx.fillRect(6, 6, ctx.measureText(opts.label).width + 16, 22);
    ctx.fillStyle = css("--text"); ctx.font = "12px ui-monospace, monospace"; ctx.textAlign = "left"; ctx.textBaseline = "middle";
    ctx.fillText(opts.label, 14, 17);
  }
}

function drawWorker(ctx, x, y, cell) {
  ctx.strokeStyle = css("--bad"); ctx.fillStyle = css("--bad"); ctx.lineWidth = 2;
  ctx.beginPath(); ctx.arc(x, y - cell * 0.22, cell * 0.12, 0, Math.PI * 2); ctx.fill();
  ctx.beginPath(); ctx.moveTo(x, y - cell * 0.1); ctx.lineTo(x, y + cell * 0.18);
  ctx.moveTo(x - cell * 0.18, y); ctx.lineTo(x + cell * 0.18, y);
  ctx.moveTo(x, y + cell * 0.18); ctx.lineTo(x - cell * 0.14, y + cell * 0.38);
  ctx.moveTo(x, y + cell * 0.18); ctx.lineTo(x + cell * 0.14, y + cell * 0.38); ctx.stroke();
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
}

// ------------------------------------------------------------------ live floor

let floorTick = 0;
setInterval(() => {
  if (!S.frameQ.length) return;
  if (S.frameQ.length > 6) S.frameQ.splice(0, S.frameQ.length - 2);  // fell behind: jump ahead
  const f = S.frameQ.shift();
  S.lastFrame = f;
  $("#tick-chip").textContent = `t ${f.t} · ${secs(f.t)}s`;
  if (S.view === "floor") {
    drawFloor($("#floor-canvas"), f, { paths: true });
    if (++floorTick % 5 === 0) renderRobots(f);
  }
}, 100);

function renderRobots(f) {
  const colors = ROBOT_COLOR();
  $("#robot-table tbody").innerHTML = f.robots.map((r) => `<tr>
    <td><span class="swatch" style="background:${colors[r.id]}"></span>${r.id}</td>
    <td>${esc(r.st)}</td><td class="mono">${mps(r.v)} m/s</td><td class="mono">${esc(r.job || "–")}</td></tr>`).join("");
}

function renderJobs(jobs) {
  $("#job-table tbody").innerHTML = jobs.length ? jobs.map((j) => `<tr>
    <td class="mono">${esc(j.id)}</td><td>${j.lines.map((l) => esc(l.slot)).join(", ")} → ${esc(j.dock)}</td>
    <td>${esc(j.status)}</td><td class="mono">${esc(j.robot_id || "")}</td></tr>`).join("")
    : `<tr><td class="hint">No open jobs</td></tr>`;
}

async function refreshState() {
  try {
    const st = await api("/api/state");
    S.state = st;
    $("#policy-chip").textContent = `policy v${st.policy.version}`;
    $("#policy-chip").title = st.policy.rules.join("\n") || "no rules";
    $("#sim-chip").classList.toggle("online", st.sim_online);
    $("#sim-text").textContent = st.sim_online ? `sim live · ${st.run_id}` : "sim offline";
    const badge = $("#inbox-badge");
    badge.hidden = !st.open_failures; badge.textContent = st.open_failures;
    $("#auto-jobs").checked = st.auto_jobs;
    renderJobs(st.jobs);
    if (!S.lastFrame && st.frame && S.view === "floor") drawFloor($("#floor-canvas"), st.frame, { paths: true });
  } catch { /* shown via login or next poll */ }
}

// ------------------------------------------------------------------ inbox

async function renderInbox() {
  const rows = await api("/api/failures");
  $("#inbox-table tbody").innerHTML = rows.length ? rows.map((f) => `<tr class="link" data-id="${f.id}">
    <td class="mono">#${f.id}</td>
    <td>${pill(FAILURE_LABEL[f.type] || f.type, "bad")}</td>
    <td>${esc(f.robot_id)}</td>
    <td class="mono">${f.tick} <span class="hint">(${secs(f.tick)}s)</span></td>
    <td>${esc(SCENARIO_LABEL[f.scenario] || f.scenario || "organic")}</td>
    <td class="mono">${f.replayed ? `${f.reproduced}/${f.replayed} reproduced` : "–"}</td>
    <td>${statusPill(f.status)}</td>
    <td><a href="#/failure/${f.id}">Open →</a></td></tr>`).join("")
    : `<tr><td colspan="8" class="hint">No failures yet. Inject one from the Floor.</td></tr>`;
  for (const tr of $("#inbox-table tbody").querySelectorAll("tr.link")) tr.onclick = () => (location.hash = `#/failure/${tr.dataset.id}`);
}

// ------------------------------------------------------------------ failure detail + replay viewer

const byTick = (frames) => new Map((frames || []).map((f) => [f.t, f]));

async function openFailure(id) {
  if (!S.fv || S.fv.id !== id) {
    stopPlay();
    S.fv = { id, data: null, live: null, liveMeta: null, frames: {}, selected: null, tick: null, lo: 0, hi: 0, playing: null };
  }
  await loadFailure();
}

const reloadFailure = debounce(() => { if (S.view === "failure") loadFailure(); }, 300);

async function loadFailure() {
  const fv = S.fv;
  let data;
  try { data = await api(`/api/failures/${fv.id}`); } catch (e) { toast(e.message, "bad"); return; }
  fv.data = data;
  const f = data.failure, cap = data.capsule;
  $("#f-title").textContent = `${FAILURE_LABEL[f.type] || f.type} · ${f.robot_id} · #${f.id}`;
  $("#f-sub").innerHTML = `tick ${f.tick} (${secs(f.tick)} s into ${esc(f.run_id)}) · ${esc(SCENARIO_LABEL[f.scenario] || f.scenario || "organic failure")} · ${statusPill(f.status)}`
    + (f.note ? ` · ${esc(f.note)}` : "") + (cap ? ` · capsule <span class="mono">${short(cap.hash)}</span> (${Math.round(cap.size_bytes / 1024)} KB, ticks ${cap.start_tick}–${cap.end_tick})` : "");
  renderActions(f, cap);
  renderPipeline(data);
  if (cap && !fv.live) {
    const live = await api(`/api/capsules/${cap.id}/live`);
    fv.live = byTick(live.frames); fv.liveMeta = live;
    fv.lo = live.start + 1; fv.hi = live.end;
    fv.tick = Math.max(fv.lo, live.failure.tick - 5 * S.tickHz);
  }
  const done = data.replays.filter((r) => r.status === "done" && !(r.id in fv.frames));
  await Promise.all(done.map(async (r) => { const full = await api(`/api/replays/${r.id}`); fv.frames[r.id] = byTick(full.frames); }));
  for (const m of Object.values(fv.frames)) { const last = Math.max(...m.keys()); if (last > fv.hi) fv.hi = last; }
  const repro = data.replays.filter((r) => r.kind === "reproduce" || r.kind === "proof");
  if (!fv.selected || !(fv.selected in fv.frames)) {
    const firstDone = repro.find((r) => r.status === "done");
    fv.selected = firstDone ? firstDone.id : null;
  }
  renderReplayChips(repro);
  renderHypotheses(data);
  renderChain(data.chain);
  setupScrubber();
  drawViewer();
}

function renderActions(f, cap) {
  const terminal = ["fixed", "dismissed", "lost", "not_reproducible"].includes(f.status);
  const el = $("#f-actions");
  el.innerHTML = "";
  const add = (label, cls, fn, enabled = true) => {
    const b = document.createElement("button"); b.textContent = label; b.className = cls; b.disabled = !enabled; b.onclick = fn; el.append(b);
  };
  add("Replay again", "", () => act(() => api(`/api/failures/${f.id}/replay`, { body: {} }), "Replay queued on VM B"), !!cap);
  if (f.scenario) add("Re-inject live", "ghost", () => act(() => api(`/api/failures/${f.id}/reinject`, { body: {} }), "Scenario re-injected on the live floor"));
  if (["trials", "awaiting_approval", "no_fix"].includes(f.status)) add("Re-diagnose", "ghost", () => act(() => api(`/api/failures/${f.id}/diagnose`, { body: {} }), "Diagnosing again"));
  if (!terminal) add("Dismiss", "ghost", () => act(() => api(`/api/failures/${f.id}/dismiss`, { body: {} }), "Dismissed"));
}

function renderPipeline(data) {
  const f = data.failure, reps = data.replays, hyps = latestRound(data.hypotheses);
  const repro = reps.filter((r) => r.kind === "reproduce" && r.status === "done");
  const ok = repro.filter((r) => r.outcome === "reproduced" && r.matches_live);
  const trials = reps.filter((r) => r.kind === "trial" && r.status === "done");
  const avoided = trials.filter((r) => r.outcome === "avoided");
  const ready = hyps.filter((h) => ["ready", "approved"].includes(h.status));
  const order = ["open", "reproducing", "diagnosing", "trials", "awaiting_approval", "fixed"];
  const at = order.indexOf(f.status);
  const failedAt = { not_reproducible: 2, no_fix: 4, lost: 1 }[f.status];
  const stages = [
    ["Detected", `rule: ${f.type}`],
    ["Captured", data.capsule ? `capsule ${short(data.capsule.hash)}` : "waiting for T+2 s"],
    ["Reproduced", repro.length ? `${ok.length}/${repro.length} · hash = live` : "–"],
    ["Diagnosed", hyps.length ? `${hyps.length} hypotheses` : "–"],
    ["Fix trials", trials.length ? `${avoided.length}/${trials.length} avoid it` : "–"],
    ["Regression gate", ready.length ? `${ready.length} passed` : "–"],
    ["Approved", f.status === "fixed" ? esc(f.note || "") : "human decision"],
  ];
  const stageOf = { open: 1, reproducing: 2, diagnosing: 3, trials: 4, awaiting_approval: 6, fixed: 7 };
  const now = stageOf[f.status] ?? -1;
  $("#f-pipeline").innerHTML = stages.map(([t, d], i) => {
    let cls = "";
    if (failedAt !== undefined) cls = i < failedAt ? "done" : i === failedAt ? "fail" : "";
    else if (at >= 0) cls = i < now ? "done" : i === now ? "now" : "";
    if (f.status === "fixed") cls = "done";
    return `<li class="${cls}"><b>${t}</b>${d}</li>`;
  }).join("");
}

function renderReplayChips(repro) {
  const fv = S.fv;
  $("#replay-chips").innerHTML = repro.map((r) => {
    const good = r.kind === "proof" ? r.outcome === "avoided" : r.outcome === "reproduced" && r.matches_live;
    const label = r.status !== "done" ? `#${r.id} ${r.kind} · ${r.status}` :
      r.kind === "proof" ? `#${r.id} proof under v${r.policy_version} · ${r.outcome === "avoided" ? "clean" : r.outcome}` :
      `#${r.id} ${r.outcome}${r.matches_live ? " · hash = live" : " · DIVERGED"} · ${short(r.trajectory_hash)}`;
    return `<span class="chip replay mono ${fv.selected === r.id ? "sel" : ""}" data-id="${r.id}" style="border-color:${r.status === "done" ? (good ? "var(--ok)" : "var(--bad)") : ""}">${esc(label)}</span>`;
  }).join("") || `<span class="hint">waiting for a replay worker…</span>`;
  for (const c of $("#replay-chips").querySelectorAll(".chip.replay")) {
    c.onclick = () => { const id = Number(c.dataset.id); if (id in fv.frames) { fv.selected = id; renderReplayChips(repro); drawViewer(); } };
  }
  const sel = repro.find((r) => r.id === fv.selected);
  $("#replay-caption").textContent = sel ? (sel.kind === "proof" ? `Replay #${sel.id} under policy v${sel.policy_version}` : `Replay #${sel.id} on ${sel.worker || "VM B"} (${sel.duration_ms} ms)`) : "Replay";
}

const latestRound = (hyps) => { const r = Math.max(0, ...hyps.map((h) => h.round)); return hyps.filter((h) => h.round === r); };

function renderHypotheses(data) {
  const fv = S.fv, hyps = latestRound(data.hypotheses);
  const SOURCES = { playbook: "the rule-based playbook", vultr: "Vultr Serverless Inference", openai: "OpenAI" };
  const [prov, model] = hyps.length ? hyps[0].source.split(":") : [];
  $("#diag-source").textContent = hyps.length ? `proposed by ${SOURCES[prov] || prov}${model ? ` · ${model}` : ""}` : "";
  if (!hyps.length) {
    $("#hypotheses").innerHTML = `<p class="hint">${data.failure.status === "diagnosing" ? "Diagnosing…" : "Hypotheses appear once the failure reproduces."}</p>`;
    return;
  }
  $("#hypotheses").innerHTML = hyps.map((h) => {
    const trial = data.replays.filter((r) => r.hypothesis_id === h.id && r.kind === "trial").at(-1);
    const g = h.gate || {}, reg = g.regression;
    const gate = h.status === "regression" ? `regression suite: ${h.regression_done}/${(g.suite || []).length} checked…`
      : reg ? `regression suite: ${reg.passed}/${reg.total} clean${reg.total ? ` under v${g.policy_version}` : " (suite empty)"}` : "";
    const newF = trial && trial.new_failures && trial.new_failures.length ? `new failures: ${trial.new_failures.map((x) => `${x.type} ${x.robot}`).join(", ")}` : "";
    return `<div class="hyp ${h.status}">
      <div class="row between"><b>#${h.rank}</b>${statusPill2(h.status)}</div>
      <div class="fix">${esc(h.fix_dsl)}</div>
      <div class="cause">${esc(h.cause)}</div>
      <div class="hint">${esc(h.rationale || "")}</div>
      <canvas data-rid="${trial ? trial.id : ""}"></canvas>
      <div class="row between"><span>trial: ${trial ? outcomePill(trial.status === "done" ? trial.outcome : null) : pill("queued")}</span>
        ${h.status === "ready" ? `<button class="approve" data-id="${h.id}">Approve → ship</button>` : ""}</div>
      <div class="gate">${esc(gate)}${newF ? `<br>${esc(newF)}` : ""}</div>
    </div>`;
  }).join("");
  for (const b of $("#hypotheses").querySelectorAll("button.approve")) {
    b.onclick = async () => {
      b.disabled = true;
      const r = await act(() => api(`/api/hypotheses/${b.dataset.id}/approve`, { body: {} }), (x) => `Policy v${x.result.version} is live on the fleet`);
      if (!r) b.disabled = false;
      reloadFailure();
    };
  }
  fv.hypCanvases = [...$("#hypotheses").querySelectorAll("canvas")];
}

function statusPill2(s) {
  const map = { trial: ["replaying", "info"], failed: ["failed trial", "bad"], regression: ["regression check", "info"],
    ready: ["ready to ship", "ok"], rejected: ["broke the suite", "bad"], approved: ["shipped", "ok"], superseded: ["superseded", ""] };
  return pill(...(map[s] || [s, ""]));
}

function renderChain(chain) {
  $("#chain").innerHTML = chain.map((e) => `<li><span class="t">${e.tick ?? ""}</span><span>${esc(e.type)}</span><span>${esc(summarize(e))}</span></li>`).join("");
}

function summarize(e) {
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
    default: return JSON.stringify(p).slice(0, 180);
  }
}

function setupScrubber() {
  const fv = S.fv, s = $("#scrub");
  if (!fv.live) { s.max = 0; return; }
  s.min = fv.lo; s.max = fv.hi; s.value = fv.tick;
  const span = Math.max(1, fv.hi - fv.lo), pct = (t) => `${((t - fv.lo) / span) * 100}%`;
  const marks = [[fv.liveMeta.failure.tick, "fail", "failure"], [fv.liveMeta.end, "end", "recording ends"]];
  for (const f of fv.live.values()) for (const e of f.ev || []) if (["pallet_dropped", "bin_mislabeled", "zone_restricted"].includes(e.type)) marks.push([f.t, "chaos", e.type.replace("_", " ")]);
  $("#scrub-marks").innerHTML = marks.map(([t, c, label]) => `<span class="m-${c}" style="left:${pct(t)}" title="${label} · t${t}"></span>`).join("");
}

function drawViewer() {
  const fv = S.fv;
  if (!fv || !fv.live) { drawFloor($("#live-canvas"), null, { empty: "capturing…" }); drawFloor($("#replay-canvas"), null, { empty: "" }); return; }
  const t = fv.tick, end = fv.liveMeta.end;
  const live = fv.live.get(Math.min(t, end));
  drawFloor($("#live-canvas"), live, { label: t > end ? `t ${end} · recording ends (T+2 s)` : `t ${t}` });
  $("#live-cap").textContent = `ticks ${fv.lo - 1}–${end}`;
  const sel = fv.selected != null ? fv.frames[fv.selected] : null;
  drawFloor($("#replay-canvas"), sel ? sel.get(t) : null, { label: `t ${t}`, empty: "waiting for a replay worker…" });
  for (const c of fv.hypCanvases || []) {
    const m = c.dataset.rid ? fv.frames[c.dataset.rid] : null;
    drawFloor(c, m ? m.get(t) : null, { label: `t ${t}`, empty: "replaying…" });
  }
  $("#scrub").value = t;
  const fail = fv.liveMeta.failure.tick;
  $("#scrub-label").textContent = `t ${t} (${t < fail ? "−" : "+"}${secs(Math.abs(t - fail))} s)`;
  const src = sel || fv.live, near = [];
  for (let k = t - 20; k <= t; k++) { const f = src.get(k); if (f) for (const e of f.ev || []) near.push(`t${k} ${e.robot || ""} ${e.type}${e.with ? ` ${e.with}` : ""}${e.slot ? ` ${e.slot}` : ""}${e.zone ? ` ${e.zone}` : ""}${e.ok === false ? " MISMATCH" : ""}`); }
  $("#viewer-events").textContent = near.slice(-4).join("   ·   ");
}

function stopPlay() {
  if (S.fv && S.fv.playing) { clearInterval(S.fv.playing); S.fv.playing = null; }
  $("#play").textContent = "▶";
}

$("#play").onclick = () => {
  const fv = S.fv;
  if (!fv || !fv.live) return;
  if (fv.playing) { stopPlay(); return; }
  if (fv.tick >= fv.hi) fv.tick = fv.lo;
  $("#play").textContent = "❚❚";
  fv.playing = setInterval(() => {
    fv.tick = Math.min(fv.hi, fv.tick + Number($("#speed").value));
    drawViewer();
    if (fv.tick >= fv.hi) stopPlay();
  }, 100);
};
$("#scrub").oninput = (e) => { if (S.fv) { S.fv.tick = Number(e.target.value); drawViewer(); } };

// ------------------------------------------------------------------ regression, policy, log

async function renderSuite() {
  const rows = await api("/api/regression");
  $("#suite-table tbody").innerHTML = rows.length ? rows.map((k) => `<tr class="link" data-f="${k.failure_id}">
    <td class="mono">#${k.id}</td><td>${pill(FAILURE_LABEL[k.type] || k.type, "bad")} ${esc(k.robot_id)} · ${esc(SCENARIO_LABEL[k.scenario] || k.scenario || "organic")}</td>
    <td><code>${esc(k.fix_dsl || "–")}</code></td><td>v${k.fixed_in ?? "–"}</td>
    <td>${k.latest ? `${outcomePill(k.latest.status === "done" ? k.latest.outcome : null)} under v${k.latest.policy_version}` : "–"}</td>
    <td class="mono">${short(k.hash)}</td></tr>`).join("")
    : `<tr><td colspan="6" class="hint">Empty. Approving a fix adds its capsule here.</td></tr>`;
  for (const tr of $("#suite-table tbody").querySelectorAll("tr.link")) tr.onclick = () => (location.hash = `#/failure/${tr.dataset.f}`);
}
$("#run-suite").onclick = () => act(() => api("/api/regression/run", { body: {} }), (r) => `${r.queued} capsules queued for re-check`).then(() => setTimeout(renderSuite, 1500));

async function renderPolicy() {
  const rows = await api("/api/policy");
  $("#policy-table tbody").innerHTML = rows.map((p) => `<tr>
    <td><b>v${p.version}</b></td><td>${p.fix_dsl ? `<code>${esc(p.fix_dsl)}</code>` : "–"}</td>
    <td class="mono">${p.rules.length ? p.rules.map(esc).join("<br>") : "base policy"}</td>
    <td>${esc(p.approved_by || "")}</td><td>${p.source_capsule_id ? `#${p.source_capsule_id}` : "–"}</td>
    <td class="mono">${short(p.hash)}</td><td class="hint">${new Date(p.created_at).toLocaleString()}</td></tr>`).join("");
}

async function renderLog(reset = true) {
  const type = $("#log-filter").value;
  if (reset) { S.log.before = null; $("#log-list").innerHTML = ""; }
  const q = new URLSearchParams({ limit: "150" });
  if (type) q.set("type", type);
  if (S.log.before) q.set("before", S.log.before);
  const rows = await api(`/api/events?${q}`);
  if (rows.length) S.log.before = rows[rows.length - 1].id;
  $("#log-list").insertAdjacentHTML("beforeend", rows.map((e) => `<li class="${esc(e.type.split(".")[0])}" data-tick="${e.tick ?? ""}">
    <span>#${e.id}</span><span>${e.tick ?? ""}</span><span class="ty">${esc(e.type)}</span><span class="p">${esc(summarize(e))}</span></li>`).join(""));
  for (const li of $("#log-list").querySelectorAll("li")) li.onclick = () => { if (li.dataset.tick) { $("#log-scrub").value = li.dataset.tick; showLogFrame(); } };
  if (reset) {
    const tl = await api("/api/timeline");
    if (tl.first != null) {
      const s = $("#log-scrub"); s.min = tl.first; s.max = tl.last;
      if (!s.dataset.set) { s.value = tl.last; s.dataset.set = "1"; }
      showLogFrame();
    } else drawFloor($("#log-canvas"), null, { empty: "no telemetry yet" });
  }
}
const showLogFrame = debounce(async () => {
  const t = $("#log-scrub").value;
  $("#log-tick").textContent = `t ${t} · ${secs(t)} s`;
  try { drawFloor($("#log-canvas"), await api(`/api/frame?tick=${t}`), { paths: true, label: `t ${t}` }); }
  catch (e) { drawFloor($("#log-canvas"), null, { empty: e.message }); }
}, 60);
$("#log-scrub").oninput = showLogFrame;
$("#log-filter").onchange = () => renderLog(true);
$("#log-more").onclick = () => renderLog(false);

// ------------------------------------------------------------------ router

const VIEWS = ["floor", "inbox", "failure", "regression", "log", "policy"];

async function route() {
  const [, name = "floor", arg] = location.hash.split("/");
  const view = VIEWS.includes(name) ? name : "floor";
  if (view !== "failure") stopPlay();
  S.view = view;
  for (const v of VIEWS) $(`#view-${v}`).hidden = v !== view;
  for (const a of document.querySelectorAll("#nav a")) a.classList.toggle("active", a.dataset.view === view || (view === "failure" && a.dataset.view === "inbox"));
  try {
    if (view === "floor") { if (S.lastFrame) drawFloor($("#floor-canvas"), S.lastFrame, { paths: true }); refreshState(); }
    else if (view === "inbox") await renderInbox();
    else if (view === "failure") await openFailure(Number(arg));
    else if (view === "regression") await renderSuite();
    else if (view === "log") await renderLog(true);
    else if (view === "policy") await renderPolicy();
  } catch (e) { toast(e.message, "bad"); }
}
window.addEventListener("hashchange", route);
window.addEventListener("resize", debounce(() => { if (S.view === "failure") drawViewer(); else route(); }, 150));

// ------------------------------------------------------------------ live updates

const refreshInbox = debounce(() => { if (S.view === "inbox") renderInbox(); }, 400);
const refreshSuite = debounce(() => { if (S.view === "regression") renderSuite(); }, 500);
const refreshStateSoon = debounce(refreshState, 500);

function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onmessage = (m) => {
    const { type, data } = JSON.parse(m.data);
    if (type === "frames") S.frameQ.push(...data);
    else if (type === "failure") {
      if (data.type) toast(`${FAILURE_LABEL[data.type] || data.type}: ${data.robot} at t${data.tick}. Capturing capsule…`, "bad");
      refreshInbox();
      if (S.view === "failure" && S.fv && S.fv.id === data.id) reloadFailure();
    } else if (type === "replay" || type === "hypothesis") {
      if (S.view === "failure") reloadFailure();
      refreshSuite();
      refreshInbox();
    } else if (type === "policy") {
      toast(`Policy v${data.version} pushed to the fleet`, "ok");
      $("#policy-chip").textContent = `policy v${data.version}`;
    } else if (type === "notice") toast(data.message || data.type, "bad");
    else if (type === "job" && S.view === "floor") refreshStateSoon();
  };
  ws.onclose = (e) => { if (e.code === 4401) showLogin(); else setTimeout(connect, 2000); };
}

// ------------------------------------------------------------------ boot + login

function showLogin() { $("#login").hidden = false; $("#login-user").focus(); }

$("#login-form").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/api/login", { body: { username: $("#login-user").value, password: $("#login-pass").value } });
    $("#login").hidden = true; $("#login-error").textContent = "";
    boot();
  } catch (err) { $("#login-error").textContent = err.message; }
};
$("#logout").onclick = async () => { await api("/api/logout", { body: {} }).catch(() => {}); location.reload(); };
$("#auto-jobs").onchange = (e) => act(() => api("/api/jobs/auto", { body: { enabled: e.target.checked } }));
$("#job-form").onsubmit = (e) => {
  e.preventDefault();
  const slots = $("#job-slots").value.split(/[\s,]+/).filter(Boolean).map((s) => s.toUpperCase());
  act(() => api("/api/jobs", { body: { slots, dock: $("#job-dock").value } }), (j) => `Job ${j.id} queued`).then((j) => { if (j) { $("#job-slots").value = ""; refreshState(); } });
};

let booted = false;
async function boot() {
  try { await api("/api/me"); } catch { return; }
  if (booted) { route(); return; }
  booted = true;
  const m = await api("/api/map");
  S.map = m; S.tickHz = m.tick_hz;
  for (const [id, s] of Object.entries(m.slots)) S.slotAt[s.cell.join(",")] = id;
  for (const [id, c] of Object.entries(m.docks)) S.dockAt[c.join(",")] = id;
  $("#job-dock").innerHTML = Object.keys(m.docks).map((d) => `<option>${d}</option>`).join("");
  const labels = { pallet_drop: "Pallet falls in an aisle", mislabel_bin: "Bin gets mislabeled", worker_in_aisle: "Worker closes an aisle", clear_floor: "Clear the floor" };
  $("#chaos-buttons").innerHTML = Object.entries(m.scenarios).map(([k, d]) =>
    `<button class="chaos ${k === "clear_floor" ? "clear" : ""}" data-s="${k}"><b>${esc(labels[k] || k)}</b><span>${esc(d)}</span></button>`).join("");
  for (const b of $("#chaos-buttons").querySelectorAll("button")) {
    b.onclick = () => act(() => api("/api/chaos", { body: { scenario: b.dataset.s } }),
      b.dataset.s === "clear_floor" ? "Floor cleared" : "Armed: fires as soon as a robot is in position");
  }
  const c = ROBOT_COLOR();
  $("#legend").innerHTML = Object.entries(c).map(([id, col]) => `<span><i style="background:${col}"></i>${id}${id === "R4" ? " (heavy lift)" : ""}</span>`).join("")
    + `<span><i style="background:${css("--pallet")}"></i>dropped pallet</span><span><i style="background:${css("--zone")};border:1px solid var(--bad)"></i>closed aisle</span>`
    + `<span><i style="background:var(--dock)"></i>dock</span>`;
  connect();
  refreshState();
  setInterval(refreshState, 2000);
  route();
}

boot();
