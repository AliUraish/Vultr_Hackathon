// Audit log with a rewind map: every tick of the fleet is in Postgres on VM A.
import { $, $$, esc, api, summarize, debounce, cfg, bus, short, toast, pill } from "../util.js";
import { Floor } from "../floor.js";

let floor = null, before = null, newest = 0, tail = null;

export function mount(root) {
  root.innerHTML = `
    <div class="panel ledger">
      <div class="panel-head"><div><h3>Audit ledger</h3>
        <div class="hint">Every event is sealed into Merkle-rooted blocks chained by hash; VM B keeps an independent copy of every block head. Editing, deleting or reordering any past event is detectable.</div></div>
        <div class="row"><span class="hint" id="led-stat"></span><button class="primary" id="led-verify">Verify integrity</button></div></div>
      <div class="chainviz" id="led-chain"></div>
      <div id="led-result"></div>
    </div>
    <div class="log-layout">
      <div class="panel">
        <div class="panel-head"><h3>Rewind the pit</h3><span class="tag" id="log-tick">t –</span></div>
        <div class="stage"><canvas id="log-canvas"></canvas></div>
        <input type="range" class="range" id="log-scrub" min="0" max="0" value="0" style="margin-top:12px">
        <div class="hint">Every tick of the fleet is stored in Postgres on VM A. Drag to any moment, or click an event.</div>
      </div>
      <div class="panel">
        <div class="panel-head"><h3>Event log</h3>
          <div class="row">
            <label class="hint row"><input type="checkbox" id="log-tail" checked> live</label>
            <select id="log-filter">
              <option value="">everything</option><option value="failure">failures</option><option value="capsule">capsules</option>
              <option value="replay">replays</option><option value="investigation">investigation</option><option value="experiment">experiments</option>
              <option value="hypothesis">hypotheses</option><option value="approval">approvals</option><option value="policy">policy</option>
              <option value="dispatch">dispatch</option><option value="input.chaos">chaos</option><option value="sim.">sim events</option>
            </select></div></div>
        <ol class="log" id="log-list"></ol>
        <button id="log-more">Older</button>
      </div>
    </div>`;
  floor = new Floor($("#log-canvas"), cfg.map, { trails: false, paths: true });
  $("#log-scrub").oninput = showFrame;
  $("#log-filter").onchange = () => load(true);
  $("#log-more").onclick = () => load(false);
  $("#led-verify").onclick = verify;
}

const row = (e, fresh) => `<li class="${esc(e.type.split(".")[0])}${fresh ? " new" : ""}" data-tick="${e.tick ?? ""}" data-id="${e.id}">
  <span class="dim">#${e.id}</span><span>${e.tick ?? ""}</span><span class="ty">${esc(e.type)}</span><span class="p">${esc(summarize(e))}</span></li>`;

function wire() {
  for (const li of $$("#log-list li")) {
    li.onclick = () => { if (li.dataset.tick) { $("#log-scrub").value = li.dataset.tick; showFrame(); } showProof(Number(li.dataset.id)); };
  }
}

async function showProof(id) {
  const el = $("#led-result");
  try {
    const p = await api(`/api/audit/proof/${id}`);
    el.innerHTML = `<div class="proof ${p.valid ? "ok" : "bad"}">
      <b>Event #${p.event}</b> is sealed as #${p.seq} in block ${p.block} ${p.valid ? pill("inclusion proof valid", "ok") : pill("proof FAILS", "bad")} ${p.witnessed ? pill("witnessed by VM B", "info") : ""}
      <div class="path mono"><span title="leaf">${short(p.leaf)}</span>${p.path.map(([side, h]) => `<i>${side === "L" ? "⟵" : "⟶"}</i><span title="${h}">${short(h)}</span>`).join("")}<i>=</i><span class="root" title="Merkle root">${short(p.merkle_root)}</span></div>
      <div class="hint">leaf = sha256 of the event's canonical content; ${p.path.length} sibling hashes lead to the block's Merkle root.</div></div>`;
  } catch (e) { el.innerHTML = `<div class="hint">#${id}: ${esc(e.message)} (blocks seal every few seconds)</div>`; }
}

