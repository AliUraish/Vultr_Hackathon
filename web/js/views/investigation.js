// Investigation panel on the incident page: the agent's live trace (left), the selected experiment in
// detail (right), a variant player, the report, and a form to test your own fix. Every number shown
// comes from an experiment row on Vultr and every variant can be replayed.
import { $, $$, esc, api, toast, pill, bus, countTo, cssVar, short } from "../util.js";
import { ReplayViewer } from "../viewer.js";

const TOOL = {
  reproduce: ["⟲", "Reproduce"], isolate_cause: ["◇", "Isolate the cause"], what_if: ["⑂", "What if"],
  stress_test: ["▦", "Stress test"], tune_fix: ["⌇", "Tune the fix"], submit_findings: ["✓", "Findings"],
};
const OUTCOME = {
  prevented: "fails today, safe with the fix", clean: "safe either way", still_fails: "still fails with the fix",
  introduced: "safe today, fails with the fix", not_placed: "hazard could not be placed",
};
const pct = (x) => (x == null ? "–" : `${Math.round(x * 100)}%`);
const cost = (c) => (c == null ? "–" : `${c > 0 ? "−" : "+"}${Math.abs(c).toFixed(1)}% throughput`);

let root = null, viewer = null, cid = null, inv = null, sel = null, follow = true;
let cache = new Map(), marked = null, varViewer = null, timer = null, offLab = null, detailSig = "", reportSig = "";
let progress = {};   // experiment id -> {done, total} from live worker results

export function mount(el, mainViewer) {
  root = el; viewer = mainViewer;
  root.innerHTML = "";
  offLab = bus.on("lab", onLab);
}

export function unmount() {
  if (offLab) offLab();
  if (varViewer) varViewer.destroy();
  clearInterval(timer);
  root = viewer = inv = varViewer = null; sel = null; follow = true; cache = new Map(); marked = null;
  detailSig = reportSig = ""; progress = {};
}

// ------------------------------------------------------------------ top level

export function update(d) {
  if (!root) return;
  cid = d.capsule ? d.capsule.id : null;
  const next = d.investigation;
  if (!next) {
    root.innerHTML = d.failure.status === "diagnosing" ? shell() : "";
    return;
  }
  if (!inv || inv.id !== next.id) {
    root.innerHTML = shell();
    sel = null; follow = true; detailSig = reportSig = ""; progress = {};
    if (varViewer) { varViewer.destroy(); varViewer = null; }
    wireForm();
  }
  inv = next;
  for (const e of inv.experiments) {
    const total = Number(e.jobs);
    progress[e.id] = { total, done: e.status === "running" ? Math.max(Number(e.jobs_done), progress[e.id]?.done || 0) : total };
  }
  renderHead();
  renderTrace();
  if (follow) {
    const last = [...inv.steps].reverse().find((s) => s.experiment_id);
    const top = inv.report && inv.report.fixes[0] && inv.report.fixes[0].stress && inv.report.fixes[0].stress.experiment;
    const pick = inv.status === "done" && top ? top : last && last.experiment_id;
    if (pick && pick !== sel) select(pick, false);
  }
  renderReport();
  markTimeline();
  clearInterval(timer);
  if (inv.status === "running") timer = setInterval(renderClock, 1000);
}

function shell() {
  return `<div class="panel inv">
    <div class="panel-head">
      <div class="row wrap"><h3>Investigation</h3><span id="inv-status">${pill("starting…", "info live")}</span><span class="hint" id="inv-source"></span></div>
      <div class="inv-stats">
        <div><b id="inv-steps">0</b><span>steps</span></div>
        <div><b id="inv-sims">0</b><span>simulations on VM B</span></div>
        <div><b id="inv-tokens">–</b><span>model tokens</span></div>
        <div><b id="inv-time">0 s</b><span>elapsed</span></div>
      </div>
    </div>
    <div class="inv-body">
      <div><ol class="inv-trace" id="inv-trace"><li class="step running"><span class="ico">…</span><div class="hint">The investigator is reading the capsule…</div></li></ol></div>
      <div class="inv-detail" id="inv-detail"><div class="empty">Pick a step to see its experiment.</div></div>
    </div>
    <div id="inv-report"></div>
    <form class="inv-form" id="inv-form">
      <span class="hint">Challenge the agent: test your own fix on this incident</span>
      <input id="inv-fix" placeholder="e.g. speed_cap(racks, 0.7)" autocomplete="off" spellcheck="false">
      <button type="button" data-k="what_if">What if</button><button type="button" data-k="stress">Stress test</button><button type="button" data-k="tune">Tune</button>
    </form>
  </div>`;
}

