// Replay viewer: live recording vs a replay, side by side or as a ghost overlay on one floor.
// One clock drives both canvases and any follower floors (fix-trial mini replays).
import { $, esc, cfg } from "./util.js";
import { Floor } from "./floor.js";

const SPEEDS = [0.5, 1, 2, 4, 8];

export class ReplayViewer {
  constructor(root) {
    this.root = root;
    this.live = new Map();
    this.replay = null;
    this.meta = null;
    this.t = 0; this.lo = 0; this.hi = 0;
    this.playing = false; this.speed = 2; this.ghost = false;
    this.followers = [];
    this.marks = [];
    this.last = 0;
    root.innerHTML = `
      <div class="viewer">
        <div class="canvases">
          <figure><figcaption><span id="vw-live-cap">Recorded live</span><span class="mono" id="vw-live-span"></span></figcaption>
            <div class="stage"><canvas id="vw-live"></canvas></div></figure>
          <figure class="replay-fig"><figcaption><span id="vw-rep-cap">Replay</span><span class="mono" id="vw-rep-meta"></span></figcaption>
            <div class="stage"><canvas id="vw-rep"></canvas></div></figure>
        </div>
        <div class="controls">
          <button class="icon primary" id="vw-play" title="Play / pause (space)">▶</button>
          <div class="timeline" id="vw-tl"><div class="track"></div><div class="fill" id="vw-fill"></div><div id="vw-marks"></div><div class="head" id="vw-head"></div></div>
          <span class="tick-label" id="vw-label">t –</span>
          <div class="seg" id="vw-speed">${SPEEDS.map((s) => `<button data-s="${s}" class="${s === this.speed ? "on" : ""}">${s}×</button>`).join("")}</div>
          <div class="seg" id="vw-mode"><button data-m="side" class="on">Side by side</button><button data-m="ghost">Ghost overlay</button></div>
        </div>
        <div class="ev-row"><div class="ev-strip" id="vw-ev"></div>
          <span class="keys"><kbd>space</kbd> play · <kbd>←</kbd><kbd>→</kbd> step · <kbd>shift</kbd> ×10 · <kbd>[</kbd><kbd>]</kbd> speed · <kbd>g</kbd> ghost</span></div>
      </div>`;
    this.fLive = new Floor($("#vw-live", root), cfg.map, { trails: true, sensors: true });
    this.fRep = new Floor($("#vw-rep", root), cfg.map, { trails: true, sensors: true });
    $("#vw-play", root).onclick = () => this.toggle();
    for (const b of root.querySelectorAll("#vw-speed button")) b.onclick = () => this.setSpeed(Number(b.dataset.s));
    for (const b of root.querySelectorAll("#vw-mode button")) b.onclick = () => this.setGhost(b.dataset.m === "ghost");
    const tl = $("#vw-tl", root);
    const seekFromEvent = (e) => {
      const rect = tl.getBoundingClientRect();
      const k = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
      this.seek(Math.round(this.lo + k * (this.hi - this.lo)));
    };
    tl.addEventListener("pointerdown", (e) => { tl.setPointerCapture(e.pointerId); seekFromEvent(e); tl.onpointermove = seekFromEvent; });
    tl.addEventListener("pointerup", () => { tl.onpointermove = null; });
    for (const [c, f] of [[$("#vw-live", root), this.fLive], [$("#vw-rep", root), this.fRep]]) {
      c.addEventListener("click", (e) => { const id = f.hit(e.clientX, e.clientY); this.fLive.selected = this.fRep.selected = id; });
    }
    this.raf = requestAnimationFrame((n) => this.loop(n));
  }

  destroy() { cancelAnimationFrame(this.raf); this.followers = []; }

