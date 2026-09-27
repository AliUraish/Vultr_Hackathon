// Regression suite: every approved fix leaves its incident behind as a test.
import { $, $$, esc, api, act, FAILURE_LABEL, SCENARIO_LABEL, short, debounce } from "../util.js";
import { robotColor } from "../floor.js";

export function mount(root) {
  root.innerHTML = `
    <div class="panel">
      <div class="panel-head">
        <div><h2>Regression suite</h2>
          <div class="hint">Every approved fix leaves its capsule here. A new fix must keep all of them clean before it can ship.</div></div>
        <button class="primary" id="suite-run">Re-check all against current policy</button>
      </div>
      <div class="suite" id="suite"></div>
    </div>`;
  $("#suite-run").onclick = () => act(() => api("/api/regression/run", { body: {} }), (r) => `${r.queued} capsules queued on VM B`).then(() => setTimeout(show, 1200));
}

export const refresh = debounce(() => show(), 500);

export async function show() {
  const rows = await api("/api/regression");
  $("#suite").innerHTML = rows.length ? rows.map((k, i) => {
    const l = k.latest, light = !l ? "" : l.status !== "done" ? "run" : l.outcome === "avoided" ? "ok" : "bad";
    return `<div class="card" data-f="${k.failure_id}" style="animation-delay:${i * 50}ms">
      <div class="row between"><div class="row"><span class="light ${light}"></span><b>capsule #${k.id}</b></div><span class="mono dim">${short(k.hash)}</span></div>
      <div>${esc(FAILURE_LABEL[k.type] || k.type)} <span style="color:${robotColor(k.robot_id)}">${esc(k.robot_id)}</span> · <span class="muted">${esc(SCENARIO_LABEL[k.scenario] || k.scenario || "organic")}</span></div>
      <div><code>${esc(k.fix_dsl || "–")}</code></div>
      <div class="hint">fixed in v${k.fixed_in ?? "–"}${l ? ` · last check: ${l.status === "done" ? l.outcome : "running"} under v${l.policy_version}` : ""}</div>
    </div>`;
  }).join("") : `<div class="empty">Empty. Approving a fix adds its capsule here.</div>`;
  for (const c of $$("#suite .card")) c.onclick = () => (location.hash = `#/incident/${c.dataset.f}`);
}
