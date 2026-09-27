// Canvas renderer for the warehouse floor: static layer cached, robots interpolated at 60 fps,
// with trails, sensor cones, heatmap, ghosts (a second run overlaid) and event effects.
import { cssVar } from "./util.js";

const ACCEL = 10, SENSOR = 800, ROBOT_HALF = 300, PALLET_HALF = 400, CLEARANCE = 100;
const DIRS = { E: [1, 0], W: [-1, 0], S: [0, 1], N: [0, -1] };
const TRAIL = 42;

export const robotColor = (id) => cssVar(`--${String(id).toLowerCase()}`) || "#ccc";

export function stopDist(v) { let d = 0; while (v > 0) { v = Math.max(v - ACCEL, 0); d += v; } return d; }

function lerp(a, b, k) { return a + (b - a) * k; }

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
}

export class Floor {
  constructor(canvas, map, opts = {}) {
    this.c = canvas;
    this.map = map;
    this.opt = { trails: true, sensors: true, paths: false, heat: false, labels: true, ...opts };
    this.effects = [];
    this.trails = new Map();
    this.pallets = new Map();          // "x,y" -> first seen (ms), for the falling animation
    this.heat = new Float32Array(map.width * map.height);
    this.selected = null;
    this.drawn = new Map();            // robot id -> {x, y} in canvas px, for hit testing
    this.slotAt = {};
    this.dockAt = {};
    for (const [id, s] of Object.entries(map.slots)) this.slotAt[s.cell.join(",")] = id;
    for (const [id, c] of Object.entries(map.docks)) this.dockAt[c.join(",")] = id;
    this.slotCell = Object.fromEntries(Object.entries(map.slots).map(([id, s]) => [id, s.cell]));
    this.static = null;
  }

  // ------------------------------------------------------------ layout + static layer
  layout() {
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(this.c.clientWidth, 240), cell = w / this.map.width, h = cell * this.map.height;
    if (!this.static || this.c.width !== Math.round(w * dpr) || this.c.height !== Math.round(h * dpr)) {
      this.c.width = Math.round(w * dpr); this.c.height = Math.round(h * dpr); this.c.style.height = `${h}px`;
      Object.assign(this, { w, h, cell, dpr, mm: cell / this.map.cell_mm });
      this.static = this.buildStatic();
    }
  }

  buildStatic() {
    const { w, h, cell, dpr, map } = this;
    const off = document.createElement("canvas");
    off.width = Math.round(w * dpr); off.height = Math.round(h * dpr);
    const ctx = off.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = cssVar("--floor"); ctx.fillRect(0, 0, w, h);
    const col = { rack: cssVar("--rack"), edge: cssVar("--rack-edge"), dock: cssVar("--dock"), home: cssVar("--home"),
      grid: cssVar("--grid"), text: "#3d4652", accent: cssVar("--accent") };
    for (let y = 0; y < map.height; y++) {
      for (let x = 0; x < map.width; x++) {
        const ch = map.layout[y][x], px = x * cell, py = y * cell;
        if (ch === "#") {
          ctx.fillStyle = "#050607"; ctx.fillRect(px, py, cell, cell);
          ctx.strokeStyle = "rgba(255,255,255,.035)"; ctx.lineWidth = 1;
          ctx.beginPath(); ctx.moveTo(px, py + cell); ctx.lineTo(px + cell, py); ctx.stroke();   // hatched wall
          continue;
        }
        ctx.strokeStyle = col.grid; ctx.lineWidth = 1; ctx.strokeRect(px + .5, py + .5, cell - 1, cell - 1);
        if (/[A-F]/.test(ch)) {
          ctx.fillStyle = col.rack; roundRect(ctx, px + 2, py + 3, cell - 4, cell - 6, 3); ctx.fill();
          ctx.strokeStyle = col.edge; ctx.stroke();
        } else if (/\d/.test(ch)) {
          const g = ctx.createRadialGradient(px + cell / 2, py + cell / 2, 1, px + cell / 2, py + cell / 2, cell * .8);
          g.addColorStop(0, "rgba(0,229,255,.28)"); g.addColorStop(1, "rgba(0,229,255,0)");
          ctx.fillStyle = g; ctx.fillRect(px - cell * .3, py - cell * .3, cell * 1.6, cell * 1.6);
          ctx.strokeStyle = "rgba(0,229,255,.55)"; roundRect(ctx, px + 3, py + 3, cell - 6, cell - 6, 4); ctx.stroke();
        } else if (ch === "H") {
          ctx.fillStyle = col.home; ctx.fillRect(px + 1, py + 1, cell - 2, cell - 2);
        }
      }
    }
    ctx.strokeStyle = "rgba(0,229,255,.18)"; ctx.lineWidth = 1.5;         // building outline
    ctx.strokeRect(cell + .75, cell + .75, (map.width - 2) * cell - 1.5, (map.height - 2) * cell - 1.5);
    if (cell >= 18) {
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.font = `${Math.max(7, cell * .26)}px ui-monospace, monospace`;
      ctx.fillStyle = col.text;
      for (const [k, id] of Object.entries(this.slotAt)) { const [x, y] = k.split(",").map(Number); ctx.fillText(id, (x + .5) * cell, (y + .5) * cell); }
      ctx.fillStyle = col.accent;
      for (const [k, id] of Object.entries(this.dockAt)) { const [x, y] = k.split(",").map(Number); ctx.fillText(id, (x + .5) * cell, (y + .5) * cell); }
    }
    return off;
  }