  // meta: {start, end, failure: {tick}, caption?, endLabel?}; a variant pair passes its own captions.
  setLive(frames, meta) {
    this.live = new Map(frames.map((f) => [f.t, f]));
    this.meta = meta;
    this.lo = meta.start + 1;
    this.hi = Math.max(this.hi, meta.end);
    if (!this.t) this.t = Math.max(this.lo, meta.failure.tick - 5 * cfg.tickHz);
    $("#vw-live-span", this.root).textContent = `ticks ${meta.start}–${meta.end}`;
    if (meta.caption) $("#vw-live-cap", this.root).textContent = meta.caption;
    this.marks = [[meta.failure.tick, "m-fail", "failure"], [meta.end, "m-end", meta.endLabel || "recording ends (T+2 s)"]];
    for (const f of frames) for (const e of f.ev || []) {
      if (["pallet_dropped", "bin_mislabeled", "zone_restricted"].includes(e.type)) this.marks.push([f.t, "m-chaos", e.type.replace("_", " ")]);
    }
    this.renderMarks();
  }

  addMarks(marks) { this.marks.push(...marks); this.renderMarks(); }

  setReplay(frames, caption, meta = "") {
    this.replay = frames ? new Map(frames.map((f) => [f.t, f])) : null;
    if (frames && frames.length) this.hi = Math.max(this.hi, frames[frames.length - 1].t);
    $("#vw-rep-cap", this.root).textContent = caption;
    $("#vw-rep-meta", this.root).textContent = meta;
    this.fRep.resetTrails();
    this.renderMarks();
  }

  follow(canvas, frames) {
    const f = new Floor(canvas, cfg.map, { trails: false, sensors: true });
    const entry = { floor: f, frames: frames ? new Map(frames.map((x) => [x.t, x])) : null, canvas };
    this.followers.push(entry);
    if (frames && frames.length) this.hi = Math.max(this.hi, frames[frames.length - 1].t);
    return entry;
  }

  clearFollowers() { this.followers = []; }

  renderMarks() {
    const span = Math.max(1, this.hi - this.lo), pct = (t) => `${((t - this.lo) / span) * 100}%`;
    $("#vw-marks", this.root).innerHTML = this.marks.filter(([t]) => t >= this.lo && t <= this.hi)
      .map(([t, c, label]) => `<span class="mark ${c}" style="left:${pct(t)}" title="${esc(label)} · t${t}"></span>`).join("");
  }

  toggle() { this.playing = !this.playing; if (this.playing && this.t >= this.hi) this.seek(this.lo); $("#vw-play", this.root).textContent = this.playing ? "❚❚" : "▶"; }
  setSpeed(s) { this.speed = s; for (const b of this.root.querySelectorAll("#vw-speed button")) b.classList.toggle("on", Number(b.dataset.s) === s); }
  setGhost(on) {
    this.ghost = on;
    this.root.querySelector(".viewer").classList.toggle("ghost", on);
    for (const b of this.root.querySelectorAll("#vw-mode button")) b.classList.toggle("on", (b.dataset.m === "ghost") === on);
    $("#vw-live-cap", this.root).textContent = on ? "Solid: left run · outline: right run" : (this.meta && this.meta.caption) || "Recorded live";
    this.fLive.static = null;
  }
  seek(t) {
    const jump = Math.abs(t - this.t) > 3;
    this.t = Math.min(this.hi, Math.max(this.lo, t));
    if (jump) { this.fLive.resetTrails(); this.fRep.resetTrails(); }
  }
  step(n) { this.playing = false; $("#vw-play", this.root).textContent = "▶"; this.seek(Math.round(this.t) + n); }

  onKey(e) {
    if (e.target.matches("input, select, textarea")) return false;
    if (e.key === " ") { this.toggle(); return true; }
    if (e.key === "ArrowRight") { this.step(e.shiftKey ? 10 : 1); return true; }
    if (e.key === "ArrowLeft") { this.step(e.shiftKey ? -10 : -1); return true; }
    if (e.key.toLowerCase() === "g") { this.setGhost(!this.ghost); return true; }
    if (e.key === "]") { this.setSpeed(SPEEDS[Math.min(SPEEDS.length - 1, SPEEDS.indexOf(this.speed) + 1)]); return true; }
    if (e.key === "[") { this.setSpeed(SPEEDS[Math.max(0, SPEEDS.indexOf(this.speed) - 1)]); return true; }
    return false;
  }

