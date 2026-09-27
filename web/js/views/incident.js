// Incident page: evidence (live vs exact replay), investigation, fixes under test, audit trail.
import { $, $$, esc, api, act, toast, secs, short, pill, statusPill, outcomePill, summarize,
  FAILURE_LABEL, SCENARIO_LABEL, debounce, money, itemLabel, TIER } from "../util.js";
import { robotColor } from "../floor.js";
import { ReplayViewer } from "../viewer.js";
import * as investigation from "./investigation.js";

let root = null, viewer = null, fid = null, data = null, frames = {}, selected = null, hypSig = "";

export function mount(el) { root = el; }

export async function show(id) {
  if (fid !== id || !viewer) {
    viewer && viewer.destroy();
    fid = id; frames = {}; selected = null; hypSig = ""; data = null; impactSig = "";
    root.innerHTML = `
      <div class="incident-head">
        <div><a class="back" href="#/incidents">← Incidents</a><h1 id="inc-title"><span class="skeleton" style="width:260px;display:inline-block"></span></h1>
          <div class="sub" id="inc-sub"></div></div>
        <div class="actions" id="inc-actions"></div>
      </div>
      <ol class="stepper" id="inc-steps"></ol>
      <div id="inc-impact"></div>
      <div class="panel glow">
        <div class="panel-head"><h3>Evidence · live vs exact replay</h3><div class="chips" id="inc-chips"></div></div>
        <div id="inc-viewer"></div>
      </div>
      <div id="inc-investigation"></div>
      <div class="panel">
        <div class="panel-head"><h3>Fixes under test</h3><span class="hint" id="inc-source"></span></div>
        <div class="hyp-grid" id="inc-hyps"></div>
      </div>
      <div class="panel">
        <div class="panel-head"><h3>Audit trail</h3><span class="hint">every step, from the event log on Vultr</span></div>
        <ol class="chain" id="inc-chain"></ol>
      </div>`;
    viewer = new ReplayViewer($("#inc-viewer"));
    investigation.mount($("#inc-investigation"), viewer);
  }
  await load();
}

export function hide() { if (viewer) viewer.destroy(); viewer = null; fid = null; investigation.unmount(); }

export const reload = debounce(() => { if (fid) load(); }, 350);

export function onKey(e) { return viewer ? viewer.onKey(e) : false; }

async function load() {
  const id = fid;
  let d;
  try { d = await api(`/api/failures/${id}`); } catch (e) { toast(e.message, "bad"); return; }
  if (id !== fid) return;
  data = d;
  const f = d.failure, cap = d.capsule;
  $("#inc-title").innerHTML = `${esc(FAILURE_LABEL[f.type] || f.type)} <span style="color:${robotColor(f.robot_id)}">${esc(f.robot_id)}</span> <span class="dim">#${f.id}</span>`;
  $("#inc-sub").innerHTML = `${statusPill(f.status)} &nbsp; t${f.tick} (${secs(f.tick)} s into ${esc(f.run_id)}) · ${esc(SCENARIO_LABEL[f.scenario] || f.scenario || "organic")}`
    + (f.note ? ` · ${esc(f.note)}` : "")
    + (cap ? ` · capsule <span class="mono">${short(cap.hash)}</span> ${Math.round(cap.size_bytes / 1024)} KB` : "");
  renderActions(f, cap);
  renderSteps(d);
  renderImpact(d);
  if (cap && !viewer.live.size) {
    const live = await api(`/api/capsules/${cap.id}/live`);
    viewer.setLive(live.frames, live);
  }
  const done = d.replays.filter((r) => r.status === "done" && !(r.id in frames));
  await Promise.all(done.map(async (r) => { frames[r.id] = (await api(`/api/replays/${r.id}`)).frames || []; }));
  const shown = d.replays.filter((r) => r.kind === "reproduce" || r.kind === "proof");
  if (!selected || !(selected in frames)) {
    const first = shown.find((r) => r.status === "done");
    if (first) selectReplay(first);
  }
  renderChips(shown);
  renderHyps(d);
  renderChain(d.chain);
  investigation.update(d, frames);
}