  // ------------------------------------------------------------ state fed by the caller
  resetTrails() { this.trails.clear(); }

  accumulate(frame) {  // congestion heat: where robots stand waiting
    for (let i = 0; i < this.heat.length; i++) this.heat[i] *= 0.996;
    for (const r of frame.robots) {
      if (r.st === "waiting" || r.st === "blocked" || r.st === "held") {
        const x = Math.floor(r.x / this.map.cell_mm), y = Math.floor(r.y / this.map.cell_mm);
        this.heat[y * this.map.width + x] += 1;
      }
    }
  }

  trigger(ev, frame, now = performance.now()) {
    const at = (rid) => frame.robots.find((r) => r.id === rid);
    const cellMM = (c) => [(c[0] + .5) * this.map.cell_mm, (c[1] + .5) * this.map.cell_mm];
    const add = (o) => this.effects.push({ t0: now, ...o });
    const r = ev.robot ? at(ev.robot) : null;
    switch (ev.type) {
      case "contact":
        if (r) { add({ kind: "shock", x: r.x, y: r.y, dur: 1100, color: cssVar("--bad") });
          add({ kind: "text", x: r.x, y: r.y, dur: 1600, color: cssVar("--bad"), text: "COLLISION" }); }
        break;
      case "struck":
        if (r) add({ kind: "shock", x: r.x, y: r.y, dur: 800, color: cssVar("--warn") });
        break;
      case "pallet_dropped": {
        const [x, y] = cellMM(ev.cell);
        add({ kind: "dust", x, y, dur: 700, color: cssVar("--pallet") });
        break;
      }
      case "dock_scan": {
        const c = this.map.docks[ev.dock]; if (!c) break;
        const [x, y] = cellMM(c);
        add({ kind: "flash", x, y, dur: 900, color: ev.ok ? cssVar("--ok") : cssVar("--bad") });
        add({ kind: "text", x, y, dur: 1500, color: ev.ok ? cssVar("--ok") : cssVar("--bad"),
          text: ev.ok ? `✓ ${ev.job || ""}` : "✕ WRONG ITEM" });
        break;
      }
      case "pick": {
        const c = this.slotCell[ev.slot]; if (!c) break;
        const [x, y] = cellMM(c);
        add({ kind: "flash", x, y, dur: 600, color: cssVar("--accent") });
        break;
      }
      case "scan_mismatch": {
        const c = this.slotCell[ev.slot]; if (!c) break;
        const [x, y] = cellMM(c);
        add({ kind: "flash", x, y, dur: 1200, color: cssVar("--warn") });
        add({ kind: "text", x, y, dur: 1700, color: cssVar("--warn"), text: "scan caught mislabel" });
        break;
      }
      case "zone_enter":
        if (r) add({ kind: "shock", x: r.x, y: r.y, dur: 900, color: cssVar("--bad") }),
          add({ kind: "text", x: r.x, y: r.y, dur: 1500, color: cssVar("--bad"), text: "ZONE BREACH" });
        break;
      case "job_exception":
        if (r) add({ kind: "text", x: r.x, y: r.y, dur: 1500, color: cssVar("--warn"), text: `exception: ${ev.reason}` });
        break;
      case "policy_applied":
        add({ kind: "banner", dur: 1800, color: cssVar("--accent"), text: `policy v${ev.version} live` });
        break;
      default:
    }
  }

