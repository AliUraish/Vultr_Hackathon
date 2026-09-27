// Canvas renderer for the pit seen from above: static layer (benches shaded by depth, haul roads, pockets,
// excavators, dump points) cached, trucks interpolated at 60 fps, with trails, lidar cones, potholes, fallen rocks,
// blast closures, ghosts (a second run overlaid) and event effects. Used by the haul map and every replay viewer.
import { cssVar, cfg, excavator, destination } from "./util.js";

const DIRS = { E: [1, 0], W: [-1, 0], S: [0, 1], N: [0, -1] };
const TRAIL = 42;
const phys = () => (cfg.map && cfg.map.physics) || { accel: 20, lidar_m: 60, robot_half: 5000, rock_half: 3000, clearance_m: 3 };

export const robotColor = (id) => cssVar(`--${String(id).toLowerCase()}`) || "#e8dcc4";

export function stopDist(v) { const a = phys().accel || 20; let d = 0; while (v > 0) { v = Math.max(v - a, 0); d += v; } return d; }

function lerp(a, b, k) { return a + (b - a) * k; }

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
}

// elevation (m) -> earth tone: the crest is pale ochre, the pit floor deep red-brown
function earth(elev, road) {
  const k = Math.min(1, Math.max(0, (elev + 48) / 56));     // 0 at the pit floor, 1 at the rim
  const r = Math.round(lerp(58, 132, k)), g = Math.round(lerp(38, 100, k)), b = Math.round(lerp(26, 66, k));
  return road ? `rgb(${Math.round(r * 1.18)},${Math.round(g * 1.14)},${Math.round(b * 1.1)})` : `rgb(${Math.round(r * .62)},${Math.round(g * .6)},${Math.round(b * .58)})`;
}

