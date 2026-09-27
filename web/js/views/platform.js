// Platform: how the Vultr deployment is used, measured live, and what scaling out buys.
import { $, esc, api, countTo, pill, money } from "../util.js";

let timer = null, snap = null, nodes = 1;

const bytes = (b) => (b > 1e9 ? `${(b / 1e9).toFixed(2)} GB` : b > 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${Math.round(b / 1e3)} KB`);
const up = (s) => (s == null ? "–" : s > 86400 ? `${(s / 86400).toFixed(1)} d` : s > 3600 ? `${(s / 3600).toFixed(1)} h` : `${Math.round(s / 60)} min`);
const bar = (v, cls = "") => `<div class="g small ${cls}"><i style="--w:${Math.round(Math.min(1, v || 0) * 100)}%"></i></div>`;

export function mount(root) {
  root.innerHTML = `
    <div class="panel glow">
      <div class="panel-head"><div><h2>Platform</h2>
        <div class="hint">Two Vultr VMs, measured live. VM A is the system of record and control; VM B runs the fleet and a pool of stateless workers that claim jobs from Postgres. Scale out by adding VM B nodes.</div></div>
        <span class="hint" id="pf-at"></span></div>
      <svg class="topo" id="pf-topo" viewBox="0 0 1000 380" preserveAspectRatio="xMidYMid meet">
        <defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" fill="currentColor"/></marker></defs>
        <g class="node user"><rect x="10" y="150" width="150" height="84" rx="12"/><text x="85" y="182" class="t">Operators</text>
          <text x="85" y="202" class="s" id="pf-ws">– browsers</text><text x="85" y="220" class="s">HTTPS + WebSocket</text></g>
        <g class="node vma"><rect x="200" y="20" width="320" height="346" rx="14"/>
          <text x="218" y="46" class="t l">VM A · control plane</text><text x="502" y="46" class="s r">vc2-1c-2gb</text>
          <g class="svc"><rect x="218" y="62" width="284" height="38" rx="8"/><text x="232" y="86" class="l">FastAPI · orchestrator · dispatcher</text></g>
          <g class="svc ai"><rect x="218" y="108" width="284" height="38" rx="8"/><text x="232" y="132" class="l">Investigator · experiment lab</text></g>
          <g class="svc db"><rect x="218" y="154" width="284" height="56" rx="8"/><text x="232" y="176" class="l">Postgres 16 · system of record</text>
            <text x="232" y="197" class="s l" id="pf-db">–</text></g>
          <g class="svc sec"><rect x="218" y="218" width="284" height="56" rx="8"/><text x="232" y="240" class="l">Audit ledger · Ed25519 signing</text>
            <text x="232" y="261" class="s l" id="pf-ledger">–</text></g>
          <text x="218" y="304" class="s l" id="pf-a-host">–</text><text x="218" y="324" class="s l" id="pf-a-ev">–</text>
          <text x="218" y="344" class="s l" id="pf-a-up">–</text></g>
        <g class="node vmb"><rect x="680" y="20" width="310" height="236" rx="14"/>
          <text x="698" y="46" class="t l">VM B · fleet + workers</text><text x="972" y="46" class="s r">vc2-2c-2gb</text>
          <g class="svc sim"><rect x="698" y="62" width="274" height="46" rx="8"/><text x="712" y="82" class="l">Live sim · 4 robots · 10 Hz</text>
            <text x="712" y="99" class="s l" id="pf-tick">–</text></g>
          <g id="pf-workers"></g>
          <text x="698" y="240" class="s l" id="pf-b-host">–</text></g>
        <g class="node ext"><rect x="680" y="282" width="310" height="84" rx="14"/>
          <text x="698" y="306" class="t l">Inference</text><text x="698" y="326" class="s l" id="pf-llm">–</text>
          <text x="698" y="346" class="s l">Vultr Serverless Inference: one setting away</text></g>
        <path class="flow f-ws" id="fl-ws" d="M160 192 H200" marker-end="url(#arr)"/>
        <path class="flow f-tel" id="fl-tel" d="M680 92 H520" marker-end="url(#arr)"/>
        <path class="flow f-cmd" id="fl-cmd" d="M520 134 H680" marker-end="url(#arr)"/>
        <path class="flow f-job" id="fl-job" d="M520 190 C600 190 610 170 680 170" marker-end="url(#arr)"/>
        <path class="flow f-llm" id="fl-llm" d="M520 300 C600 300 600 322 680 322" marker-end="url(#arr)"/>
        <text x="600" y="82" class="s c" id="pf-tel">telemetry</text>
        <text x="600" y="125" class="s c">fleet API · signed policy</text>
        <text x="600" y="214" class="s c" id="pf-claims">jobs</text>
        <text x="600" y="292" class="s c">model calls</text>
      </svg>
      <div class="legend"><span><i style="background:var(--accent)"></i>telemetry (every tick)</span><span><i style="background:var(--ok)"></i>dispatch, chaos, signed policy</span>
        <span><i style="background:var(--accent-2)"></i>replay + experiment jobs (SKIP LOCKED queue)</span><span><i style="background:var(--warn)"></i>model calls</span></div>
    </div>
    <div class="pf-grid">
      <div class="panel"><div class="panel-head"><h3>Worker pool · VM B</h3><span class="hint">last 15 min</span></div>
        <table class="grid"><thead><tr><th>worker</th><th>jobs</th><th>sims</th><th>sims/s busy</th><th>utilization</th><th>seen</th></tr></thead><tbody id="pf-wk"></tbody></table></div>
      <div class="panel"><div class="panel-head"><h3>Job queue</h3><span class="hint">Postgres · priority + SKIP LOCKED</span></div>
        <table class="grid"><thead><tr><th>kind</th><th>queued</th><th>running</th><th>done 15m</th><th>p50</th><th>p95</th><th>wait p95</th></tr></thead><tbody id="pf-q"></tbody></table></div>
      <div class="panel cap"><div class="panel-head"><h3>Capacity model</h3><span class="hint">from measured worker throughput</span></div>
        <div id="pf-cap" class="hint">Run an investigation to measure throughput.</div></div>
      <div class="panel"><div class="panel-head"><h3>Integrity</h3></div><div id="pf-int" class="kvs"></div></div>
    </div>`;
}

export async function show() {
  clearInterval(timer);
  await refresh();
  timer = setInterval(refresh, 3000);
}

export function hide() { clearInterval(timer); }

async function refresh() {
  try { snap = await api("/api/platform"); } catch { return; }
  const s = snap, a = s.vm_a, b = s.vm_b;
  $("#pf-at").textContent = `updated ${new Date(s.at * 1000).toLocaleTimeString()}`;
  $("#pf-ws").textContent = `${a.ws_clients} live browser${a.ws_clients === 1 ? "" : "s"}`;
  const t = a.tables || {};
  $("#pf-db").textContent = `${bytes(a.db_bytes)} · ${Number(t.events || 0).toLocaleString()} events · ${Number(t.ticks || 0).toLocaleString()} ticks`;
  $("#pf-ledger").textContent = `${s.ledger.blocks} blocks · ${s.ledger.witnessed} witnessed by VM B · ${s.signing.mode}`;
  const ha = a.host || {}, hb = b.host || {};
  $("#pf-a-host").textContent = `load ${(ha.load || [])[0] ?? "–"} · ${ha.cpus} vCPU · mem ${ha.mem_used_pct ?? "–"}% of ${ha.mem_total_mb ?? "–"} MB`;
  $("#pf-a-ev").textContent = `${a.events_per_min} events/min into the log`;
  $("#pf-a-up").textContent = `control plane up ${up(a.uptime_s)} · ${money(a.usd_per_hour, 3)}/h`;
  $("#pf-tick").textContent = b.online ? `tick ${hb.tick_ms_p50 ?? "–"} ms p50 · ${hb.tick_ms_p99 ?? "–"} ms p99 · budget 100 ms` : "offline";
  $("#pf-b-host").textContent = `load ${(hb.load || [])[0] ?? "–"} · ${hb.cpus ?? "–"} vCPU · mem ${hb.mem_used_pct ?? "–"}% · ${money(b.usd_per_hour, 3)}/h`;
  $("#pf-tel").textContent = `telemetry ${b.ticks_per_s} ticks/s`;
  $("#pf-claims").textContent = `jobs · ${s.claims_per_s} claims/s`;
  const inf = s.inference;
  $("#pf-llm").textContent = `${inf.provider} · ${inf.model} · ${inf.investigations} runs · ${((inf.tokens_in + inf.tokens_out) / 1000).toFixed(1)}k tokens`;
  speed("#fl-tel", b.ticks_per_s / 10);
  speed("#fl-job", Math.min(1, s.claims_per_s / 4 + s.queue.reduce((n, q) => n + Number(q.running), 0) * 0.5));
  speed("#fl-cmd", b.online ? 0.4 : 0);
  speed("#fl-ws", a.ws_clients ? 0.6 : 0);
  speed("#fl-llm", s.queue.some((q) => ["variants", "isolate", "verify", "whatif"].includes(q.kind) && Number(q.running)) ? 0.8 : 0.08);
  renderWorkers(s.workers);
  renderQueue(s.queue);
  renderCapacity(s);
  renderIntegrity(s);
}

function speed(sel, k) {
  const el = $(sel);
  if (!el) return;
  el.classList.toggle("idle", k <= 0.01);
  el.style.animationDuration = `${Math.max(0.35, 2.4 - 2 * Math.min(1, k))}s`;
}

function renderWorkers(ws) {
  const g = $("#pf-workers");
  const shown = ws.filter((w) => w.last_seen_s != null && w.last_seen_s < 30).slice(0, 4);
  g.innerHTML = shown.map((w, i) => {
    const y = 118 + i * 28, u = w.utilization || 0;
    return `<g class="svc wk ${u > 0.02 ? "busy" : ""}"><rect x="698" y="${y}" width="274" height="24" rx="6"/>
      <rect class="u" x="698" y="${y}" width="${Math.max(2, 274 * u)}" height="24" rx="6"/>
      <text x="712" y="${y + 16}" class="s l">${esc(w.worker)} · ${w.jobs} jobs · ${Math.round(u * 100)}% busy</text></g>`;
  }).join("") || `<text x="712" y="136" class="s l">no workers claiming</text>`;
  $("#pf-wk").innerHTML = ws.map((w) => `<tr><td class="mono">${esc(w.worker)}</td><td>${w.jobs}</td><td>${w.sims}</td>
    <td>${w.sims_per_busy_s ?? "–"}</td><td>${bar(w.utilization)} <span class="hint">${Math.round((w.utilization || 0) * 100)}%</span></td>
    <td class="hint">${w.last_seen_s != null ? `${w.last_seen_s}s ago` : "–"}</td></tr>`).join("") || `<tr><td colspan="6" class="hint">no workers yet</td></tr>`;
}

function renderQueue(q) {
  const ms = (v) => (v == null ? "–" : v > 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`);
  $("#pf-q").innerHTML = q.map((r) => `<tr><td class="mono">${esc(r.kind)}</td><td>${r.queued}</td><td>${Number(r.running) ? pill(r.running, "info live") : 0}</td>
    <td>${r.done}</td><td>${ms(r.p50_ms)}</td><td>${ms(r.p95_ms)}</td><td>${ms(r.wait_p95_ms)}</td></tr>`).join("")
    || `<tr><td colspan="7" class="hint">no jobs in the last 6 hours</td></tr>`;
}