  hit(clientX, clientY) {
    const rect = this.c.getBoundingClientRect();
    const x = clientX - rect.left, y = clientY - rect.top;
    let best = null, bd = this.cell * .7;
    for (const [id, p] of this.drawn) { const d = Math.hypot(p.x - x, p.y - y); if (d < bd) { bd = d; best = id; } }
    return best;
  }

  // ------------------------------------------------------------ frame drawing
  draw(a, b = null, alpha = 0, now = performance.now(), extra = {}) {
    this.layout();
    const ctx = this.c.getContext("2d");
    const { w, h } = this;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.drawImage(this.static, 0, 0, w, h);
    if (!a) {
      ctx.fillStyle = cssVar("--muted"); ctx.font = "14px system-ui"; ctx.textAlign = "center";
      ctx.fillText(extra.empty || "waiting for data…", w / 2, h / 2);
      return;
    }
    if (this.opt.heat) this.drawHeat(ctx);
    this.drawZones(ctx, a, now);
    this.drawPallets(ctx, a, now);

    const pos = new Map();
    for (const ra of a.robots) {
      const rb = b && b.robots.find((r) => r.id === ra.id);
      pos.set(ra.id, { r: ra, x: rb ? lerp(ra.x, rb.x, alpha) : ra.x, y: rb ? lerp(ra.y, rb.y, alpha) : ra.y,
        v: rb ? lerp(ra.v, rb.v, alpha) : ra.v });
    }
    if (this.opt.paths) this.drawPaths(ctx, pos, now);
    if (this.opt.trails) this.drawTrails(ctx, pos, now);
    if (this.opt.sensors) for (const p of pos.values()) this.drawSensor(ctx, p);
    if (extra.ghost) this.drawGhosts(ctx, extra.ghost, pos);

    this.drawn.clear();
    for (const p of pos.values()) this.drawRobot(ctx, p, now, extra.dim);
    this.drawEffects(ctx, now);

    if (extra.label) {
      ctx.font = "12px ui-monospace, monospace"; ctx.textAlign = "left"; ctx.textBaseline = "middle";
      const tw = ctx.measureText(extra.label).width;
      ctx.fillStyle = "rgba(0,0,0,.7)"; roundRect(ctx, 8, 8, tw + 18, 24, 6); ctx.fill();
      ctx.fillStyle = cssVar("--text"); ctx.fillText(extra.label, 17, 20);
    }
  }

  drawHeat(ctx) {
    const { cell } = this;
    for (let i = 0; i < this.heat.length; i++) {
      const v = this.heat[i]; if (v < 1) continue;
      const x = i % this.map.width, y = Math.floor(i / this.map.width);
      ctx.fillStyle = `rgba(255,59,92,${Math.min(.55, v / 60)})`;
      ctx.fillRect(x * cell, y * cell, cell, cell);
    }
  }

  drawZones(ctx, a, now) {
    const { cell } = this;
    const pulse = .12 + .08 * Math.sin(now / 260);
    for (const z of a.zones || []) {
      const cells = this.map.zones[z] || [];
      ctx.fillStyle = `rgba(255,59,92,${pulse})`;
      for (const [x, y] of cells) ctx.fillRect(x * cell, y * cell, cell, cell);
      ctx.save(); ctx.setLineDash([6, 5]); ctx.lineDashOffset = -now / 40; ctx.strokeStyle = "rgba(255,59,92,.8)"; ctx.lineWidth = 1.5;
      const xs = cells.map((c) => c[0]), ys = cells.map((c) => c[1]);
      ctx.strokeRect(Math.min(...xs) * cell + 1, Math.min(...ys) * cell + 1, (Math.max(...xs) - Math.min(...xs) + 1) * cell - 2,
        (Math.max(...ys) - Math.min(...ys) + 1) * cell - 2);
      ctx.restore();
      if (cells.length) {  // a worker pacing inside the closed aisle
        const k = (Math.sin(now / 1400) + 1) / 2, i = k * (cells.length - 1), c0 = cells[Math.floor(i)], c1 = cells[Math.ceil(i)];
        const fx = lerp(c0[0], c1[0], i % 1) + .5, fy = lerp(c0[1], c1[1], i % 1) + .5;
        this.drawWorker(ctx, fx * cell, fy * cell, cell, now);
      }
    }
  }