  sample(map, t) {
    if (!map) return { a: null, b: null, alpha: 0 };
    const ti = Math.floor(t);
    let a = map.get(ti);
    if (!a) for (let k = ti; k >= ti - 400 && !a; k--) a = map.get(k);
    const b = map.get(ti + 1) || null;
    return { a, b, alpha: b ? t - ti : 0 };
  }

  loop(now) {
    const dt = this.last ? Math.min(0.1, (now - this.last) / 1000) : 0;
    this.last = now;
    const before = Math.floor(this.t);
    if (this.playing && this.live.size) {
      this.t = Math.min(this.hi, this.t + dt * cfg.tickHz * this.speed);
      if (this.t >= this.hi) this.toggle();
    }
    const after = Math.floor(this.t);
    if (this.live.size) this.render(now, before, after);
    this.raf = requestAnimationFrame((n) => this.loop(n));
  }

  render(now, before, after) {
    const end = this.meta ? this.meta.end : this.hi;
    const L = this.sample(this.live, Math.min(this.t, end));
    const R = this.sample(this.replay, this.t);
    if (after > before && after - before < 30) {     // fire effects for ticks we just played through
      for (let t = before + 1; t <= after; t++) {
        const lf = this.live.get(t); if (lf && t <= end) for (const e of lf.ev || []) this.fLive.trigger(e, lf, now);
        const rf = this.replay && this.replay.get(t); if (rf) for (const e of rf.ev || []) this.fRep.trigger(e, rf, now);
        for (const fo of this.followers) { const ff = fo.frames && fo.frames.get(t); if (ff) for (const e of ff.ev || []) fo.floor.trigger(e, ff, now); }
      }
    }
    const label = this.t > end ? `t ${Math.floor(this.t)} · recording ended` : `t ${Math.floor(this.t)}`;
    if (this.ghost) {
      this.fLive.draw(L.a, L.b, L.alpha, now, { label, ghost: R.a ? R : null });
    } else {
      this.fLive.draw(L.a, L.b, L.alpha, now, { label, dim: this.t > end });
      this.fRep.draw(R.a, R.b, R.alpha, now, { label: `t ${Math.floor(this.t)}`, empty: "waiting for a replay worker…" });
    }
    for (const fo of this.followers) {
      if (!fo.canvas.isConnected) continue;
      const S = this.sample(fo.frames, this.t);
      fo.floor.draw(S.a, S.b, S.alpha, now, { label: `t ${Math.floor(this.t)}`, empty: "replaying on VM B…" });
    }
    const span = Math.max(1, this.hi - this.lo), k = (this.t - this.lo) / span;
    $("#vw-fill", this.root).style.width = `${k * 100}%`;
    $("#vw-head", this.root).style.left = `${k * 100}%`;
    const fail = this.meta ? this.meta.failure.tick : 0, d = (this.t - fail) / cfg.tickHz;
    $("#vw-label", this.root).textContent = `t ${Math.floor(this.t)} (${d < 0 ? "−" : "+"}${Math.abs(d).toFixed(1)} s)`;
    const src = this.replay || this.live, near = [];
    for (let t = Math.floor(this.t) - 25; t <= Math.floor(this.t); t++) {
      const f = src.get(t); if (!f) continue;
      for (const e of f.ev || []) near.push(`t${t} ${e.robot || ""} ${e.type}${e.with ? ` ${e.with}` : ""}${e.slot ? ` ${e.slot}` : ""}${e.zone ? ` ${e.zone}` : ""}${e.ok === false ? " MISMATCH" : ""}`);
    }
    $("#vw-ev", this.root).textContent = near.slice(-4).join("   ·   ");
  }
}