function selectReplay(r) {
  selected = r.id;
  const cap = r.kind === "proof" ? `Replay #${r.id} under haul rules v${r.policy_version}` : `Exact replay #${r.id}`;
  viewer.setReplay(frames[r.id] || [], cap, `${r.worker || "VM B"} · ${r.duration_ms ?? "–"} ms`);
}

function renderActions(f, cap) {
  const el = $("#inc-actions"); el.innerHTML = "";
  const terminal = ["fixed", "dismissed", "lost", "not_reproducible"].includes(f.status);
  const add = (label, cls, fn, on = true) => { const b = document.createElement("button"); b.textContent = label; b.className = cls; b.disabled = !on; b.onclick = fn; el.append(b); };
  add("Replay again", "primary", () => act(() => api(`/api/failures/${f.id}/replay`, { body: {} }), "Replay queued on VM B"), !!cap);
  if (f.scenario) add("Re-inject live", "", () => act(() => api(`/api/failures/${f.id}/reinject`, { body: {} }), "Recreating the original situation in the live pit"));
  if (["trials", "awaiting_approval", "no_fix"].includes(f.status)) add("Investigate again", "", () => act(() => api(`/api/failures/${f.id}/diagnose`, { body: {} }), "New investigation started"));
  if (!terminal) add("Dismiss", "", () => act(() => api(`/api/failures/${f.id}/dismiss`, { body: {} }), "Dismissed"));
}

let impactSig = "";
function renderImpact(d) {
  const im = d.impact, o = d.order, det = d.failure.detail || {};
  const sig = JSON.stringify([im, o && o.status]);
  if (sig === impactSig) return;
  impactSig = sig;
  if (!im) { $("#inc-impact").innerHTML = ""; return; }
  const mism = d.failure.type === "wrong_item" && det.expected ? `<div class="mism">
      <span class="hint">should be</span><span>${(det.expected || []).map((x) => esc(itemLabel(x))).join(", ")}</span>
      <span class="hint">was loaded</span><span class="bad">${(det.actual || []).map((x) => esc(itemLabel(x))).join(", ") || "–"}</span></div>` : "";
  $("#inc-impact").innerHTML = `<div class="impact">
    <div class="cost"><span class="hint">estimated cost of this incident</span><b>${money(im.estimated_cost_usd)}</b><span class="hint">${esc(im.basis)}</span></div>
    ${o ? `<div class="ord">
      <div class="row wrap"><span class="mono">${esc(o.id)}</span><b>${esc(o.customer)}</b>${pill(o.tier, TIER[o.tier] || "")}
        ${o.priority === "expedite" ? pill("expedite", "warn") : ""}<span class="hint">for ${esc(o.customer)}</span></div>
      <div class="lines">${o.lines.map((l) => `${l.qty} t ${esc(l.name)} <span class="mono dim">${esc(l.sku)} · ${esc(l.slot)}</span>`).join("<br>")}</div>
      <div class="hint">${money(o.value, 2)} · ${esc(o.carrier)} → ${esc(o.dock)} · order is ${esc(o.status.replace("_", " "))}</div>
    </div>` : `<div class="ord hint">No load ticket was in progress.</div>`}
    ${mism}</div>`;
}

const latestRound = (hyps) => { const r = Math.max(0, ...hyps.map((h) => h.round)); return hyps.filter((h) => h.round === r); };