async function loadLedger() {
  try {
    const a = await api("/api/audit");
    $("#led-stat").textContent = `${a.total_blocks} blocks · ${a.unsealed} waiting to seal · policies ${a.signing.mode}`;
    $("#led-chain").innerHTML = a.blocks.slice().reverse().map((b) => blockChip(b)).join("");
  } catch { /* next time */ }
}

const blockChip = (b, fresh) => `<div class="blk ${b.witnessed ? "w" : ""} ${fresh ? "fresh" : ""}" title="block ${b.n}: seq ${b.first_seq}-${b.last_seq}\nroot ${b.merkle_root}\nprev ${b.prev_hash}\nhash ${b.hash}">
  <b>#${b.n}</b><span>${b.events} ev</span><span class="mono">${short(b.hash).slice(0, 8)}</span></div>`;

bus.on("ledger", (b) => {
  const el = $("#led-chain");
  if (!el) return;
  el.insertAdjacentHTML("beforeend", blockChip(b, true));
  while (el.children.length > 14) el.firstElementChild.remove();
});

async function verify() {
  const btn = $("#led-verify"), el = $("#led-result");
  btn.disabled = true; btn.textContent = "Verifying…";
  el.innerHTML = `<div class="skeleton" style="height:40px"></div>`;
  try {
    const r = await api("/api/audit/verify", { body: {} });
    const w = r.witness;
    el.innerHTML = `<div class="verify ${r.ok ? "ok" : "bad"}">
      <b>${r.ok ? "✓ Intact" : `✕ ${r.problem_count} problem(s)`}</b>
      <span>${r.blocks} blocks · ${Number(r.events).toLocaleString()} events re-hashed · every Merkle root and block link recomputed in ${r.ms} ms</span>
      <span>${w.reachable ? `VM B witness: ${w.matching}/${w.checked} block heads match` : `VM B witness unreachable`}</span>
      <span class="mono dim">head ${esc(r.head)}</span>
      ${r.problems.map((p) => `<div class="bad">${esc(p)}</div>`).join("")}</div>`;
  } catch (e) { toast(e.message, "bad"); el.innerHTML = ""; }
  btn.disabled = false; btn.textContent = "Verify integrity";
}

async function load(reset) {
  const q = new URLSearchParams({ limit: "150" });
  if ($("#log-filter").value) q.set("type", $("#log-filter").value);
  if (!reset && before) q.set("before", before);
  const rows = await api(`/api/events?${q}`);
  if (reset) { $("#log-list").innerHTML = ""; newest = rows.length ? rows[0].id : 0; }
  if (rows.length) before = rows[rows.length - 1].id;
  $("#log-list").insertAdjacentHTML("beforeend", rows.map((e) => row(e, false)).join(""));
  wire();
}

async function poll() {
  if (!$("#log-tail") || !$("#log-tail").checked || !newest) return;
  const q = new URLSearchParams({ limit: "100" });
  if ($("#log-filter").value) q.set("type", $("#log-filter").value);
  const rows = (await api(`/api/events?${q}`)).filter((e) => e.id > newest);
  if (!rows.length) return;
  newest = rows[0].id;
  $("#log-list").insertAdjacentHTML("afterbegin", rows.map((e) => row(e, true)).join(""));
  wire();
}

const showFrame = debounce(async () => {
  const t = Number($("#log-scrub").value);
  $("#log-tick").innerHTML = `t <b>${t}</b> · ${(t / cfg.tickHz).toFixed(1)} s`;
  try { const f = await api(`/api/frame?tick=${t}`); floor.resetTrails(); floor.draw(f, null, 0, performance.now(), { label: `t ${t}` }); }
  catch (e) { floor.draw(null, null, 0, performance.now(), { empty: e.message }); }
}, 50);

export async function show() {
  loadLedger();
  await load(true);
  const tl = await api("/api/timeline");
  if (tl.first != null) {
    const s = $("#log-scrub"); s.min = tl.first; s.max = tl.last;
    if (!s.dataset.set) { s.value = tl.last; s.dataset.set = "1"; }
    showFrame();
  } else floor.draw(null, null, 0, performance.now(), { empty: "no telemetry yet" });
  clearInterval(tail); tail = setInterval(() => poll().catch(() => {}), 2000);
}

export function hide() { clearInterval(tail); }