export class Floor {
  constructor(canvas, map, opts = {}) {
    this.c = canvas;
    this.map = map;
    this.opt = { trails: true, sensors: true, paths: false, heat: false, labels: true, ...opts };
    this.effects = [];
    this.trails = new Map();
    this.rocks = new Map();            // "x,y" -> first seen (ms), for the falling animation
    this.heat = new Float32Array(map.width * map.height);
    this.selected = null;
    this.drawn = new Map();            // truck id -> {x, y} in canvas px, for hit testing
    this.slotAt = {};
    this.dockAt = {};
    for (const [id, s] of Object.entries(map.slots)) this.slotAt[s.cell.join(",")] = id;
    for (const [id, c] of Object.entries(map.docks)) this.dockAt[c.join(",")] = id;
    this.slotCell = Object.fromEntries(Object.entries(map.slots).map(([id, s]) => [id, s.access]));
    this.pockets = new Map((map.pockets || []).map(([x, y, o]) => [`${x},${y}`, o]));
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
    ctx.fillStyle = "#070605"; ctx.fillRect(0, 0, w, h);
    const elev = map.elev || [];
    for (let y = 0; y < map.height; y++) {
      for (let x = 0; x < map.width; x++) {
        const ch = map.layout[y][x], px = x * cell, py = y * cell, e = (elev[y] || [])[x] ?? 0;
        const road = ch !== "#" && !/[A-D]/.test(ch);
        ctx.fillStyle = earth(e, road); ctx.fillRect(px, py, cell + .5, cell + .5);
        if (!road) {   // bench face: darker toward the road below it (the highwall drops away)
          const below = (elev[y + 1] || [])[x];
          if (below !== undefined && below < e - 4) {
            const g = ctx.createLinearGradient(0, py, 0, py + cell);
            g.addColorStop(0, "rgba(0,0,0,0)"); g.addColorStop(1, "rgba(0,0,0,.45)");
            ctx.fillStyle = g; ctx.fillRect(px, py, cell, cell);
          }
        } else {
          ctx.fillStyle = "rgba(0,0,0,.08)";   // tyre ruts
          ctx.fillRect(px + cell * .28, py, cell * .06, cell); ctx.fillRect(px + cell * .66, py, cell * .06, cell);
        }
      }
    }
    // benches and berms: an outline where road meets rock
    ctx.strokeStyle = "rgba(20,12,6,.55)"; ctx.lineWidth = Math.max(1, cell * .06);
    for (let y = 0; y < map.height; y++) for (let x = 0; x < map.width; x++) {
      const isRoad = (xx, yy) => { const ch = (map.layout[yy] || "")[xx]; return ch && ch !== "#" && !/[A-D]/.test(ch); };
      if (!isRoad(x, y)) continue;
      const px = x * cell, py = y * cell;
      ctx.beginPath();
      if (!isRoad(x, y - 1)) { ctx.moveTo(px, py); ctx.lineTo(px + cell, py); }
      if (!isRoad(x, y + 1)) { ctx.moveTo(px, py + cell); ctx.lineTo(px + cell, py + cell); }
      if (!isRoad(x - 1, y)) { ctx.moveTo(px, py); ctx.lineTo(px, py + cell); }
      if (!isRoad(x + 1, y)) { ctx.moveTo(px + cell, py); ctx.lineTo(px + cell, py + cell); }
      ctx.stroke();
    }
    // keep-left lane arrows on the two-lane roads
    if (cell >= 16) {
      ctx.fillStyle = "rgba(255,240,210,.16)";
      for (const [x, y, dx, dy] of map.lanes || []) {
        if ((x + y) % 3) continue;
        const cx = (x + .5) * cell, cy = (y + .5) * cell, s = cell * .16;
        ctx.beginPath(); ctx.moveTo(cx + dx * s, cy + dy * s);
        ctx.lineTo(cx - dx * s - dy * s * .8, cy - dy * s + dx * s * .8); ctx.lineTo(cx - dx * s + dy * s * .8, cy - dy * s - dx * s * .8);
        ctx.fill();
      }
    }
    // loading and tipping pockets
    for (const [k, owner] of this.pockets) {
      const [x, y] = k.split(",").map(Number);
      ctx.fillStyle = owner.startsWith("DK") ? "rgba(245,184,0,.10)" : "rgba(255,138,0,.10)";
      ctx.fillRect(x * cell + 1, y * cell + 1, cell - 2, cell - 2);
    }
    // dump points
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    for (const [id, c] of Object.entries(map.docks)) {
      const px = c[0] * cell, py = c[1] * cell;
      ctx.strokeStyle = "rgba(245,184,0,.8)"; ctx.lineWidth = 1.5; roundRect(ctx, px + 2, py + 2, cell - 4, cell - 4, 4); ctx.stroke();
      if (cell >= 18) { ctx.fillStyle = "#f5b800"; ctx.font = `700 ${Math.max(7, cell * .26)}px system-ui`; ctx.fillText(id.replace("DK", "D"), px + cell / 2, py + cell / 2); }
    }
    // excavators: gold body, the dig face they work
    for (const [id, s] of Object.entries(map.slots)) {
      const [x, y] = s.cell, px = x * cell, py = y * cell;
      ctx.fillStyle = "#f5b800"; ctx.shadowColor = "#f5b800"; ctx.shadowBlur = 8;
      roundRect(ctx, px + cell * .14, py + cell * .14, cell * .72, cell * .72, cell * .14); ctx.fill(); ctx.shadowBlur = 0;
      ctx.fillStyle = "#1a1200"; ctx.font = `800 ${Math.max(7, cell * .3)}px system-ui`;
      ctx.fillText(id, px + cell / 2, py + cell / 2 + 1);
    }
    // park and workshop bays
    for (const [x, y] of map.homes || []) { ctx.strokeStyle = "rgba(255,240,210,.18)"; ctx.strokeRect(x * cell + 3, y * cell + 3, cell - 6, cell - 6); }
    for (const [x, y] of map.garage || []) { ctx.strokeStyle = "rgba(255,59,79,.35)"; ctx.setLineDash([3, 3]); ctx.strokeRect(x * cell + 3, y * cell + 3, cell - 6, cell - 6); ctx.setLineDash([]); }
    if (cell >= 18) {
      ctx.font = `600 ${Math.max(7, cell * .24)}px system-ui`; ctx.fillStyle = "rgba(255,240,210,.5)";
      const h0 = (map.homes || [])[0], g0 = (map.garage || [])[1];
      if (h0) ctx.fillText("TRUCK PARK", (h0[0] + 4) * cell, (h0[1] + .5) * cell);
      if (g0) ctx.fillText("WORKSHOP", (g0[0] + .5) * cell, (g0[1] + .5) * cell);
    }
    return off;
  }

