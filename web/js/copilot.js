// Operations copilot chat: ask about one robot or the whole site. Answers come from live data on VM A.
import { $, esc, api } from "./util.js";

const SUGGEST = {
  robot: (id) => [`What is ${id} doing right now?`, "Why did it stop last?", "What's on board and where is it going?", "Any risk ahead on its route?"],
  site: () => ["Where is traffic congested right now?", "Summarize the last standoffs and who yielded", "Which robot is busiest, and why?", "How is throughput today?"],
};

const threads = new Map();   // "robot:R2" | "site" -> [{role, text, meta}]

function fmt(text) {
  return esc(text)
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/^\s*[-•]\s+(.+)$/gm, "<li>$1</li>")
    .replace(/(<li>.*<\/li>\n?)+/gs, (m) => `<ul>${m}</ul>`)
    .replace(/\n{2,}/g, "<br><br>").replace(/\n/g, "<br>");
}

export class Copilot {
  constructor(root) {
    this.root = root; this.scope = "site"; this.robot = null; this.busy = false;
    root.innerHTML = `
      <div class="cp-thread" id="cp-thread"></div>
      <div class="cp-suggest" id="cp-suggest"></div>
      <form class="cp-form" id="cp-form" autocomplete="off">
        <div class="cp-scope" id="cp-scope"></div>
        <textarea id="cp-input" rows="1" maxlength="600" placeholder="Ask anything…"></textarea>
        <button class="primary cp-send" id="cp-send" title="Send (Enter)">↑</button>
      </form>`;
    const input = $("#cp-input", root);
    input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = `${Math.min(120, input.scrollHeight)}px`; });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); this.send(); } });
    $("#cp-form", root).onsubmit = (e) => { e.preventDefault(); this.send(); };
  }

  key() { return this.scope === "robot" ? `robot:${this.robot}` : "site"; }

  setScope(scope, robot = this.robot) {
    this.scope = scope; this.robot = robot;
    const sc = $("#cp-scope", this.root);
    sc.innerHTML = `${robot ? `<button type="button" data-s="robot" class="${scope === "robot" ? "on" : ""}">${esc(robot)}</button>` : ""}
      <button type="button" data-s="site" class="${scope === "site" ? "on" : ""}">Whole site</button>`;
    for (const b of sc.querySelectorAll("button")) b.onclick = () => this.setScope(b.dataset.s);
    $("#cp-input", this.root).placeholder = scope === "robot" ? `Ask about ${robot}…` : "Ask about the whole site…";
    this.render();
  }

  focus() { $("#cp-input", this.root).focus(); }

  render() {
    const msgs = threads.get(this.key()) || [];
    const th = $("#cp-thread", this.root);
    th.innerHTML = msgs.length ? msgs.map((m) => this.bubble(m)).join("") : `<div class="cp-empty">
      <div class="cp-orb">✦</div><b>${this.scope === "robot" ? `Ask about ${esc(this.robot)}` : "Ask about the site"}</b>
      <span>Answers are grounded in live telemetry, the event log, orders and the fleet policy. The copilot explains; it can't move robots.</span></div>`;
    th.scrollTop = th.scrollHeight;
    const sug = (this.scope === "robot" ? SUGGEST.robot(this.robot) : SUGGEST.site());
    $("#cp-suggest", this.root).innerHTML = msgs.length > 2 ? "" : sug.map((q) => `<button type="button">${esc(q)}</button>`).join("");
    for (const b of this.root.querySelectorAll("#cp-suggest button")) b.onclick = () => { $("#cp-input", this.root).value = b.textContent; this.send(); };
  }

  bubble(m) {
    if (m.role === "user") return `<div class="cp-msg user"><div>${esc(m.text)}</div></div>`;
    if (m.pending) return `<div class="cp-msg bot pending"><span class="av">✦</span><div><span class="dots"><i></i><i></i><i></i></span><span class="hint">reading live telemetry…</span></div></div>`;
    return `<div class="cp-msg bot ${m.error ? "err" : ""}"><span class="av">✦</span><div><div class="txt">${fmt(m.text)}</div>${m.meta ? `<div class="meta">${esc(m.meta)}</div>` : ""}</div></div>`;
  }

  async send() {
    const input = $("#cp-input", this.root), q = input.value.trim();
    if (!q || this.busy) return;
    const key = this.key(), scope = this.scope, robot = this.robot;
    const msgs = threads.get(key) || [];
    threads.set(key, msgs);
    const history = msgs.filter((m) => !m.pending && !m.error).slice(-6).map((m) => ({ role: m.role, text: m.text }));
    msgs.push({ role: "user", text: q }, { role: "bot", pending: true });
    input.value = ""; input.style.height = "auto";
    this.busy = true; $("#cp-send", this.root).disabled = true;
    this.render();
    try {
      const r = await api("/api/ask", { body: { question: q, scope, robot: scope === "robot" ? robot : null, history } });
      const [prov, model] = String(r.source).split(":");
      msgs.splice(msgs.length - 1, 1, { role: "assistant", text: r.answer, meta: `${model || prov} · ${(r.ms / 1000).toFixed(1)} s · live data` });
    } catch (e) {
      msgs.splice(msgs.length - 1, 1, { role: "assistant", text: e.message, error: true });
    }
    this.busy = false; $("#cp-send", this.root).disabled = false;
    if (this.key() === key) this.render();
  }
}
