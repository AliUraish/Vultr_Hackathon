// Live telemetry: WebSocket in, a jitter buffer of 10 Hz frames, and a playback clock slightly behind
// real time so the floor can interpolate smoothly between ticks.
import { bus, cfg } from "./util.js";

const KEEP = 900;          // frames kept (90 s)
const LAG = 2.5;           // ticks behind the newest frame

class LiveFeed {
  constructor() {
    this.frames = new Map();
    this.latest = -1;
    this.play = null;
    this.last = 0;
    this.consumed = -1;
    this.ws = null;
    this.robotHistory = new Map();   // robot id -> recent speeds (per tick)
  }

  connect() {
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
    this.ws = ws;
    ws.onopen = () => bus.emit("ws", true);
    ws.onmessage = (m) => {
      const { type, data } = JSON.parse(m.data);
      if (type === "frames") this.ingest(data);
      else bus.emit(type, data);
    };
    ws.onclose = () => {
      bus.emit("ws", false);
      setTimeout(() => this.connect(), 2000);
    };
  }

  ingest(frames) {
    for (const f of frames) {
      if (f.t < this.latest - 50) this.reset();          // a new sim run started
      this.frames.set(f.t, f);
      if (f.t > this.latest) this.latest = f.t;
    }
    for (const t of this.frames.keys()) if (t < this.latest - KEEP) this.frames.delete(t);
  }

  seed(frame) { if (frame && this.latest < 0) this.ingest([frame]); }

  reset() { this.frames.clear(); this.latest = -1; this.play = null; this.consumed = -1; this.robotHistory.clear(); }

  frameAt(t) {
    for (let k = t; k > t - 20; k--) { const f = this.frames.get(k); if (f) return f; }
    return null;
  }

  // Advance the clock; returns frames to draw plus any ticks passed since the last call.
  advance(now) {
    if (this.latest < 0) return null;
    const dt = this.last ? Math.min(0.25, (now - this.last) / 1000) : 0;
    this.last = now;
    const target = this.latest - LAG;
    if (this.play === null || this.play < target - 15 || this.play > this.latest) this.play = Math.max(0, target);
    else this.play = Math.min(this.latest, this.play + dt * cfg.tickHz * (this.play < target - 2 ? 1.35 : 1));
    const ta = Math.floor(this.play);
    const a = this.frameAt(ta), b = this.frames.get(ta + 1) || null;
    const crossed = [];
    if (this.consumed < ta - 40) this.consumed = ta - 1;
    for (let t = this.consumed + 1; t <= ta; t++) {
      const f = this.frames.get(t);
      if (!f) continue;
      crossed.push(f);
      for (const r of f.robots) {
        let h = this.robotHistory.get(r.id);
        if (!h) { h = []; this.robotHistory.set(r.id, h); }
        h.push(r.v); if (h.length > 300) h.shift();
      }
    }
    this.consumed = Math.max(this.consumed, ta);
    return { a, b, alpha: b ? this.play - ta : 0, crossed, tick: ta };
  }
}

export const live = new LiveFeed();