  drawWorker(ctx, x, y, cell, now) {
    const s = cell, leg = Math.sin(now / 160) * s * .12;
    ctx.save(); ctx.strokeStyle = cssVar("--bad"); ctx.fillStyle = cssVar("--bad"); ctx.lineWidth = Math.max(1.5, s * .07);
    ctx.shadowColor = cssVar("--bad"); ctx.shadowBlur = 10;
    ctx.beginPath(); ctx.arc(x, y - s * .24, s * .11, 0, Math.PI * 2); ctx.fill();
    ctx.beginPath(); ctx.moveTo(x, y - s * .12); ctx.lineTo(x, y + s * .14);
    ctx.moveTo(x - s * .16, y - s * .02); ctx.lineTo(x + s * .16, y - s * .02);
    ctx.moveTo(x, y + s * .14); ctx.lineTo(x - s * .1 + leg, y + s * .36);
    ctx.moveTo(x, y + s * .14); ctx.lineTo(x + s * .1 - leg, y + s * .36); ctx.stroke();
    ctx.restore();
  }

  drawPallets(ctx, a, now) {
    const { cell } = this;
    const live = new Set();
    for (const c of a.pallets || []) {
      const key = c.join(","); live.add(key);
      if (!this.pallets.has(key)) this.pallets.set(key, now);
      const k = Math.min(1, (now - this.pallets.get(key)) / 420), fall = 1 - k;   // falls in from above
      const s = cell * .8 * (1 + fall * .6), cx = (c[0] + .5) * cell, cy = (c[1] + .5) * cell - fall * cell * .9;
      ctx.save();
      ctx.fillStyle = "rgba(0,0,0,.5)"; ctx.beginPath();
      ctx.ellipse((c[0] + .5) * cell, (c[1] + .5) * cell + cell * .3, s * .45 * (1 - fall * .5), s * .12, 0, 0, Math.PI * 2); ctx.fill();
      ctx.shadowColor = cssVar("--pallet"); ctx.shadowBlur = 12;
      ctx.fillStyle = cssVar("--pallet"); roundRect(ctx, cx - s / 2, cy - s / 2, s, s, 3); ctx.fill();
      ctx.shadowBlur = 0; ctx.strokeStyle = "rgba(0,0,0,.55)"; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(cx - s / 2 + 3, cy - s / 2 + 3); ctx.lineTo(cx + s / 2 - 3, cy + s / 2 - 3);
      ctx.moveTo(cx + s / 2 - 3, cy - s / 2 + 3); ctx.lineTo(cx - s / 2 + 3, cy + s / 2 - 3); ctx.stroke();
      ctx.restore();
    }
    for (const k of [...this.pallets.keys()]) if (!live.has(k)) this.pallets.delete(k);
  }

  drawPaths(ctx, pos, now) {
    const { cell, mm } = this;
    ctx.save(); ctx.setLineDash([4, 6]); ctx.lineDashOffset = -now / 30; ctx.lineWidth = 1.5;
    for (const p of pos.values()) {
      if (!p.r.p || !p.r.p.length) continue;
      ctx.strokeStyle = robotColor(p.r.id) + "88"; ctx.beginPath(); ctx.moveTo(p.x * mm, p.y * mm);
      for (const [x, y] of p.r.p) ctx.lineTo((x + .5) * cell, (y + .5) * cell);
      ctx.stroke();
    }
    ctx.restore();
  }

  drawTrails(ctx, pos) {
    const { mm } = this;
    for (const p of pos.values()) {
      let t = this.trails.get(p.r.id);
      if (!t) { t = []; this.trails.set(p.r.id, t); }
      const last = t[t.length - 1];
      if (!last || Math.hypot(last.x - p.x, last.y - p.y) > 60) { t.push({ x: p.x, y: p.y }); if (t.length > TRAIL) t.shift(); }
      if (last && Math.hypot(last.x - p.x, last.y - p.y) > 2500) t.splice(0, t.length - 1);  // jumped (seek): restart
      if (t.length < 2) continue;
      const col = robotColor(p.r.id);
      ctx.lineCap = "round";
      for (let i = 1; i < t.length; i++) {
        ctx.strokeStyle = col; ctx.globalAlpha = (i / t.length) * .5; ctx.lineWidth = 1 + (i / t.length) * 3;
        ctx.beginPath(); ctx.moveTo(t[i - 1].x * mm, t[i - 1].y * mm); ctx.lineTo(t[i].x * mm, t[i].y * mm); ctx.stroke();
      }
      ctx.globalAlpha = 1;
    }
  }