function renderCapacity(s) {
  const c = s.capacity, el = $("#pf-cap");
  if (!c) return;
  if (!el.querySelector("input")) {
    el.innerHTML = `<div class="row between"><span>VM B nodes</span><b id="cap-n">1</b></div>
      <input type="range" class="range" id="cap-range" min="1" max="10" value="${nodes}">
      <div class="cap-out" id="cap-out"></div>
      <div class="hint">Workers are stateless (they cache capsules and claim with SKIP LOCKED), so a new VM B with the same node token adds capacity with no other change. The live sim stays on one node; experiments fan out.</div>`;
    $("#cap-range").oninput = (e) => { nodes = Number(e.target.value); drawCap(); };
  }
  drawCap();
}

function drawCap() {
  const c = snap && snap.capacity;
  if (!c) return;
  const extra = nodes - 1, rate = c.sims_per_s + extra * c.per_extra_node.sims_per_s;
  const cost = snap.vm_a.usd_per_hour + nodes * c.per_extra_node.usd_per_hour;
  $("#cap-n").textContent = nodes;
  $("#cap-out").innerHTML = `
    <div><b>${rate.toFixed(0)}</b><span>simulations / s</span></div>
    <div><b>${(30 / rate).toFixed(1)} s</b><span>30-variant stress test</span></div>
    <div><b>${(270 / rate).toFixed(1)} s</b><span>9-point tune</span></div>
    <div><b>${(600 / rate).toFixed(0)} s</b><span>sim time per investigation</span></div>
    <div><b>${c.workers + extra * c.per_extra_node.workers}</b><span>workers</span></div>
    <div><b>${money(cost, 3)}</b><span>per hour, both tiers</span></div>`;
}

function renderIntegrity(s) {
  const sg = s.signing, fleet = sg.fleet || {};
  $("#pf-int").innerHTML = `
    <div><span>policy signing</span><b>${sg.mode === "ed25519" ? pill("Ed25519", "ok") : pill("unsigned", "warn")}</b><span class="mono dim">${esc(sg.key_id || "")}</span></div>
    <div><span>fleet verifies</span><b>${fleet.mode === "ed25519" ? pill("yes", "ok") : pill(fleet.mode || "unknown", "warn")}</b><span class="hint">${fleet.rejected ? `${fleet.rejected} rejected` : "no rejected policies"}</span></div>
    <div><span>audit ledger</span><b>${s.ledger.blocks} blocks</b><span class="hint">${Number(s.ledger.sealed).toLocaleString()} events sealed · ${s.ledger.witnessed} witnessed by VM B</span></div>
    <div><span>simulations run</span><b>${Number(s.sims_total).toLocaleString()}</b><span class="hint">every one stored with its result</span></div>`;
}