function renderSteps(d) {
  const f = d.failure, reps = d.replays, hyps = latestRound(d.hypotheses);
  const repro = reps.filter((r) => r.kind === "reproduce" && r.status === "done");
  const exact = repro.filter((r) => r.outcome === "reproduced" && r.matches_live);
  const trials = reps.filter((r) => r.kind === "trial" && r.status === "done");
  const avoided = trials.filter((r) => r.outcome === "avoided");
  const ready = hyps.filter((h) => ["ready", "approved"].includes(h.status));
  const inv = d.investigation;
  const stages = [
    ["Detected", `rule: ${f.type}`],
    ["Captured", d.capsule ? `capsule ${short(d.capsule.hash)}` : "waiting for T+2 s"],
    ["Reproduced", repro.length ? `${exact.length}/${repro.length} exact` : "–"],
    ["Investigated", inv ? `${inv.steps.length} steps · ${inv.sims} sims` : hyps.length ? `${hyps.length} hypotheses` : "–"],
    ["Fix trials", trials.length ? `${avoided.length}/${trials.length} avoid it` : "–"],
    ["Regression gate", ready.length ? `${ready.length} passed` : "–"],
    ["Approved", f.status === "fixed" ? esc(f.note || "") : "human decision"],
  ];
  const now = { open: 1, reproducing: 2, diagnosing: 3, trials: 4, awaiting_approval: 6, fixed: 7 }[f.status] ?? -1;
  const failedAt = { not_reproducible: 2, no_fix: 4, lost: 1 }[f.status];
  $("#inc-steps").innerHTML = stages.map(([t, s], i) => {
    let cls = "";
    if (f.status === "fixed") cls = "done";
    else if (failedAt !== undefined) cls = i < failedAt ? "done" : i === failedAt ? "fail" : "";
    else if (now >= 0) cls = i < now ? "done" : i === now ? "now" : "";
    return `<li class="${cls}"><b>${t}</b>${s}</li>`;
  }).join("");
}

function renderChips(shown) {
  $("#inc-chips").innerHTML = shown.map((r) => {
    const good = r.kind === "proof" ? r.outcome === "avoided" : r.outcome === "reproduced" && r.matches_live;
    const label = r.status !== "done" ? `#${r.id} ${r.kind} · ${r.status}…`
      : r.kind === "proof" ? `#${r.id} proof under v${r.policy_version} · ${r.outcome === "avoided" ? "clean" : r.outcome}`
      : `#${r.id} ${r.matches_live ? "hash = live" : "DIVERGED"} · ${short(r.trajectory_hash)}`;
    return `<span class="chip replay ${r.status === "done" ? (good ? "good" : "badr") : ""} ${selected === r.id ? "sel" : ""}" data-id="${r.id}">${esc(label)}</span>`;
  }).join("") || `<span class="hint">waiting for a replay worker…</span>`;
  for (const c of $$("#inc-chips .chip.replay")) {
    c.onclick = () => { const r = shown.find((x) => x.id === Number(c.dataset.id)); if (r && r.id in frames) { selectReplay(r); renderChips(shown); } };
  }
}