function renderHead() {
  const st = { running: ["investigating", "info live"], done: ["done", "ok"], error: ["failed", "bad"] }[inv.status] || [inv.status, ""];
  $("#inv-status", root).innerHTML = pill(...st);
  const [prov, model] = String(inv.source || "").split(":");
  const who = { vultr: "Vultr Serverless Inference", scripted: "the scripted investigator", starting: "" }[prov] ?? prov;
  $("#inv-source", root).textContent = who ? `driven by ${who}${model ? ` · ${model}` : ""}` : "";
  countTo($("#inv-steps", root), inv.steps.length);
  countTo($("#inv-sims", root), inv.sims, { ms: 900 });
  const tok = inv.report && inv.report.tokens;
  $("#inv-tokens", root).textContent = tok && (tok.in || tok.out) ? `${((tok.in + tok.out) / 1000).toFixed(1)}k` : "–";
  renderClock();
}

function renderClock() {
  if (!inv || !root) return;
  const t0 = Date.parse(inv.created_at), t1 = inv.finished_at ? Date.parse(inv.finished_at) : Date.now();
  const el = $("#inv-time", root); if (el) el.textContent = `${Math.max(0, Math.round((t1 - t0) / 1000))} s`;
}

// ------------------------------------------------------------------ trace

function argText(s) {
  const a = s.args || {};
  if (s.tool === "what_if") return (a.rules || []).join(" + ");
  if (s.tool === "stress_test") return `${a.fix || ""}${a.variants ? ` × ${a.variants} variants` : ""}`;
  if (s.tool === "tune_fix") return `${a.fix || ""}${a.values ? ` @ ${a.values.join(", ")}` : ""}`;
  if (s.tool === "reproduce") return `× ${a.times || 3}`;
  if (s.tool === "submit_findings") return (a.fixes || []).join(", ");
  return "";
}

function renderTrace() {
  const ol = $("#inv-trace", root);
  if (!inv.steps.length) return;
  if (ol.querySelector(".step:not([data-n])")) ol.innerHTML = "";
  for (const s of inv.steps) {
    let li = ol.querySelector(`li[data-n="${s.n}"]`);
    const sig = `${s.status}|${s.summary}|${s.experiment_id}`;
    if (li && li.dataset.sig === sig) continue;
    const [ico, label] = TOOL[s.tool] || ["•", s.tool];
    const html = `<span class="ico">${ico}</span>
      <div class="body"><div class="row between"><b>${esc(label)}</b><span class="mono dim">${s.n}</span></div>
        ${argText(s) ? `<code>${esc(argText(s))}</code>` : ""}
        ${s.why ? `<div class="why">${esc(s.why)}</div>` : ""}
        ${s.status === "running" ? `<div class="bar"><i></i></div>` : `<div class="res ${s.status}">${esc(s.summary || "")}</div>`}
      </div>`;
    if (!li) { li = document.createElement("li"); li.dataset.n = s.n; ol.append(li); li.onclick = () => { if (li.dataset.exp) { follow = false; select(Number(li.dataset.exp), true); } }; }
    li.className = `step ${s.status} ${s.tool}`;
    li.dataset.sig = sig;
    if (s.experiment_id) li.dataset.exp = s.experiment_id;
    li.innerHTML = html;
    li.classList.toggle("sel", s.experiment_id != null && s.experiment_id === sel);
  }
  renderProgress();
  if (inv.status === "running" && follow) ol.scrollTop = ol.scrollHeight;
}