  drawSensor(ctx, p) {
    const d = DIRS[p.r.d]; if (!d || p.v <= 0) return;
    const { mm } = this;
    const blind = stopDist(Math.round(p.v)) + CLEARANCE > SENSOR;    // can't stop within what it can see
    const x0 = p.x + d[0] * ROBOT_HALF, y0 = p.y + d[1] * ROBOT_HALF;
    const x1 = x0 + d[0] * SENSOR, y1 = y0 + d[1] * SENSOR;
    const sp = ROBOT_HALF * .9, spread = ROBOT_HALF * 1.3;
    const px = -d[1], py = d[0];
    ctx.save();
    const g = ctx.createLinearGradient(x0 * mm, y0 * mm, x1 * mm, y1 * mm);
    const c = blind ? "255,59,92" : "0,229,255";
    g.addColorStop(0, `rgba(${c},${blind ? .38 : .2})`); g.addColorStop(1, `rgba(${c},0)`);
    ctx.fillStyle = g; ctx.beginPath();
    ctx.moveTo((x0 + px * sp) * mm, (y0 + py * sp) * mm); ctx.lineTo((x1 + px * spread) * mm, (y1 + py * spread) * mm);
    ctx.lineTo((x1 - px * spread) * mm, (y1 - py * spread) * mm); ctx.lineTo((x0 - px * sp) * mm, (y0 - py * sp) * mm);
    ctx.closePath(); ctx.fill(); ctx.restore();
  }

  drawGhosts(ctx, ghost, pos) {
    const { mm, cell } = this;
    const { a, b, alpha } = ghost;
    if (!a) return;
    const s = cell * .6;
    ctx.save();
    for (const ra of a.robots) {
      const rb = b && b.robots.find((r) => r.id === ra.id);
      const x = (rb ? lerp(ra.x, rb.x, alpha) : ra.x) * mm, y = (rb ? lerp(ra.y, rb.y, alpha) : ra.y) * mm;
      const col = robotColor(ra.id);
      const real = pos.get(ra.id);
      if (real) {
        const dx = real.x * mm - x, dy = real.y * mm - y, dist = Math.hypot(dx, dy);
        if (dist > cell * .25) {    // where the two runs diverge
          ctx.strokeStyle = cssVar("--accent-2"); ctx.globalAlpha = Math.min(1, dist / cell); ctx.setLineDash([3, 4]); ctx.lineWidth = 1.5;
          ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(real.x * mm, real.y * mm); ctx.stroke(); ctx.globalAlpha = 1;
        }
      }
      ctx.setLineDash([4, 3]); ctx.strokeStyle = col; ctx.lineWidth = 2; ctx.globalAlpha = .85;
      roundRect(ctx, x - s / 2, y - s / 2, s, s, s * .28); ctx.stroke(); ctx.globalAlpha = 1;
    }
    for (const c of ghost.a.pallets || []) {
      ctx.setLineDash([3, 3]); ctx.strokeStyle = cssVar("--pallet");
      ctx.strokeRect(c[0] * cell + cell * .1, c[1] * cell + cell * .1, cell * .8, cell * .8);
    }
    ctx.restore();
  }