function renderHyps(d) {
  const hyps = latestRound(d.hypotheses);
  const trialOf = (h) => d.replays.filter((r) => r.hypothesis_id === h.id && r.kind === "trial").at(-1);
  const sig = JSON.stringify(hyps.map((h) => [h.id, h.status, h.gate, trialOf(h)?.status, trialOf(h)?.id in frames]));
  const src = hyps[0]?.source || "";
  const [prov, model] = src.split(":");
  const who = { playbook: "the rule playbook", vultr: "the investigator · Vultr Serverless Inference", scripted: "the scripted investigator" }[prov] || prov;
  $("#inc-source").textContent = hyps.length ? `proposed by ${who}${model ? ` · ${model}` : ""}` : "";
  if (sig === hypSig) return;
  hypSig = sig;
  viewer.clearFollowers();
  if (!hyps.length) {
    $("#inc-hyps").innerHTML = `<div class="empty">${d.failure.status === "diagnosing" ? "The investigator is working…" : "Fixes appear once the incident reproduces."}</div>`;
    return;
  }
  const STAT = { trial: ["replaying", "info live"], failed: ["failed the gate", "bad"], regression: ["regression check", "info live"],
    ready: ["ready to ship", "ok"], rejected: ["broke the suite", "bad"], approved: ["shipped", "ok"], superseded: ["superseded", ""] };
  $("#inc-hyps").innerHTML = hyps.map((h, i) => {
    const t = trialOf(h), g = h.gate || {}, reg = g.regression, ev = h.evidence || {};
    const rob = g.robustness ?? ev.robustness, costPct = g.cost_pct ?? ev.cost_pct, bar = g.min_robustness ?? 0.95;
    const gate = [
      rob != null ? `stress test: ${Math.round(rob * 100)}% of ${ev.placed ?? ""} variants safe (gate ${Math.round(bar * 100)}%)${costPct != null ? ` · throughput ${costPct > 0 ? "−" : "+"}${Math.abs(costPct).toFixed(1)}%` : ""}` : "",
      g.reason ? `rejected: ${g.reason}` : "",
      h.status === "regression" ? `regression suite: ${h.regression_done}/${(g.suite || []).length} checked…` : "",
      reg ? `regression suite: ${reg.passed}/${reg.total} clean${reg.total ? ` under v${g.policy_version}` : " (empty)"}` : "",
      t && t.new_failures && t.new_failures.length ? `new failures: ${t.new_failures.map((x) => `${x.type} ${x.robot}`).join(", ")}` : "",
      t && t.warnings && t.warnings.length ? `warning after the recording: ${t.warnings.map((x) => `${x.type} ${x.robot}`).join(", ")}` : "",
    ].filter(Boolean).map(esc).join("<br>");
    return `<div class="hyp ${h.status}" style="animation-delay:${i * 70}ms">
      <div class="row between"><b>#${h.rank}</b>${pill(...(STAT[h.status] || [h.status, ""]))}</div>
      <div class="fix">${esc(h.fix_dsl)}</div>
      <div class="cause" title="${esc(h.cause)}">${esc(h.cause)}</div>
      <div class="hint">${esc(h.rationale || "")}</div>
      <canvas data-rid="${t ? t.id : ""}"></canvas>
      <div class="row between"><span>trial: ${t ? outcomePill(t.status === "done" ? t.outcome : null) : pill("queued")}</span>
        ${h.status === "ready" ? `<button class="primary approve" data-id="${h.id}">Approve → ship</button>` : ""}</div>
      <div class="gate">${gate}</div></div>`;
  }).join("");
  for (const c of $$("#inc-hyps canvas")) {
    viewer.follow(c, c.dataset.rid ? frames[c.dataset.rid] : null);
    c.title = "Compare with what happened live";
    c.onclick = () => {     // load this fix's forked run into the main viewer, overlaid on the live recording
      const rid = Number(c.dataset.rid), t = d.replays.find((r) => r.id === rid);
      if (!t || !(rid in frames)) return;
      const h = hyps.find((x) => x.id === t.hypothesis_id);
      selected = rid;
      viewer.setReplay(frames[rid], `Fix trial #${rid}: ${h ? h.fix_dsl : ""}`, `${t.outcome || ""} · ${t.worker || "VM B"}`);
      viewer.setGhost(true);
      $("#inc-viewer").scrollIntoView({ behavior: "smooth", block: "center" });
      toast("Solid = what happened live · outline = the same moment with the fix", "ok", 4000);
    };
  }
  for (const b of $$("#inc-hyps button.approve")) {
    b.onclick = async () => {
      b.disabled = true;
      const r = await act(() => api(`/api/hypotheses/${b.dataset.id}/approve`, { body: {} }), (x) => `Haul rules v${x.result.version} are live on the fleet`);
      if (!r) b.disabled = false;
      reload();
    };
  }
}

function renderChain(chain) {
  $("#inc-chain").innerHTML = chain.map((e) => `<li><span class="t">${e.tick ?? ""}</span><span class="ty">${esc(e.type)}</span><span>${esc(summarize(e))}</span></li>`).join("");
}

export function currentId() { return fid; }
