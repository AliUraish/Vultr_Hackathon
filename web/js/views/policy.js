// Fleet policy history: each version is the previous one plus one approved fix.
import { $, esc, api, short, pill } from "../util.js";

export function mount(root) {
  root.innerHTML = `
    <div class="panel">
      <div class="panel-head"><div><h2>Fleet policy</h2>
        <div class="hint">Each version is the previous one plus one proven, approved fix, signed by the control plane. VM B verifies the signature before the robots hot-reload it on the next tick.</div></div></div>
      <div class="versions" id="versions"></div>
    </div>`;
}

export async function show() {
  const rows = await api("/api/policy");
  $("#versions").innerHTML = rows.map((p, i) => `
    <div class="version ${i === 0 ? "current" : ""}" style="animation-delay:${i * 60}ms">
      <div class="row between"><h4>v${p.version} ${i === 0 ? '<span class="pill info">live on the fleet</span>' : ""}</h4>
        <span class="row">${p.signature ? pill("signed · Ed25519", "ok") : pill("unsigned", "warn")}<span class="mono dim" title="${esc(p.signature || "")}">${p.key_id ? `key ${esc(p.key_id)}` : ""} · ${short(p.hash)}</span></span></div>
      <div class="hint">${p.fix_dsl ? `added <code>${esc(p.fix_dsl)}</code> · approved by ${esc(p.approved_by || "")} from capsule #${p.source_capsule_id ?? "–"}` : "base policy"}
        · ${new Date(p.created_at).toLocaleString()}</div>
      <div class="rules">${p.rules.length ? p.rules.map((r) => `<code class="${r === p.fix_dsl ? "new" : ""}">${esc(r)}</code>`).join("") : '<span class="dim">no rules</span>'}</div>
    </div>`).join("");
}