  drawRobot(ctx, p, now, dim) {
    const { mm, cell } = this;
    const r = p.r, x = p.x * mm, y = p.y * mm, s = cell * .6, col = robotColor(r.id);
    this.drawn.set(r.id, { x, y });
    ctx.save();
    if (dim) ctx.globalAlpha = .35;
    ctx.shadowColor = col; ctx.shadowBlur = r.st === "idle" ? 6 : 16;
    ctx.fillStyle = col; roundRect(ctx, x - s / 2, y - s / 2, s, s, s * .28); ctx.fill();
    ctx.shadowBlur = 0;
    const ring = r.st === "estop" ? cssVar("--bad") : ["waiting", "blocked", "held"].includes(r.st) ? cssVar("--warn") : null;
    if (ring) {
      ctx.strokeStyle = ring; ctx.lineWidth = 2.5; ctx.globalAlpha = r.st === "estop" ? .6 + .4 * Math.sin(now / 90) : .9;
      roundRect(ctx, x - s / 2 - 3, y - s / 2 - 3, s + 6, s + 6, s * .32); ctx.stroke(); ctx.globalAlpha = dim ? .35 : 1;
    }
    const d = DIRS[r.d];
    if (d) {
      ctx.fillStyle = "rgba(0,0,0,.55)"; ctx.beginPath();
      const tx = x + d[0] * s * .47, ty = y + d[1] * s * .47, pv = [-d[1] * s * .17, d[0] * s * .17];
      ctx.moveTo(tx, ty); ctx.lineTo(x + d[0] * s * .18 + pv[0], y + d[1] * s * .18 + pv[1]);
      ctx.lineTo(x + d[0] * s * .18 - pv[0], y + d[1] * s * .18 - pv[1]); ctx.fill();
    }
    if (r.c) { ctx.fillStyle = "#fff"; ctx.shadowColor = "#fff"; ctx.shadowBlur = 6; ctx.fillRect(x + s * .16, y - s * .5, s * .3, s * .3); ctx.shadowBlur = 0; }
    ctx.fillStyle = "#000"; ctx.font = `800 ${Math.max(9, s * .42)}px system-ui`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText(r.id.slice(1), x, y + 1);
    if (this.selected === r.id) {
      const k = (now % 1200) / 1200;
      ctx.strokeStyle = col; ctx.lineWidth = 2; ctx.globalAlpha = 1 - k;
      ctx.beginPath(); ctx.arc(x, y, s * (.75 + k * .6), 0, Math.PI * 2); ctx.stroke();
      ctx.globalAlpha = 1; ctx.beginPath(); ctx.arc(x, y, s * .78, 0, Math.PI * 2); ctx.stroke();
    }
    ctx.restore();
  }

  drawEffects(ctx, now) {
    const { mm, w, h } = this;
    this.effects = this.effects.filter((e) => now - e.t0 < e.dur);
    for (const e of this.effects) {
      const k = (now - e.t0) / e.dur, x = e.x * mm, y = e.y * mm;
      ctx.save();
      if (e.kind === "shock") {
        for (const off of [0, .18]) {
          const kk = Math.max(0, k - off); if (kk <= 0) continue;
          ctx.strokeStyle = e.color; ctx.globalAlpha = (1 - kk) * .9; ctx.lineWidth = 3 * (1 - kk) + 1;
          ctx.shadowColor = e.color; ctx.shadowBlur = 14;
          ctx.beginPath(); ctx.arc(x, y, this.cell * (.4 + kk * 2.2), 0, Math.PI * 2); ctx.stroke();
        }
      } else if (e.kind === "dust") {
        ctx.fillStyle = e.color; ctx.globalAlpha = (1 - k) * .5;
        for (let i = 0; i < 10; i++) {
          const ang = i / 10 * Math.PI * 2, rr = this.cell * (.3 + k * .9);
          ctx.beginPath(); ctx.arc(x + Math.cos(ang) * rr, y + Math.sin(ang) * rr, 2.2 * (1 - k) + .5, 0, Math.PI * 2); ctx.fill();
        }
      } else if (e.kind === "flash") {
        const g = ctx.createRadialGradient(x, y, 1, x, y, this.cell * 1.4);
        g.addColorStop(0, e.color); g.addColorStop(1, "transparent");
        ctx.globalAlpha = (1 - k) * .7; ctx.fillStyle = g; ctx.fillRect(x - this.cell * 1.5, y - this.cell * 1.5, this.cell * 3, this.cell * 3);
      } else if (e.kind === "text") {
        ctx.globalAlpha = k < .8 ? 1 : (1 - k) / .2;
        ctx.font = `700 ${Math.max(10, this.cell * .34)}px system-ui`; ctx.textAlign = "center";
        ctx.fillStyle = e.color; ctx.shadowColor = "#000"; ctx.shadowBlur = 6;
        ctx.fillText(e.text, x, y - this.cell * (.7 + k * .9));
      } else if (e.kind === "banner") {
        ctx.globalAlpha = k < .15 ? k / .15 : k > .75 ? (1 - k) / .25 : 1;
        ctx.strokeStyle = e.color; ctx.lineWidth = 3; ctx.shadowColor = e.color; ctx.shadowBlur = 24; ctx.strokeRect(2, 2, w - 4, h - 4);
        ctx.font = "700 16px system-ui"; ctx.textAlign = "center"; ctx.fillStyle = e.color;
        ctx.fillText(e.text, w / 2, 30);
      }
      ctx.restore();
    }
  }
}