function renderProgress() {
  const running = inv.experiments.filter((e) => e.status === "running");
  const done = running.reduce((a, e) => a + (progress[e.id]?.done || 0), 0), total = running.reduce((a, e) => a + (progress[e.id]?.total || 0), 0);
  for (const bar of $$(".step.running .bar i", root)) bar.style.width = total ? `${Math.max(6, (done / total) * 100)}%` : "12%";
}

function onLab(d) {
  if (!inv || !d.experiment_id || !(d.experiment_id in progress)) return;
  const p = progress[d.experiment_id];
  p.done = Math.min(p.total || Infinity, (p.done || 0) + 1);
  renderProgress();
}

// ------------------------------------------------------------------ experiment detail

async function fetchExp(eid) {
  if (cache.has(eid)) return cache.get(eid);
  const e = await api(`/api/experiments/${eid}`);
  if (e.status !== "running") cache.set(eid, e);
  return e;
}

async function select(eid, scroll) {
  sel = eid;
  for (const li of $$("#inv-trace li", root)) li.classList.toggle("sel", Number(li.dataset.exp) === eid);
  let e;
  try { e = await fetchExp(eid); } catch (err) { toast(err.message, "bad"); return; }
  if (sel !== eid || !root) return;
  showExperiment(e);
  if (scroll) $("#inv-detail", root).scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function showExperiment(e, point = null) {
  const sig = `${e.id}|${e.status}|${point}`;
  if (sig === detailSig) return;
  detailSig = sig;
  if (varViewer) { varViewer.destroy(); varViewer = null; }
  const el = $("#inv-detail", root);
  const r = e.result || {};
  const head = `<div class="row between"><b>${esc(TOOL[{ isolate: "isolate_cause", stress: "stress_test", tune: "tune_fix" }[e.kind] || e.kind]?.[1] || e.kind)}</b>
    <span class="mono dim">experiment #${e.id} · ${e.sims} sims · ${e.duration_ms != null ? `${(e.duration_ms / 1000).toFixed(1)} s` : "running"}</span></div>
    ${e.summary ? `<div class="summary">${esc(e.summary)}</div>` : ""}${e.error ? `<div class="error">${esc(e.error)}</div>` : ""}`;
  if (e.status === "running") { el.innerHTML = head + `<div class="skeleton" style="height:120px"></div>`; return; }
  const body = { reproduce: reproView, isolate: isolateView, what_if: whatIfView, stress: stressView, tune: tuneView, baseline: baselineView }[e.kind];
  el.innerHTML = head + (body ? body(e, r, point) : "");
  el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
  if (e.kind === "isolate") drawDdmin($("canvas.ddmin", el), r.trace || [], r.total || 1);
  if (e.kind === "tune") wireTune(e, r, point);
  if (e.kind === "stress" || e.kind === "tune") wireTiles(e, point);
  if (e.kind === "what_if") $("button.watch", el)?.addEventListener("click", () => watchWhatIf(r));
}

function reproView(e, r) {
  const runs = r.runs || [];
  return `<div class="hashes">${runs.map((x, i) => `<div class="hash ${x.matches_live && x.outcome === "reproduced" ? "ok" : "bad"}" style="animation-delay:${i * 120}ms">
      <span class="mono">${esc(short(x.hash))}…</span><span>${esc(x.worker || "VM B")}</span><span>${x.matches_live ? "= live" : "≠ live"} · ${x.ms} ms</span></div>`).join("")}</div>
    <div class="verdict ${r.identical && r.matches_live ? "ok" : "bad"}">${r.identical && r.matches_live ? "Deterministic: every replay is identical to the live run, tick for tick" : "Not deterministic"}</div>`;
}

function isolateView(e, r) {
  if (!r.ok) return `<div class="hint">${esc(r.reason || "")}</div>`;
  return `<canvas class="ddmin"></canvas>
    <div class="legend"><span><i style="background:var(--bad)"></i>subset still causes the failure</span><span><i style="background:var(--line-2)"></i>does not</span></div>
    <div class="hint">Delta debugging replayed ${r.tested} subsets of the ${r.total} recorded events.</div>
    <ul class="minimal">${(r.minimal || []).map((m) => `<li class="${m.kind}"><span class="mono">${esc(m.input)}</span></li>`).join("") || "<li>no recorded event is needed: the cause is in the starting state</li>"}</ul>`;
}

function whatIfView(e, r) {
  const good = r.outcome === "avoided";
  return `<div class="verdict ${good ? "ok" : r.outcome === "reproduced" ? "bad" : "warn"}">${good ? "The failure does not happen" : r.outcome === "reproduced" ? "The failure still happens" : "Avoided, but something else breaks"}</div>
    <div class="rules">${(r.rules || []).map((x) => `<code>${esc(x)}</code>`).join("")}</div>
    ${r.replay ? `<button class="watch primary">Watch it against the incident ▸</button>` : ""}`;
}

function gauge(label, v, cls) {
  return `<div class="gauge ${cls}"><span>${label}</span><div class="g"><i style="--w:${Math.round((v || 0) * 100)}%"></i></div><b>${pct(v)}</b></div>`;
}

function tiles(list) {
  return `<div class="tiles">${list.map((t, i) => `<button class="tile ${t.outcome}" data-v="${t.id}" style="animation-delay:${i * 18}ms"
      title="variant ${t.id}: ${esc(t.label)} · ${OUTCOME[t.outcome] || t.outcome}"></button>`).join("")}</div>
    <div class="legend">${Object.entries(OUTCOME).map(([k, v]) => `<span><i class="tile ${k}"></i>${v}</span>`).join("")}</div>`;
}

function stressView(e, r) {
  return `<div class="gauges">${gauge("today", r.baseline_robustness, "base")}${gauge("with fix", r.robustness, r.passes ? "ok" : "bad")}
      <div class="gate-line" style="--g:${r.min_robustness || 0.95}" title="the gate: ${pct(r.min_robustness || 0.95)} of variants safe"></div></div>
    <div class="nums"><span><b>${r.prevented}</b> prevented</span><span><b>${r.still_fails}</b> still fail</span><span><b>${r.introduced}</b> new</span>
      <span><b>${cost(r.cost_pct)}</b></span><span>jobs ${r.jobs_base} → ${r.jobs_fix}</span></div>
    ${tiles(r.tiles || [])}<div class="hint">Click a variant to watch it with and without the fix.</div><div id="inv-var"></div>`;
}

function baselineView(e, r) {
  const list = (r.runs || []).map((x, i) => ({ id: x.id, label: (r.variants || [])[i]?.label || "", outcome: !x.fired ? "not_placed" : x.target || x.safety.length ? "still_fails" : "clean" }));
  return tiles(list);
}

function tuneView(e, r, point) {
  const p = (r.points || []).find((x) => x.fix === (point || r.best)) || (r.points || [])[0];
  return `<canvas class="tune"></canvas><div class="chart-tip" hidden></div>
    <div class="legend"><span><i style="background:var(--ok)"></i>variants safe</span><span><i style="background:var(--warn)"></i>throughput cost</span>
      <span><i style="background:var(--accent)"></i>gate ${pct(r.min_robustness)}</span><span><i style="background:var(--dim)"></i>today ${pct(r.baseline_robustness)}</span></div>
    ${p ? `<div class="row between point"><code>${esc(p.fix)}</code><span>${pct(p.robustness)} safe · ${cost(p.cost_pct)}${p.fix === r.best ? " · " + pill("cheapest safe", "ok") : ""}</span></div>${tiles(p.tiles || [])}` : ""}
    <div id="inv-var"></div>`;
}

function wireTiles(e, point) {
  const fix = e.kind === "tune" ? (point || e.result.best || (e.result.points || [])[0]?.fix) : e.result.fix;
  for (const b of $$("#inv-detail .tiles .tile", root)) b.onclick = () => playVariant(e, Number(b.dataset.v), fix, b);
}

async function playVariant(e, vid, fix, btn) {
  for (const b of $$("#inv-detail .tile.sel", root)) b.classList.remove("sel");
  btn.classList.add("sel", "loading");
  let base, withFix;
  try {
    [base, withFix] = await Promise.all([
      api(`/api/experiments/${e.id}/render`, { body: { variant: vid, which: "base" } }),
      api(`/api/experiments/${e.id}/render`, { body: { variant: vid, which: "fix", fix } }),
    ]);
  } catch (err) { toast(err.message, "bad"); btn.classList.remove("loading"); return; }
  btn.classList.remove("loading");
  const host = $("#inv-var", root);
  if (!host) return;
  if (varViewer) varViewer.destroy();
  const bf = base.frames, ff = withFix.frames;
  if (!bf.length) return;
  const hit = base.run.first_hit || base.run.fired_at || bf[0].t;
  const verdict = (run) => (run.target || run.safety.length ? pill("fails", "bad") : pill("safe", "ok"));
  host.innerHTML = `<div class="var-head"><b>Variant ${vid}</b><span class="hint">${esc(base.variant.label || "")}</span>
      <span>today ${verdict(base.run)} → with <code>${esc(fix)}</code> ${verdict(withFix.run)}</span></div><div class="var-view"></div>`;
  varViewer = new ReplayViewer($(".var-view", host));
  varViewer.setLive(bf, { start: bf[0].t - 1, end: bf[bf.length - 1].t, failure: { tick: hit },
    caption: "Today's policy", endLabel: "variant ends" });
  varViewer.setReplay(ff, "With the fix", "");
  varViewer.setSpeed(2);
  varViewer.toggle();
  host.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function watchWhatIf(r) {
  try {
    const rep = await api(`/api/replays/${r.replay}`);
    viewer.setReplay(rep.frames || [], `What if ${r.rules.join(" + ")}`, r.outcome);
    viewer.setGhost(true);
    document.querySelector("#inc-viewer").scrollIntoView({ behavior: "smooth", block: "center" });
    toast("Solid = what happened live · outline = the same moment with the rule in force", "ok", 4000);
  } catch (err) { toast(err.message, "bad"); }
}

// ------------------------------------------------------------------ charts

function setup(canvas, height) {
  const dpr = window.devicePixelRatio || 1, w = canvas.clientWidth || 400, h = height;
  canvas.width = w * dpr; canvas.height = h * dpr; canvas.style.height = `${h}px`;
  const ctx = canvas.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h };
}

function animate(ms, draw) {
  const t0 = performance.now();
  const f = (now) => { const k = Math.min(1, (now - t0) / ms); draw(1 - (1 - k) ** 3); if (k < 1) requestAnimationFrame(f); };
  requestAnimationFrame(f);
}

function drawDdmin(canvas, trace, total) {
  if (!canvas) return;
  const { ctx, w, h } = setup(canvas, 120);
  const n = Math.max(trace.length, 1), bw = Math.min(26, (w - 8) / n), bad = cssVar("--bad"), dim = cssVar("--line-2");
  animate(700, (k) => {
    ctx.clearRect(0, 0, w, h);
    trace.forEach((t, i) => {
      const bh = Math.max(3, (t.size / total) * (h - 24) * Math.min(1, k * n / (i + 1)));
      ctx.fillStyle = t.fails ? bad : dim;
      if (t.fails) { ctx.shadowColor = bad; ctx.shadowBlur = 8; }
      ctx.fillRect(4 + i * bw, h - 14 - bh, bw - 3, bh);
      ctx.shadowBlur = 0;
    });
    ctx.fillStyle = cssVar("--dim"); ctx.font = "11px system-ui";
    ctx.fillText("events kept per test →", 4, h - 2);
  });
}

function wireTune(e, r, point) {
  const canvas = $("#inv-detail canvas.tune", root), tip = $("#inv-detail .chart-tip", root);
  const pts = (r.points || []).filter((p) => p.setting != null);
  if (!canvas || !pts.length) return;
  const { ctx, w, h } = setup(canvas, 220);
  const L = 44, R = 50, T = 14, B = 26;
  const xs = pts.map((p) => p.setting), x0 = Math.min(...xs), x1 = Math.max(...xs);
  const costs = pts.map((p) => p.cost_pct ?? 0), cMax = Math.max(5, ...costs), cMin = Math.min(0, ...costs);
  const X = (v) => L + (x1 === x0 ? 0.5 : (v - x0) / (x1 - x0)) * (w - L - R);
  const Y = (v) => T + (1 - v) * (h - T - B);
  const YC = (c) => T + (1 - (c - cMin) / (cMax - cMin)) * (h - T - B);
  const ok = cssVar("--ok"), warn = cssVar("--warn"), acc = cssVar("--accent"), dimc = cssVar("--dim"), bad = cssVar("--bad");
  const chosen = point || r.best;
  const draw = (k) => {
    ctx.clearRect(0, 0, w, h);
    ctx.font = "11px system-ui"; ctx.fillStyle = dimc; ctx.strokeStyle = cssVar("--line-2"); ctx.lineWidth = 1;
    for (const v of [0, 0.5, 1]) { ctx.beginPath(); ctx.moveTo(L, Y(v)); ctx.lineTo(w - R, Y(v)); ctx.stroke(); ctx.fillText(pct(v), 6, Y(v) + 4); }
    ctx.textAlign = "center";
    for (const p of pts) ctx.fillText(p.setting, X(p.setting), h - 8);
    ctx.textAlign = "left";
    ctx.fillStyle = warn; ctx.fillText(`${cMax.toFixed(0)}%`, w - R + 6, YC(cMax) + 4); ctx.fillText(`${cMin.toFixed(0)}%`, w - R + 6, YC(cMin) + 4);
    const dash = (y, color, label) => { ctx.setLineDash([5, 5]); ctx.strokeStyle = color; ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(w - R, y); ctx.stroke(); ctx.setLineDash([]); ctx.fillStyle = color; ctx.fillText(label, L + 4, y - 4); };
    dash(Y(r.min_robustness || 0.95), acc, `gate ${pct(r.min_robustness)}`);
    if (r.baseline_robustness != null) dash(Y(r.baseline_robustness), dimc, `today ${pct(r.baseline_robustness)}`);
    const upto = Math.max(1, Math.ceil(k * pts.length));
    const line = (val, color, y) => {
      ctx.strokeStyle = color; ctx.lineWidth = 2.2; ctx.shadowColor = color; ctx.shadowBlur = 10; ctx.beginPath();
      pts.slice(0, upto).forEach((p, i) => (i ? ctx.lineTo(X(p.setting), y(val(p))) : ctx.moveTo(X(p.setting), y(val(p)))));
      ctx.stroke(); ctx.shadowBlur = 0;
    };
    line((p) => p.cost_pct ?? 0, warn, YC);
    line((p) => p.robustness ?? 0, ok, Y);
    pts.slice(0, upto).forEach((p) => {
      const pass = (p.robustness ?? 0) >= (r.min_robustness || 0.95);
      ctx.fillStyle = pass ? ok : bad; ctx.beginPath(); ctx.arc(X(p.setting), Y(p.robustness ?? 0), 4, 0, 7); ctx.fill();
      if (p.fix === chosen) { ctx.strokeStyle = "#fff"; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(X(p.setting), Y(p.robustness ?? 0), 9, 0, 7); ctx.stroke(); }
    });
  };
  animate(900, draw);
  const nearest = (ev) => {
    const rect = canvas.getBoundingClientRect(), mx = ev.clientX - rect.left;
    return pts.reduce((a, p) => (Math.abs(X(p.setting) - mx) < Math.abs(X(a.setting) - mx) ? p : a), pts[0]);
  };
  canvas.onmousemove = (ev) => {
    const p = nearest(ev);
    tip.hidden = false;
    tip.style.left = `${canvas.offsetLeft + X(p.setting)}px`; tip.style.top = `${canvas.offsetTop + Y(p.robustness ?? 0)}px`;
    tip.innerHTML = `<code>${esc(p.fix)}</code><br>${pct(p.robustness)} safe · ${cost(p.cost_pct)}<br>prevents ${p.prevented}, still fails ${p.still_fails}, new ${p.introduced}`;
  };
  canvas.onmouseleave = () => { tip.hidden = true; };
  canvas.onclick = (ev) => { const p = nearest(ev); detailSig = ""; showExperiment(e, p.fix); };
}

// ------------------------------------------------------------------ report, timeline marks, operator form

function renderReport() {
  const el = $("#inv-report", root), r = inv.report;
  const sig = JSON.stringify([inv.status, inv.error, r && r.fixes]);
  if (sig === reportSig) return;
  reportSig = sig;
  if (inv.status === "error") { el.innerHTML = `<div class="report bad"><b>The investigation failed</b><div class="hint">${esc(inv.error || "")} · the one-shot diagnosis took over.</div></div>`; return; }
  if (!r) { el.innerHTML = ""; return; }
  el.innerHTML = `<div class="report">
    <div class="row between"><h3>Findings</h3><span>${pill(`confidence: ${r.confidence}`, r.confidence === "high" ? "ok" : r.confidence === "low" ? "warn" : "info")}</span></div>
    <p class="cause">${esc(r.root_cause)}</p>
    <ul class="evidence">${(r.evidence || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
    <div class="fixes">${(r.fixes || []).map((f, i) => `<div class="rec" data-exp="${f.stress?.experiment || ""}">
        <span class="rank">${i + 1}</span><code>${esc(f.fix)}</code>
        <div class="g small"><i style="--w:${Math.round((f.stress?.robustness || 0) * 100)}%"></i></div>
        <span class="mono">${pct(f.stress?.robustness)} safe · ${cost(f.stress?.cost_pct)}</span>
        <div class="hint">${esc(f.why)}</div></div>`).join("") || `<div class="hint">No fix passed.</div>`}</div>
    ${(r.rejected || []).length ? `<div class="rejected"><h3>Rejected</h3>${r.rejected.map((x) => `<div><code class="struck">${esc(x.fix)}</code> <span class="hint">${esc(x.reason)}</span></div>`).join("")}</div>` : ""}
    <div class="hint">${r.steps} steps · ${inv.sims} simulations · ${r.tokens && (r.tokens.in || r.tokens.out) ? `${r.tokens.in + r.tokens.out} model tokens · ` : ""}every number links to an experiment stored on Vultr. Fixes still need the exact-incident trial, the regression suite and your approval.</div>
  </div>`;
  for (const d of $$(".rec[data-exp]", el)) if (d.dataset.exp) d.onclick = () => { follow = false; select(Number(d.dataset.exp), true); };
}

async function markTimeline() {
  if (!viewer || marked === inv.id) return;
  const iso = inv.experiments.find((e) => e.kind === "isolate" && e.status === "done");
  if (!iso) return;
  marked = inv.id;
  try {
    const e = await fetchExp(iso.id);
    viewer.addMarks((e.result.minimal || []).map((m) => [m.tick, "m-cause", `cause: ${m.input}`]));
  } catch { marked = null; }
}

function wireForm() {
  const form = $("#inv-form", root);
  form.onsubmit = (e) => e.preventDefault();
  for (const b of $$("button", form)) {
    b.onclick = async () => {
      const fix = $("#inv-fix", form).value.trim();
      if (!fix || !cid) { toast("Type a rule first, e.g. speed_cap(racks, 0.7)", "warn"); return; }
      const kind = b.dataset.k;
      for (const x of $$("button", form)) x.disabled = true;
      b.classList.add("busy");
      try {
        const body = kind === "what_if" ? { kind, rules: [fix] } : { kind, fix };
        const res = await api(`/api/capsules/${cid}/experiments`, { body });
        toast(res.summary, "ok", 7000);
        cache.delete(res.experiment);
        follow = false;
        await select(res.experiment, true);
      } catch (err) { toast(err.message, "bad"); }
      finally { for (const x of $$("button", form)) x.disabled = false; b.classList.remove("busy"); }
    };
  }
}
