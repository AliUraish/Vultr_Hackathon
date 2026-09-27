// Incident inbox.
import { $, $$, esc, api, secs, statusPill, FAILURE_LABEL, SCENARIO_LABEL, countTo } from "../util.js";
import { robotColor } from "../floor.js";

const SEV = { collision: "", zone_breach: "", wrong_item: "warn", stall: "info", task_overdue: "info" };

export function mount(root) {
  root.innerHTML = `
    <div class="panel">
      <div class="panel-head">
        <div><h2>Incidents</h2>
          <div class="hint">Every failure the safety rules catch is recorded exactly, replayed, investigated and tested against fixes. You approve.</div></div>
        <div class="row wrap">
          <span class="chip">open <b id="inc-open" data-v="0">0</b></span>
          <span class="chip">fixed <b id="inc-fixed" data-v="0">0</b></span>
          <span class="chip">exact replays <b id="inc-exact" data-v="0">0</b></span>
        </div>
      </div>
      <table class="grid">
        <thead><tr><th></th><th>#</th><th>Incident</th><th>When</th><th>Cause</th><th>Evidence</th><th>Status</th><th></th></tr></thead>
        <tbody id="inc-rows"><tr><td colspan="8"><div class="skeleton"></div></td></tr></tbody>
      </table>
    </div>`;
}

export async function show() {
  const rows = await api("/api/failures");
  const open = rows.filter((f) => !["fixed", "dismissed", "lost"].includes(f.status)).length;
  countTo($("#inc-open"), open);
  countTo($("#inc-fixed"), rows.filter((f) => f.status === "fixed").length);
  countTo($("#inc-exact"), rows.reduce((n, f) => n + Number(f.reproduced || 0), 0));
  $("#inc-rows").innerHTML = rows.length ? rows.map((f, i) => `
    <tr class="link" data-id="${f.id}" style="animation-delay:${Math.min(i, 12) * 30}ms">
      <td style="width:14px"><span class="sev ${SEV[f.type] ?? ""}"></span></td>
      <td class="mono dim">#${f.id}</td>
      <td><b>${esc(FAILURE_LABEL[f.type] || f.type)}</b> <span class="mono" style="color:${robotColor(f.robot_id)}">${esc(f.robot_id)}</span></td>
      <td class="mono">t${f.tick} <span class="dim">${secs(f.tick)}s</span></td>
      <td>${esc(SCENARIO_LABEL[f.scenario] || f.scenario || "organic")}</td>
      <td class="mono">${f.replayed ? `${f.reproduced}/${f.replayed} exact` : "–"}</td>
      <td>${statusPill(f.status)}</td>
      <td class="dim">→</td></tr>`).join("")
    : `<tr><td colspan="8"><div class="empty">No incidents yet. Inject one from the Floor.</div></td></tr>`;
  for (const tr of $$("#inc-rows tr.link")) tr.onclick = () => (location.hash = `#/incident/${tr.dataset.id}`);
}