  // ------------------------------------------------------------ state fed by the caller
  resetTrails() { this.trails.clear(); }

  accumulate(frame) {  // congestion heat: where trucks stand holding
    for (let i = 0; i < this.heat.length; i++) this.heat[i] *= 0.996;
    for (const r of frame.robots) {
      if (["waiting", "blocked", "held", "queued"].includes(r.st)) {
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
      case "rock_fell": {
        const [x, y] = cellMM(ev.cell);
        add({ kind: "dust", x, y, dur: 900, color: "#b98a5e" });
        break;
      }
      case "pothole_formed": {
        const [x, y] = cellMM(ev.cell);
        add({ kind: "dust", x, y, dur: 900, color: "#6b5238" });
        add({ kind: "text", x, y, dur: 1500, color: cssVar("--warn"), text: "road damage" });
        break;
      }
      case "pothole_detected": {
        const [x, y] = cellMM(ev.cell);
        add({ kind: "flash", x, y, dur: 700, color: cssVar("--accent") });
        break;
      }
      case "dock_scan": {
        const c = this.map.docks[ev.dock]; if (!c) break;
        const [x, y] = cellMM(c);
        add({ kind: "flash", x, y, dur: 900, color: ev.ok ? cssVar("--ok") : cssVar("--bad") });
        add({ kind: "text", x, y, dur: 1500, color: ev.ok ? cssVar("--ok") : cssVar("--bad"),
          text: ev.ok ? `✓ tipped` : "✕ WRONG MATERIAL" });
        break;
      }
      case "pick": {
        const c = this.map.slots[ev.slot]; if (!c) break;
        const [x, y] = cellMM(c.cell);
        add({ kind: "flash", x, y, dur: 600, color: cssVar("--accent") });
        break;
      }
      case "scan_mismatch": {
        const c = this.map.slots[ev.slot]; if (!c) break;
        const [x, y] = cellMM(c.cell);
        add({ kind: "flash", x, y, dur: 1200, color: cssVar("--warn") });
        add({ kind: "text", x, y, dur: 1700, color: cssVar("--warn"), text: "grade check caught it" });
        break;
      }
      case "zone_enter":
        if (r) add({ kind: "shock", x: r.x, y: r.y, dur: 900, color: cssVar("--bad") }),
          add({ kind: "text", x: r.x, y: r.y, dur: 1500, color: cssVar("--bad"), text: "BLAST ZONE BREACH" });
        break;
      case "job_exception":
        if (r) add({ kind: "text", x: r.x, y: r.y, dur: 1500, color: cssVar("--warn"), text: `exception: ${ev.reason}` });
        break;
      case "policy_applied":
        add({ kind: "banner", dur: 1800, color: cssVar("--accent"), text: `haul rules v${ev.version} live` });
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

  cellAt(clientX, clientY) {
    const rect = this.c.getBoundingClientRect();
    return [Math.floor((clientX - rect.left) / this.cell), Math.floor((clientY - rect.top) / this.cell)];
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
    this.drawHoles(ctx, a);
    this.drawRocks(ctx, a, now);

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
      ctx.fillStyle = `rgba(255,59,79,${Math.min(.55, v / 60)})`;
      ctx.fillRect(x * cell, y * cell, cell, cell);
    }
  }

  drawZones(ctx, a, now) {
    const { cell } = this;
    const pulse = .14 + .08 * Math.sin(now / 260);
    for (const z of a.zones || []) {
      const cells = this.map.zones[z] || [];
      ctx.fillStyle = `rgba(255,59,79,${pulse})`;
      for (const [x, y] of cells) ctx.fillRect(x * cell, y * cell, cell, cell);
      ctx.save(); ctx.setLineDash([6, 5]); ctx.lineDashOffset = -now / 40; ctx.strokeStyle = "rgba(255,59,79,.85)"; ctx.lineWidth = 1.5;
      const xs = cells.map((c) => c[0]), ys = cells.map((c) => c[1]);
      ctx.strokeRect(Math.min(...xs) * cell + 1, Math.min(...ys) * cell + 1, (Math.max(...xs) - Math.min(...xs) + 1) * cell - 2,
        (Math.max(...ys) - Math.min(...ys) + 1) * cell - 2);
      ctx.restore();
      if (cells.length && cell >= 14) {
        const mx = (Math.min(...xs) + Math.max(...xs) + 1) / 2 * cell, my = (Math.min(...ys) + .5) * cell;
        ctx.fillStyle = "#ff8a95"; ctx.font = `800 ${Math.max(8, cell * .3)}px system-ui`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
        ctx.fillText("BLAST · CLOSED", mx, my);
      }
    }
  }

  drawHoles(ctx, a) {
    const { cell } = this;
    for (const [x, y, depth, known] of a.holes || []) {
      const cx = (x + .5) * cell, cy = (y + .5) * cell, r = cell * (.16 + depth / 6000);
      ctx.save();
      const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, r);
      g.addColorStop(0, "rgba(10,6,3,.95)"); g.addColorStop(.7, "rgba(35,22,12,.85)"); g.addColorStop(1, "rgba(60,40,22,0)");
      ctx.fillStyle = g; ctx.beginPath(); ctx.ellipse(cx, cy, r * 1.25, r, 0, 0, Math.PI * 2); ctx.fill();
      if (known) { ctx.strokeStyle = "rgba(245,184,0,.75)"; ctx.lineWidth = 1.2; ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.arc(cx, cy, r * 1.6, 0, Math.PI * 2); ctx.stroke(); }
      ctx.restore();
    }
  }

  drawRocks(ctx, a, now) {
    const { cell } = this;
    const live = new Set();
    for (const c of a.rocks || a.pallets || []) {
      const key = c.join(","); live.add(key);
      if (!this.rocks.has(key)) this.rocks.set(key, now);
      const k = Math.min(1, (now - this.rocks.get(key)) / 420), fall = 1 - k;   // falls in from above
      const s = cell * .5 * (1 + fall * .6), cx = (c[0] + .5) * cell, cy = (c[1] + .5) * cell - fall * cell * .9;
      ctx.save();
      ctx.fillStyle = "rgba(0,0,0,.45)"; ctx.beginPath();
      ctx.ellipse((c[0] + .5) * cell, (c[1] + .5) * cell + cell * .2, s * .55 * (1 - fall * .5), s * .18, 0, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = "#8a6a4a"; ctx.strokeStyle = "#3a2b1c"; ctx.lineWidth = 1.5;
      ctx.beginPath();
      const pts = 7;
      for (let i = 0; i < pts; i++) {
        const ang = i / pts * Math.PI * 2, rr = s * (.42 + ((i * 37) % 5) * .035);
        const px = cx + Math.cos(ang) * rr, py = cy + Math.sin(ang) * rr * .85;
        i ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
      }
      ctx.closePath(); ctx.fill(); ctx.stroke();
      ctx.restore();
    }
    for (const k of [...this.rocks.keys()]) if (!live.has(k)) this.rocks.delete(k);
  }

  drawPaths(ctx, pos, now) {
    const { cell, mm } = this;
    ctx.save(); ctx.setLineDash([4, 6]); ctx.lineDashOffset = -now / 30; ctx.lineWidth = 2;
    for (const p of pos.values()) {
      if (!p.r.p || !p.r.p.length) continue;
      ctx.strokeStyle = "rgba(90,255,140,.65)"; ctx.beginPath(); ctx.moveTo(p.x * mm, p.y * mm);
      for (const [x, y] of p.r.p) ctx.lineTo((x + .5) * cell, (y + .5) * cell);
      ctx.stroke();
    }
    ctx.restore();
  }

  drawTrails(ctx, pos) {
    const { mm } = this;
    const step = this.map.cell_mm * .06, jump = this.map.cell_mm * 2.5;
    for (const p of pos.values()) {
      let t = this.trails.get(p.r.id);
      if (!t) { t = []; this.trails.set(p.r.id, t); }
      const last = t[t.length - 1];
      if (!last || Math.hypot(last.x - p.x, last.y - p.y) > step) { t.push({ x: p.x, y: p.y }); if (t.length > TRAIL) t.shift(); }
      if (last && Math.hypot(last.x - p.x, last.y - p.y) > jump) t.splice(0, t.length - 1);  // jumped (seek): restart
      if (t.length < 2) continue;
      const col = robotColor(p.r.id);
      ctx.lineCap = "round";
      for (let i = 1; i < t.length; i++) {
        ctx.strokeStyle = col; ctx.globalAlpha = (i / t.length) * .45; ctx.lineWidth = 1 + (i / t.length) * 3;
        ctx.beginPath(); ctx.moveTo(t[i - 1].x * mm, t[i - 1].y * mm); ctx.lineTo(t[i].x * mm, t[i].y * mm); ctx.stroke();
      }
      ctx.globalAlpha = 1;
    }
  }

  drawSensor(ctx, p) {
    const d = DIRS[p.r.d]; if (!d || p.v <= 0) return;
    const { mm } = this, ph = phys();
    const half = ph.robot_half || 5000, sensor = (ph.lidar_m || 60) * 1000, clear = (ph.clearance_m || 3) * 1000;
    const blind = stopDist(Math.round(p.v)) + clear > sensor;    // can't stop within what it can see
    const reach = Math.min(sensor, stopDist(Math.round(p.v)) + clear + half);
    const x0 = p.x + d[0] * half, y0 = p.y + d[1] * half;
    const x1 = x0 + d[0] * reach, y1 = y0 + d[1] * reach;
    const sp = half * .9, spread = half * 1.4;
    const px = -d[1], py = d[0];
    ctx.save();
    const g = ctx.createLinearGradient(x0 * mm, y0 * mm, x1 * mm, y1 * mm);
    const c = blind ? "255,59,79" : "245,184,0";
    g.addColorStop(0, `rgba(${c},${blind ? .4 : .22})`); g.addColorStop(1, `rgba(${c},0)`);
    ctx.fillStyle = g; ctx.beginPath();
    ctx.moveTo((x0 + px * sp) * mm, (y0 + py * sp) * mm); ctx.lineTo((x1 + px * spread) * mm, (y1 + py * spread) * mm);
    ctx.lineTo((x1 - px * spread) * mm, (y1 - py * spread) * mm); ctx.lineTo((x0 - px * sp) * mm, (y0 - py * sp) * mm);
    ctx.closePath(); ctx.fill(); ctx.restore();
  }

  drawGhosts(ctx, ghost, pos) {
    const { mm, cell } = this;
    const { a, b, alpha } = ghost;
    if (!a) return;
    const s = cell * .62;
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
      roundRect(ctx, x - s / 2, y - s / 2, s, s, s * .22); ctx.stroke(); ctx.globalAlpha = 1;
    }
    for (const c of ghost.a.rocks || ghost.a.pallets || []) {
      ctx.setLineDash([3, 3]); ctx.strokeStyle = "#b98a5e";
      ctx.strokeRect(c[0] * cell + cell * .25, c[1] * cell + cell * .25, cell * .5, cell * .5);
    }
    ctx.restore();
  }

  drawRobot(ctx, p, now, dim) {
    const { mm, cell } = this;
    const r = p.r, x = p.x * mm, y = p.y * mm, col = robotColor(r.id);
    const d = DIRS[r.d] || [1, 0];
    const L = cell * .74, W = cell * .48;   // a haul truck: longer than it is wide
    this.drawn.set(r.id, { x, y });
    ctx.save();
    ctx.translate(x, y); ctx.rotate(Math.atan2(d[1], d[0]));
    if (dim) ctx.globalAlpha = .35;
    ctx.shadowColor = col; ctx.shadowBlur = r.st === "idle" ? 4 : 12;
    ctx.fillStyle = "#e3a800"; roundRect(ctx, -L / 2, -W / 2, L, W, W * .2); ctx.fill();   // yellow body
    ctx.shadowBlur = 0;
    ctx.fillStyle = r.c ? "#6b4a30" : "#3a2f22"; roundRect(ctx, -L / 2 + L * .06, -W / 2 + W * .14, L * .6, W * .72, W * .12); ctx.fill();   // tray
    ctx.fillStyle = col; ctx.fillRect(L / 2 - L * .16, -W / 2 + W * .1, L * .1, W * .8);   // accent stripe on the cab end
    const ring = r.st === "estop" || r.st === "fault" ? cssVar("--bad") : ["waiting", "blocked", "held", "queued"].includes(r.st) ? cssVar("--warn") : null;
    if (ring) {
      ctx.strokeStyle = ring; ctx.lineWidth = 2.5; ctx.globalAlpha = r.st === "estop" || r.st === "fault" ? .6 + .4 * Math.sin(now / 90) : .9;
      roundRect(ctx, -L / 2 - 3, -W / 2 - 3, L + 6, W + 6, W * .3); ctx.stroke(); ctx.globalAlpha = dim ? .35 : 1;
    }
    ctx.restore();
    ctx.save();
    ctx.fillStyle = "#111"; ctx.font = `800 ${Math.max(8, cell * .26)}px system-ui`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillStyle = "rgba(0,0,0,.55)"; roundRect(ctx, x - cell * .22, y - cell * .56, cell * .44, cell * .24, 4); ctx.fill();
    ctx.fillStyle = col; ctx.fillText(r.id.replace("T0", "T"), x, y - cell * .44);
    if (this.selected === r.id) {
      const k = (now % 1200) / 1200;
      ctx.strokeStyle = col; ctx.lineWidth = 2; ctx.globalAlpha = 1 - k;
      ctx.beginPath(); ctx.arc(x, y, cell * (.5 + k * .5), 0, Math.PI * 2); ctx.stroke();
      ctx.globalAlpha = 1; ctx.beginPath(); ctx.arc(x, y, cell * .52, 0, Math.PI * 2); ctx.stroke();
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
        ctx.fillStyle = e.color; ctx.globalAlpha = (1 - k) * .55;
        for (let i = 0; i < 14; i++) {
          const ang = i / 14 * Math.PI * 2, rr = this.cell * (.2 + k * (.8 + (i % 3) * .2));
          ctx.beginPath(); ctx.arc(x + Math.cos(ang) * rr, y + Math.sin(ang) * rr, 3 * (1 - k) + 1, 0, Math.PI * 2); ctx.fill();
        }
      } else if (e.kind === "flash") {
        const g = ctx.createRadialGradient(x, y, 1, x, y, this.cell * 1.4);
        g.addColorStop(0, e.color); g.addColorStop(1, "transparent");
        ctx.globalAlpha = (1 - k) * .7; ctx.fillStyle = g; ctx.fillRect(x - this.cell * 1.5, y - this.cell * 1.5, this.cell * 3, this.cell * 3);
      } else if (e.kind === "text") {
        ctx.globalAlpha = k < .8 ? 1 : (1 - k) / .2;
        ctx.font = `700 ${Math.max(10, this.cell * .32)}px system-ui`; ctx.textAlign = "center";
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

  // name of a face / dump under a canvas point (for tooltips)
  describeCell(c) {
    const k = c.join(",");
    const s = this.slotAt[k];
    if (s) return { kind: "face", slot: s, title: `${excavator(s).id} · face ${s}` };
    const d = this.dockAt[k];
    if (d) return { kind: "dump", dock: d, title: destination(d) };
    return null;
  }
}
