// The live open pit in 3D: built from the sim's own map (one scene unit = one 20 m road segment, cell (x, y) ->
// (x + .5, y + .5); elevations from the map, exaggerated so the benches read from the air), driven by the live
// 10 Hz frames and interpolated at display rate. Nothing here is scripted: every haul, hold, standoff, ruling,
// pothole, load and dump is the fleet on VM B; the dust, lidar returns and machine motion dress it.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { CSS2DRenderer, CSS2DObject } from "three/addons/renderers/CSS2DRenderer.js";
import { grain, softDot, dustPuff, afternoonSky, lightRoad, sandRipples, radialGlow, hazard, chevrons } from "./textures.js";
import { HaulTruck, Excavator, makeDozer, makeDrillRig, makeLightTower, makeServiceUte, makeCrusher } from "./machines.js";

const M = 1 / 20;                  // metres -> scene units
const VEX = 1.5;                   // vertical exaggeration
const MACHINE = 1.15 * M;          // machines are modelled in metres; slightly enlarged so they read from the air
const TRUCK = 1.5 * M;             // the haul trucks are the story: a size up again so each reads from the overview
const HM = (m) => m * VEX * M;     // elevation (m) -> scene height
const MAT = { ore: 0x8f5d3b, lowgrade: 0xb08a62, waste: 0x958c80, sand: 0xe9cf9a };
const HAZE = 0xe8dcc4;          // afternoon dust haze: fog, far dust
const easeInOut = (k) => (k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2);
const smooth = (a, b, x) => { const t = Math.min(1, Math.max(0, (x - a) / (b - a))); return t * t * (3 - 2 * t); };
const hash = (x, y) => { const s = Math.sin(x * 127.1 + y * 311.7) * 43758.5453; return s - Math.floor(s); };
function vnoise(x, y) {
  const xi = Math.floor(x), yi = Math.floor(y), xf = x - xi, yf = y - yi;
  const u = xf * xf * (3 - 2 * xf), v = yf * yf * (3 - 2 * yf);
  const a = hash(xi, yi), b = hash(xi + 1, yi), c = hash(xi, yi + 1), d = hash(xi + 1, yi + 1);
  return a + (b - a) * u + (c - a) * v + (a - b - c + d) * u * v;
}
const fbm = (x, y) => vnoise(x, y) * .55 + vnoise(x * 2.3, y * 2.3) * .3 + vnoise(x * 5.1, y * 5.1) * .15;
const YAW = { E: 0, N: Math.PI / 2, W: Math.PI, S: -Math.PI / 2 };
const UP = new THREE.Vector3(0, 1, 0);

// ------------------------------------------------------------------ terrain
class Terrain {
  constructor(map) {
    this.map = map; this.W = map.width; this.H = map.height;
    this.margin = 16; this.res = 5;
    const flat = (x, y) => { const ch = (map.layout[y] || "")[x]; return ch !== undefined && ch !== "#"; };   // roads, pockets, excavator pads
    this.flat = flat;
    // no bench is dead level: a ~3 m fall along the pit and a slow ±0.8 m roll, so roads, berms and pads carry a grade
    this.tilt = (x, y) => 3 * (.5 - x / this.W) + (vnoise(x * .16 + 4.3, y * .23 + 1.7) - .5) * 1.6;
    this.level = (x, y) => ((map.elev[y] || [])[x] ?? map.rim_m) + this.tilt(x, y);
    this.nx = (this.W + this.margin * 2) * this.res + 1; this.nz = (this.H + this.margin * 2) * this.res + 1;
    this.h = new Float32Array(this.nx * this.nz);
    const flats = [];
    for (let y = 0; y < this.H; y++) for (let x = 0; x < this.W; x++) if (flat(x, y)) flats.push([x, y, this.level(x, y)]);
    this.flats = flats;
    const byCell = new Map(flats.map((f) => [`${f[0]},${f[1]}`, f]));
    const PLATEAU = map.rim_m + 7;
    const docks = Object.values(map.docks);
    for (let j = 0; j < this.nz; j++) {
      for (let i = 0; i < this.nx; i++) {
        const wx = i / this.res - this.margin, wz = j / this.res - this.margin;
        const cx = Math.floor(wx), cy = Math.floor(wz);
        let m;
        if (byCell.has(`${cx},${cy}`)) m = this.roadLevel(wx, wz, byCell);
        else {
          let best = 1e9;
          const far = cx < -6 || cy < -6 || cx > this.W + 5 || cy > this.H + 5;   // the outer plateau: no roads to terrace down to
          if (!far) for (let y = cy - 5; y <= cy + 5; y++) for (let x = cx - 5; x <= cx + 5; x++) {
            const f = byCell.get(`${x},${y}`); if (!f) continue;
            const dx = Math.max(x - wx, 0, wx - (x + 1)), dz = Math.max(y - wz, 0, wz - (y + 1));
            const d = Math.hypot(dx, dz);
            const h = f[2] + this.stair(d, wx, wz);
            if (h < best) best = h;
          }
          const inside = cx > 0 && cy > 1 && cx < this.W - 1 && cy < this.H - 1 && !(cy === this.H - 2);
          const cap = inside ? this.level(cx, cy) : PLATEAU + (fbm(wx * .25, wz * .25) - .5) * 5;
          m = Math.min(best, cap) + (fbm(wx * 1.7, wz * 1.7) - .5) * 1.2;
          // the tipping heads: a waste dump to the north-west, stockpiles behind their dump points
          for (const [dx, dy] of docks) {
            const d = Math.hypot(wx - (dx + .5), wz - (dy - 1.9));
            if (d < 3.4) m = Math.max(m, map.rim_m + 2 + 10 * smooth(3.4, 1.2, d));
          }
        }
        this.h[j * this.nx + i] = HM(m);
      }
    }
  }

  stair(d, wx, wz) {   // terraced highwall: 10 m benches, steep faces, flat berms
    const P = 0.3, F = 0.07;
    const k = Math.floor(d / P), f = smooth(0, F, d - k * P);
    return 10 * (k + f) + (fbm(wx * 3, wz * 3) - .5) * 2.2 * Math.min(1, d * 4);
  }

  roadLevel(wx, wz, byCell) {
    const x0 = Math.floor(wx - .5), z0 = Math.floor(wz - .5), fx = wx - .5 - x0, fz = wz - .5 - z0;
    let s = 0, w = 0;
    for (const [dx, dz, ww] of [[0, 0, (1 - fx) * (1 - fz)], [1, 0, fx * (1 - fz)], [0, 1, (1 - fx) * fz], [1, 1, fx * fz]]) {
      const f = byCell.get(`${x0 + dx},${z0 + dz}`);
      if (f && ww > 0) { s += f[2] * ww; w += ww; }
    }
    return w ? s / w : this.level(Math.floor(wx), Math.floor(wz));
  }

  height(wx, wz) {   // bilinear lookup, scene units
    const fi = (wx + this.margin) * this.res, fj = (wz + this.margin) * this.res;
    const i = Math.max(0, Math.min(this.nx - 2, Math.floor(fi))), j = Math.max(0, Math.min(this.nz - 2, Math.floor(fj)));
    const u = Math.min(1, Math.max(0, fi - i)), v = Math.min(1, Math.max(0, fj - j));
    const a = this.h[j * this.nx + i], b = this.h[j * this.nx + i + 1], c = this.h[(j + 1) * this.nx + i], d = this.h[(j + 1) * this.nx + i + 1];
    return a + (b - a) * u + (c - a) * v + (a - b - c + d) * u * v;
  }

  mesh() {
    const w = this.W + this.margin * 2, d = this.H + this.margin * 2;
    const g = new THREE.PlaneGeometry(w, d, this.nx - 1, this.nz - 1);
    g.rotateX(-Math.PI / 2);
    g.translate(w / 2 - this.margin, 0, d / 2 - this.margin);
    const pos = g.attributes.position, col = new Float32Array(pos.count * 3), c = new THREE.Color();
    const pit = new THREE.Color(0xcfae80), rockLo = new THREE.Color(0xb48e62), rockHi = new THREE.Color(0xe3c79a),
      dirt = new THREE.Color(0xeed8b0), desert = new THREE.Color(0xf0dab0), face = new THREE.Color(0xa47a52);
    for (let k = 0; k < pos.count; k++) {
      const x = pos.getX(k), z = pos.getZ(k);
      const i = Math.round((x + this.margin) * this.res), j = Math.round((z + this.margin) * this.res);
      const y = this.h[Math.min(this.nz - 1, j) * this.nx + Math.min(this.nx - 1, i)];
      pos.setY(k, y);
      const hx = this.height(x + .08, z) - this.height(x - .08, z), hz = this.height(x, z + .08) - this.height(x, z - .08);
      const slope = Math.min(1, Math.hypot(hx, hz) * 5);
      const inPit = x > 0 && z > 0 && x < this.W && z < this.H;
      const depth = Math.min(1, Math.max(0, -y / HM(48)));
      c.copy(inPit ? rockHi : desert).lerp(rockLo, depth * .7);
      if (inPit && this.flat(Math.floor(x), Math.floor(z))) c.copy(dirt).lerp(pit, depth * .45);
      c.lerp(face, slope * .75);
      const band = Math.sin(y * 38) * .5 + .5;              // strata on the faces
      c.multiplyScalar((1 - slope * band * .2 + (fbm(x * 2, z * 2) - .5) * .14) * 1.32);   // the grain map darkens by ~25%
      col[k * 3] = c.r; col[k * 3 + 1] = c.g; col[k * 3 + 2] = c.b;
    }
    g.setAttribute("color", new THREE.BufferAttribute(col, 3));
    g.computeVertexNormals();
    const t = grain().clone(); t.needsUpdate = true; t.repeat.set(w / 2.5, d / 2.5);
    const m = new THREE.Mesh(g, new THREE.MeshStandardMaterial({ vertexColors: true, map: t, roughness: .97, metalness: 0 }));
    m.receiveShadow = true;
    return m;
  }
}

// ------------------------------------------------------------------ dust
class Dust {
  constructor(scene, max = 7000) {
    this.max = max; this.n = 0; this.head = 0;
    this.pos = new Float32Array(max * 3); this.vel = new Float32Array(max * 3);
    this.size = new Float32Array(max); this.grow = new Float32Array(max); this.alpha = new Float32Array(max);
    this.life = new Float32Array(max); this.maxLife = new Float32Array(max); this.a0 = new Float32Array(max);
    this.col = new Float32Array(max * 3);
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(this.pos, 3).setUsage(THREE.DynamicDrawUsage));
    g.setAttribute("aSize", new THREE.BufferAttribute(this.size, 1).setUsage(THREE.DynamicDrawUsage));
    g.setAttribute("aAlpha", new THREE.BufferAttribute(this.alpha, 1).setUsage(THREE.DynamicDrawUsage));
    g.setAttribute("aColor", new THREE.BufferAttribute(this.col, 3).setUsage(THREE.DynamicDrawUsage));
    this.mat = new THREE.ShaderMaterial({
      uniforms: { map: { value: dustPuff() }, scale: { value: 400 }, fogColor: { value: new THREE.Color(HAZE) },
        fogNear: { value: 60 }, fogFar: { value: 190 } },
      vertexShader: `attribute float aSize; attribute float aAlpha; attribute vec3 aColor; varying float vA; varying vec3 vC; varying float vFog;
        uniform float scale; uniform float fogNear; uniform float fogFar;
        void main() { vec4 mv = modelViewMatrix * vec4(position, 1.0); gl_Position = projectionMatrix * mv;
          gl_PointSize = aSize * scale / max(0.5, -mv.z); vA = aAlpha; vC = aColor; vFog = smoothstep(fogNear, fogFar, -mv.z); }`,
      fragmentShader: `uniform sampler2D map; uniform vec3 fogColor; varying float vA; varying vec3 vC; varying float vFog;
        void main() { vec4 t = texture2D(map, gl_PointCoord); gl_FragColor = vec4(mix(vC, fogColor, vFog), t.a * vA);
          if (gl_FragColor.a < 0.004) discard; }`,
      transparent: true, depthWrite: false,
    });
    this.points = new THREE.Points(g, this.mat);
    this.points.frustumCulled = false; this.points.renderOrder = 5;
    scene.add(this.points);
    this.wind = new THREE.Vector3(.12, 0, .05);
  }

  emit(x, y, z, o = {}) {
    const n = o.n ?? 1;
    const c = new THREE.Color(o.color ?? 0xc9a57a);
    for (let k = 0; k < n; k++) {
      const i = this.head; this.head = (this.head + 1) % this.max; this.n = Math.min(this.max, this.n + 1);
      const sp = o.spread ?? .05;
      this.pos[i * 3] = x + (Math.random() - .5) * sp; this.pos[i * 3 + 1] = y + Math.random() * (o.lift ?? .02); this.pos[i * 3 + 2] = z + (Math.random() - .5) * sp;
      const v = o.vel || [0, .1, 0], j = o.jitter ?? .08;
      this.vel[i * 3] = v[0] + (Math.random() - .5) * j; this.vel[i * 3 + 1] = v[1] + Math.random() * j * .6; this.vel[i * 3 + 2] = v[2] + (Math.random() - .5) * j;
      this.size[i] = (o.size ?? .35) * (.7 + Math.random() * .6); this.grow[i] = o.grow ?? .5;
      this.maxLife[i] = (o.life ?? 3) * (.7 + Math.random() * .6); this.life[i] = 0; this.a0[i] = o.alpha ?? .45;
      const tint = .88 + Math.random() * .24;
      this.col[i * 3] = c.r * tint; this.col[i * 3 + 1] = c.g * tint; this.col[i * 3 + 2] = c.b * tint;
    }
  }

  update(dt, cam) {
    this.mat.uniforms.scale.value = (this.renderHeight || 800) * .5;
    const w = this.wind;
    for (let i = 0; i < this.max; i++) {
      if (this.life[i] >= this.maxLife[i]) { this.alpha[i] = 0; continue; }
      this.life[i] += dt;
      const k = this.life[i] / this.maxLife[i];
      this.vel[i * 3 + 1] *= (1 - dt * .6);
      this.pos[i * 3] += (this.vel[i * 3] + w.x * k) * dt; this.pos[i * 3 + 1] += this.vel[i * 3 + 1] * dt; this.pos[i * 3 + 2] += (this.vel[i * 3 + 2] + w.z * k) * dt;
      this.vel[i * 3] *= (1 - dt * .4); this.vel[i * 3 + 2] *= (1 - dt * .4);
      this.size[i] += this.grow[i] * dt;
      this.alpha[i] = this.a0[i] * Math.min(1, k * 8) * (1 - k) * (1 - k);
    }
    const g = this.points.geometry;
    g.attributes.position.needsUpdate = g.attributes.aSize.needsUpdate = g.attributes.aAlpha.needsUpdate = g.attributes.aColor.needsUpdate = true;
  }
}

// ------------------------------------------------------------------ lidar returns
class Lidar {
  // Each truck's roof lidar paints the ground and highwalls around it: concentric return rings out to 60 m,
  // brightest where the rotating head has just swept. Obstacles it has classified glow on top.
  constructor(scene, terrain, rangeUnits) {
    this.terrain = terrain; this.range = rangeUnits;
    this.rings = [0.45, 0.7, 0.98, 1.3, 1.65, 2.05, 2.5, 3.0].filter((r) => r <= rangeUnits + .01);
    this.beams = 120;
    this.per = this.rings.length * this.beams;
    this.maxTrucks = 16;
    const n = this.per * this.maxTrucks;
    this.pos = new Float32Array(n * 3); this.alpha = new Float32Array(n); this.col = new Float32Array(n * 3);
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(this.pos, 3).setUsage(THREE.DynamicDrawUsage));
    g.setAttribute("aAlpha", new THREE.BufferAttribute(this.alpha, 1).setUsage(THREE.DynamicDrawUsage));
    g.setAttribute("aColor", new THREE.BufferAttribute(this.col, 3).setUsage(THREE.DynamicDrawUsage));
    this.mat = new THREE.ShaderMaterial({
      uniforms: { map: { value: softDot() }, size: { value: 3.2 } },
      vertexShader: `attribute float aAlpha; attribute vec3 aColor; varying float vA; varying vec3 vC; uniform float size;
        void main() { vec4 mv = modelViewMatrix * vec4(position, 1.0); gl_Position = projectionMatrix * mv; gl_PointSize = size * (18.0 / max(4.0, -mv.z)) + 1.2; vA = aAlpha; vC = aColor; }`,
      fragmentShader: `uniform sampler2D map; varying float vA; varying vec3 vC; void main() { float a = texture2D(map, gl_PointCoord).a * vA; if (a < 0.01) discard; gl_FragColor = vec4(vC, a); }`,
      transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    });
    this.points = new THREE.Points(g, this.mat); this.points.frustumCulled = false; this.points.renderOrder = 6;
    scene.add(this.points);
    this.gold = new THREE.Color(0xffc23a); this.hot = new THREE.Color(0xff4d3a); this.white = new THREE.Color(0xfff1d0);
  }

  update(trucks, now, hazards, selected, on) {
    this.points.visible = on;
    if (!on) return;
    let t = 0;
    for (const tr of trucks) {
      if (t >= this.maxTrucks) break;
      const base = t * this.per, p = tr.group.position, sweep = (now / 1000 * 2.2 + t * 1.3) % (Math.PI * 2);
      const dim = tr.lidarDim ?? 1, focus = !selected || selected === tr.id ? 1 : .35;
      for (let r = 0; r < this.rings.length; r++) {
        const rad = this.rings[r];
        for (let b = 0; b < this.beams; b++) {
          const k = base + r * this.beams + b, a = b / this.beams * Math.PI * 2;
          const x = p.x + Math.cos(a) * rad, z = p.z + Math.sin(a) * rad;
          let y = this.terrain.height(x, z);
          const wall = y - p.y > .35;                       // a highwall face: the return sits on it
          this.pos[k * 3] = x; this.pos[k * 3 + 1] = y + .02; this.pos[k * 3 + 2] = z;
          let lag = (sweep - a) % (Math.PI * 2); if (lag < 0) lag += Math.PI * 2;
          const fresh = Math.max(0, 1 - lag / (Math.PI * 1.6));
          let c = wall ? this.white : this.gold, boost = 0;
          for (const hz of hazards) {
            const d = Math.hypot(hz.x - x, hz.z - z);
            if (d < hz.r) { c = this.hot; boost = .5; break; }
          }
          this.col[k * 3] = c.r; this.col[k * 3 + 1] = c.g; this.col[k * 3 + 2] = c.b;
          this.alpha[k] = (0.06 + fresh * .55 + boost * fresh) * dim * focus * (1 - r / (this.rings.length + 2)) * (wall ? 1.2 : 1);
        }
      }
      t++;
    }
    for (let k = t * this.per; k < this.maxTrucks * this.per; k++) this.alpha[k] = 0;
    const g = this.points.geometry;
    g.attributes.position.needsUpdate = g.attributes.aAlpha.needsUpdate = g.attributes.aColor.needsUpdate = true;
  }
}

// ------------------------------------------------------------------ lidar survey
// Every 5 s (the sim's survey beat) the fleet's lidar re-surveys the pit: a scan sheet sweeps across it, laying a
// square grid over the ground that rides every rise and dip, with height contours, and the road as it now is
// (new sand drifts, bumps, potholes) appears behind the sweep.
const SCAN_VS = `uniform float uFront; varying vec3 vW;
  void main() { vec4 w = modelMatrix * vec4(position, 1.0);
    w.y += exp(-abs(w.z - uFront) * 3.0) * 0.07;          // the grid ripples up as the sheet passes
    vW = w.xyz; gl_Position = projectionMatrix * viewMatrix * w; }`;
const SCAN_FS = `uniform float uFront; uniform float uFade; uniform vec3 uGold; uniform vec3 uHot; varying vec3 vW;
  float grid(float v, float k) { float g = abs(fract(v * k - 0.5) - 0.5) / fwidth(v * k); return 1.0 - min(g, 1.0); }
  void main() {
    float lines = max(grid(vW.x, 2.0), grid(vW.z, 2.0));
    float contour = grid(vW.y, 7.0) * 0.55;
    float behind = uFront - vW.z;
    float trail = behind > 0.0 ? exp(-behind * 0.22) : 0.0;
    float edge = exp(-abs(behind) * 5.0);
    float a = (lines * 0.75 + contour) * trail * uFade + edge * 0.55 * step(0.0, uFade - 0.01);
    if (a < 0.01) discard;
    gl_FragColor = vec4(mix(uGold, uHot, edge), min(a, 0.9));
  }`;

class Survey {
  constructor(scene, terrainMesh, W, H) {
    this.W = W; this.H = H; this.t0 = -1e9; this.dur = 1700; this.linger = 900;
    this.mat = new THREE.ShaderMaterial({
      uniforms: { uFront: { value: -100 }, uFade: { value: 0 }, uGold: { value: new THREE.Color(0xffb400) }, uHot: { value: new THREE.Color(0xfff4d0) } },
      vertexShader: SCAN_VS, fragmentShader: SCAN_FS, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
      polygonOffset: true, polygonOffsetFactor: -4, polygonOffsetUnits: -4,
    });
    this.grid = new THREE.Mesh(terrainMesh.geometry, this.mat);
    this.grid.position.y = .02; this.grid.renderOrder = 9; this.grid.visible = false;
    scene.add(this.grid);
    // the scan sheet: a tall curtain of light at the sweep front
    const c = document.createElement("canvas"); c.width = 4; c.height = 128;
    const g = c.getContext("2d"), gr = g.createLinearGradient(0, 0, 0, 128);
    gr.addColorStop(0, "rgba(255,200,80,0)"); gr.addColorStop(.75, "rgba(255,200,80,.22)"); gr.addColorStop(1, "rgba(255,240,200,.75)");
    g.fillStyle = gr; g.fillRect(0, 0, 4, 128);
    const t = new THREE.CanvasTexture(c);
    this.sheet = new THREE.Mesh(new THREE.PlaneGeometry(W + 24, 7), new THREE.MeshBasicMaterial({ map: t, transparent: true, depthWrite: false,
      blending: THREE.AdditiveBlending, side: THREE.DoubleSide, fog: false }));
    this.sheet.position.set(W / 2, -1.2, 0); this.sheet.renderOrder = 10; this.sheet.visible = false;
    scene.add(this.sheet);
  }

  start(now) { this.t0 = now; }

  front(now) { return -4 + (this.H + 8) * Math.min(1, (now - this.t0) / this.dur); }

  active(now) { return now - this.t0 < this.dur + this.linger; }

  update(now, on) {
    const k = (now - this.t0) / this.dur;
    const live = on && k >= 0 && now - this.t0 < this.dur + this.linger;
    this.grid.visible = live; this.sheet.visible = live && k < 1;
    if (!live) return;
    const z = this.front(now);
    this.mat.uniforms.uFront.value = k < 1 ? z : this.H + 40;
    this.mat.uniforms.uFade.value = k < 1 ? 1 : Math.max(0, 1 - (now - this.t0 - this.dur) / this.linger);
    this.sheet.position.z = z;
    this.sheet.material.opacity = k < .08 ? k / .08 : k > .9 ? (1 - k) / .1 : 1;
  }
}

// ------------------------------------------------------------------ wind-blown sand
// Fine sand streaming low across the pit with the wind, thicker in gusts; streaks, not dots.
class SandWind {
  constructor(scene, terrain, W, H, n = 2600) {
    this.terrain = terrain; this.W = W; this.H = H; this.n = n;
    this.pos = new Float32Array(n * 3); this.alpha = new Float32Array(n); this.seed = new Float32Array(n);
    for (let i = 0; i < n; i++) this.spawn(i, true);
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(this.pos, 3).setUsage(THREE.DynamicDrawUsage));
    g.setAttribute("aAlpha", new THREE.BufferAttribute(this.alpha, 1).setUsage(THREE.DynamicDrawUsage));
    this.mat = new THREE.ShaderMaterial({
      uniforms: { uColor: { value: new THREE.Color(0xf4e3c0) }, uSize: { value: 26 } },
      vertexShader: `attribute float aAlpha; varying float vA; uniform float uSize;
        void main() { vec4 mv = modelViewMatrix * vec4(position, 1.0); gl_Position = projectionMatrix * mv;
          gl_PointSize = uSize * 12.0 / max(3.0, -mv.z); vA = aAlpha; }`,
      fragmentShader: `uniform vec3 uColor; varying float vA;
        void main() { vec2 p = gl_PointCoord - 0.5; float a = exp(-p.y * p.y * 90.0) * smoothstep(0.5, 0.1, abs(p.x)) * vA;
          if (a < 0.01) discard; gl_FragColor = vec4(uColor, a); }`,
      transparent: true, depthWrite: false,
    });
    this.points = new THREE.Points(g, this.mat); this.points.frustumCulled = false; this.points.renderOrder = 5;
    scene.add(this.points);
    this.gust = 0;
  }

  spawn(i, anywhere) {
    const x = anywhere ? Math.random() * (this.W + 16) - 8 : -8 - Math.random() * 3, z = Math.random() * (this.H + 12) - 6;
    this.pos[i * 3] = x; this.pos[i * 3 + 2] = z;
    this.pos[i * 3 + 1] = this.terrain.height(x, z) + .03 + Math.pow(Math.random(), 2.2) * .9;
    this.seed[i] = Math.random();
  }

  update(dt, now, on) {
    this.points.visible = on;
    if (!on) return;
    // a gust builds every few seconds and dies away
    const t = now / 1000;
    this.gust = .45 + .55 * Math.pow((Math.sin(t * .9) * .5 + .5) * (Math.sin(t * .23 + 1) * .5 + .5), 1.5);
    const speed = 1.1 + 1.6 * this.gust;
    for (let i = 0; i < this.n; i++) {
      const s = this.seed[i];
      this.pos[i * 3] += speed * (.6 + s * .8) * dt;
      this.pos[i * 3 + 2] += (.25 + Math.sin(t * 1.3 + s * 9) * .2) * dt;
      const gy = this.terrain.height(this.pos[i * 3], this.pos[i * 3 + 2]);
      if (this.pos[i * 3 + 1] < gy + .02) this.pos[i * 3 + 1] = gy + .02 + s * .05;   // skims up and over the benches
      this.pos[i * 3 + 1] += Math.sin(t * 3 + s * 20) * .03 * dt;
      if (this.pos[i * 3] > this.W + 8 || this.pos[i * 3 + 2] > this.H + 7) this.spawn(i, false);
      this.alpha[i] = (.1 + .28 * this.gust) * (.4 + s * .6);
    }
    const g = this.points.geometry;
    g.attributes.position.needsUpdate = g.attributes.aAlpha.needsUpdate = true;
  }
}

// a heap of broken rock or sand: a noisy cone that sits on the ground
function pileGeometry(r, h, seed) {
  const g = new THREE.ConeGeometry(r, h, 44, 8, false);
  const p = g.attributes.position;
  for (let k = 0; k < p.count; k++) {
    const px = p.getX(k), pz = p.getZ(k), py = p.getY(k);
    const top = (py + h / 2) / h;
    const n = (fbm(px * 4 + seed, pz * 4 - seed) - .5) * .22 + (fbm(px * 11 + seed * 3, pz * 11) - .5) * .06;
    const slump = 1 + .25 * Math.pow(1 - top, 3);                  // the toe spreads out
    p.setXYZ(k, px * (1 + n) * slump, py + (top > .98 ? 0 : n * h * .25) - h * .06 * Math.pow(top, 4), pz * (1 + n) * slump);
  }
  g.computeVertexNormals();
  return g;
}

// ------------------------------------------------------------------ the pit
export class Site3D {
  constructor(host, map, { accents, onPick, onHover, onAsk, onFace }) {
    this.host = host; this.map = map; this.accents = accents; this.onPick = onPick; this.onHover = onHover; this.onAsk = onAsk; this.onFace = onFace;
    this.robots = new Map(); this.overlays = new Map(); this.effects = []; this.excavators = new Map();
    this.layers = { routes: true, rings: true, lidar: true, claims: false, labels: true, dust: true };
    this.follow = null; this.fly = null; this.last = 0;
    this.W = map.width; this.H = map.height;
    this.lidarRange = ((map.physics && map.physics.lidar_m) || 60) / 20;

    const r = this.renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
    r.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
    r.shadowMap.enabled = true; r.shadowMap.type = THREE.PCFSoftShadowMap;
    r.outputColorSpace = THREE.SRGBColorSpace;
    r.toneMapping = THREE.ACESFilmicToneMapping; r.toneMappingExposure = 1.0;
    host.append(r.domElement);
    r.domElement.className = "gl";
    this.css = new CSS2DRenderer();
    this.css.domElement.className = "css2d";
    host.append(this.css.domElement);

    const scene = this.scene = new THREE.Scene();
    scene.background = new THREE.Color(HAZE);
    scene.fog = new THREE.Fog(HAZE, 90, 280);
    this.camera = new THREE.PerspectiveCamera(36, 1, 0.05, 400);
    this.home = { pos: new THREE.Vector3(this.W / 2 + 9, 21, this.H + 13), target: new THREE.Vector3(this.W / 2 + 1, -2.2, this.H / 2 + 1.2) };
    this.camera.position.copy(this.home.pos);
    const c = this.controls = new OrbitControls(this.camera, r.domElement);
    c.target.copy(this.home.target); c.enableDamping = true; c.dampingFactor = 0.08;
    c.maxPolarAngle = 1.32; c.minDistance = 1.5; c.maxDistance = 120; c.screenSpacePanning = true;
    c.addEventListener("start", () => { this.fly = null; this.chase = false; });

    this.terrain = new Terrain(map);
    this.sky(); this.lights();
    const ground = this.terrain.mesh();
    scene.add(ground);
    this.buildRoads(); this.buildBerms(); this.buildDumps(); this.buildProps(); this.buildPark(); this.buildPiles();
    this.survey = new Survey(scene, ground, this.W, this.H);
    this.surveyTicks = (map.physics && map.physics.survey_ticks) || 30;
    this.sand = new SandWind(scene, this.terrain, this.W, this.H);
    this.dust = new Dust(scene);
    this.lidar = new Lidar(scene, this.terrain, this.lidarRange);
    for (const [slot, s] of Object.entries(map.slots)) this.addExcavator(slot, s);
    for (const spec of [...map.robots, ...(map.spares || [])]) this.addRobot(spec.id);
    this.techs = new Map(); this.pings = new Map(); this.clockOffset = 0;
    this.rocks = new Map(); this.closed = new Map(); this.holes = new Map(); this.assign = [];
    this.ambient();

    this.ray = new THREE.Raycaster(); this.pointer = new THREE.Vector2();
    let down = null;
    r.domElement.addEventListener("pointerdown", (e) => { down = { x: e.clientX, y: e.clientY, t: performance.now() }; });
    r.domElement.addEventListener("pointerup", (e) => {
      if (!down) return;
      const moved = Math.hypot(e.clientX - down.x, e.clientY - down.y), quick = performance.now() - down.t < 500;
      down = null;
      if (moved < 7 && quick) {
        const hit = this.pick(e.clientX, e.clientY);
        if (hit && hit.face) { this.onFace && this.onFace(hit.face); return; }
        this.onPick && this.onPick(hit && hit.robot);
      }
    });
    r.domElement.addEventListener("pointermove", (e) => {
      const hit = this.pick(e.clientX, e.clientY);
      r.domElement.style.cursor = hit ? "pointer" : "grab";
      const id = hit && hit.robot;
      if (id !== this.hovered) { this.hovered = id; this.onHover && this.onHover(id); }
    });
    this.resizeObs = new ResizeObserver(() => this.resize());
    this.resizeObs.observe(host);
    this.resize();
  }

  drop(obj) {
    this.scene.remove(obj);
    obj.traverse((o) => { if (o.isCSS2DObject && o.element) o.element.remove(); });
  }

  ground(x, z) { return this.terrain.height(x, z); }

  // ------------------------------------------------------------ sky, light, static world
  sky() {
    const g = new THREE.SphereGeometry(300, 32, 16);
    const m = new THREE.MeshBasicMaterial({ map: afternoonSky(), side: THREE.BackSide, fog: false, depthWrite: false });
    const s = new THREE.Mesh(g, m); s.position.set(this.W / 2, 0, this.H / 2); this.scene.add(s);
    // mid-afternoon sun in the west, high enough for crisp but not long shadows
    const sun = new THREE.Sprite(new THREE.SpriteMaterial({ map: radialGlow("255,248,228"), transparent: true, depthWrite: false, fog: false, opacity: 1 }));
    sun.scale.setScalar(46); sun.position.set(this.W / 2 - 190, 150, this.H / 2 + 70); this.scene.add(sun);
    const halo = new THREE.Sprite(new THREE.SpriteMaterial({ map: radialGlow("255,236,200"), transparent: true, depthWrite: false, fog: false, opacity: .35 }));
    halo.scale.setScalar(170); halo.position.copy(sun.position); this.scene.add(halo);
    // thin high cloud
    const cloudTex = radialGlow("255,255,255");
    for (let i = 0; i < 9; i++) {
      const c = new THREE.Sprite(new THREE.SpriteMaterial({ map: cloudTex, transparent: true, depthWrite: false, fog: false, opacity: .16 + (i % 3) * .05 }));
      const a = i / 9 * Math.PI * 2 + .4;
      c.scale.set(90 + (i % 4) * 30, 14 + (i % 3) * 5, 1); c.position.set(this.W / 2 + Math.cos(a) * 200, 70 + (i % 4) * 14, this.H / 2 + Math.sin(a) * 200);
      this.scene.add(c);
    }
  }

  lights() {
    const s = this.scene;
    s.add(new THREE.HemisphereLight(0xdce8f4, 0xc2a177, 1.05));
    const key = this.key = new THREE.DirectionalLight(0xfff0da, 3.1);
    key.position.set(this.W / 2 - 24, 30, this.H / 2 + 11);
    key.target.position.set(this.W / 2, -2, this.H / 2);
    key.castShadow = true;
    key.shadow.mapSize.set(4096, 4096);
    Object.assign(key.shadow.camera, { left: -30, right: 30, top: 24, bottom: -24, near: 1, far: 90 });
    key.shadow.bias = -0.0005; key.shadow.normalBias = 0.03;
    s.add(key, key.target);
    const fill = new THREE.DirectionalLight(0xc9dcef, 0.45);
    fill.position.set(this.W + 16, 14, -10); s.add(fill);
    s.add(new THREE.AmbientLight(0xfff4e2, 0.18));
  }

  buildRoads() {
    // one flat textured quad per road segment, lying on the terrain; ruts run along the road
    const pos = [], uv = [], idx = [];
    const layout = this.map.layout, isRoad = (x, y) => { const ch = (layout[y] || "")[x]; return ch && ch !== "#" && !/[A-F]/.test(ch); };
    let n = 0;
    for (let y = 0; y < this.H; y++) for (let x = 0; x < this.W; x++) {
      if (!isRoad(x, y)) continue;
      const alongX = (isRoad(x - 1, y) || isRoad(x + 1, y)) && !(isRoad(x, y - 1) && isRoad(x, y + 1) && !(isRoad(x - 1, y) && isRoad(x + 1, y)));
      const S = 3;
      for (let j = 0; j <= S; j++) for (let i = 0; i <= S; i++) {
        const px = x + i / S, pz = y + j / S;
        pos.push(px, this.ground(px, pz) + 0.006, pz);
        uv.push(alongX ? px : pz, alongX ? j / S : i / S);
      }
      for (let j = 0; j < S; j++) for (let i = 0; i < S; i++) {
        const a = n + j * (S + 1) + i, b = a + 1, c2 = a + S + 1, d = c2 + 1;
        idx.push(a, c2, b, b, c2, d);
      }
      n += (S + 1) * (S + 1);
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    g.setAttribute("uv", new THREE.Float32BufferAttribute(uv, 2));
    g.setIndex(idx); g.computeVertexNormals();
    const t = lightRoad().clone(); t.needsUpdate = true;
    const m = new THREE.Mesh(g, new THREE.MeshStandardMaterial({ map: t, roughness: .95, color: 0xffffff,
      polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -2 }));
    m.receiveShadow = true;
    this.scene.add(m);
  }

  buildBerms() {
    // safety windrows along road edges where the ground drops away or sits level: a low mounded ridge of dirt
    const layout = this.map.layout, isFlat = (x, y) => { const ch = (layout[y] || "")[x]; return ch !== undefined && ch !== "#"; };
    const segs = [];
    for (let y = 0; y < this.H; y++) for (let x = 0; x < this.W; x++) {
      if (!isFlat(x, y) || /[A-F]/.test(layout[y][x])) continue;
      const lv = this.terrain.level(x, y);
      for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
        if (isFlat(x + dx, y + dy)) continue;
        const nb = this.terrain.level(x + dx, y + dy);
        if (nb > lv + 2) continue;   // a highwall toe: no berm, the face is the barrier
        segs.push([x + .5 + dx * .44, y + .5 + dy * .44, dx !== 0]);
      }
    }
    const geo = new THREE.CylinderGeometry(.075, .09, 1, 10, 1, false, 0, Math.PI);
    geo.rotateZ(Math.PI / 2); geo.rotateX(-Math.PI / 2); geo.scale(1, .55, 1);
    const inst = new THREE.InstancedMesh(geo, new THREE.MeshStandardMaterial({ color: 0xcfae82, roughness: 1 }), segs.length);
    const mtx = new THREE.Matrix4(), q = new THREE.Quaternion(), up = new THREE.Vector3(0, 1, 0);
    segs.forEach(([x, z, vertical], i) => {
      q.setFromAxisAngle(up, vertical ? Math.PI / 2 : 0);
      mtx.compose(new THREE.Vector3(x, this.ground(x, z) + .005, z), q, new THREE.Vector3(1.02, 1, 1));
      inst.setMatrixAt(i, mtx);
    });
    inst.castShadow = true; inst.receiveShadow = true;
    this.scene.add(inst);
  }

  buildDumps() {
    this.docks = {};
    const kinds = { DK1: "crusher", DK2: "stockpile", DK3: "waste", DK4: "stockpile" };
    const pileColor = { DK2: MAT.lowgrade, DK4: MAT.sand, DK3: MAT.waste };
    for (const [id, [x, y]] of Object.entries(this.map.docks)) {
      const cx = x + .5, cz = y + .5, base = this.ground(cx, cz);
      let pile = null;
      if (kinds[id] === "crusher") {
        const cr = makeCrusher(); cr.group.scale.setScalar(M); cr.group.position.set(cx, base, cz - 1.25); cr.group.rotation.y = -Math.PI / 2;
        cr.group.traverse((o) => { o.castShadow = true; o.receiveShadow = true; });
        this.scene.add(cr.group);
      } else {
        const r = kinds[id] === "waste" ? 1.9 : 1.25, h = kinds[id] === "waste" ? .75 : 1.05;
        pile = new THREE.Mesh(pileGeometry(r, h, x), this.pileMat(pileColor[id]));
        pile.position.set(cx, base + h / 2 - .08, cz - 1.55);
        if (kinds[id] === "waste") pile.scale.set(1.2, .8, 1);
        pile.castShadow = pile.receiveShadow = true;
        this.scene.add(pile);
      }
      // tipping edge marker: a hazard-striped kerb across the back of the pocket
      const kerb = new THREE.Mesh(new THREE.BoxGeometry(1.5, .05, .06), new THREE.MeshStandardMaterial({ map: hazard({ w: 256, h: 32, stripe: 12 }), roughness: .6 }));
      kerb.position.set(cx, base + .03, cz - .5); this.scene.add(kerb);
      const el = document.createElement("div"); el.className = "tag3d dock";
      Object.assign(el.style, { display: "inline-flex", alignItems: "baseline", gap: "6px", padding: "3px 8px" });   // one short line, not a card
      el.innerHTML = `<b>${id}</b><span class="carrier"></span><span class="n"><i>0</i>t</span>`;
      const lab = new CSS2DObject(el); lab.position.set(cx, base + 1.35, cz - 1.15); this.scene.add(lab);   // up over the stockpile, clear of the trucks
      this.docks[id] = { pos: new THREE.Vector3(cx, base, cz), el, count: 0, shown: -1, pile, id };
    }
  }

  setDockInfo(dock, name, tonnes) {
    const d = this.docks[dock]; if (!d) return;
    if (name && d.name !== name) { d.name = name; d.el.querySelector(".carrier").textContent = name; }
    if (tonnes != null && tonnes > d.count) { d.count = tonnes; d.el.querySelector("i").textContent = Math.round(tonnes).toLocaleString(); d.el.classList.remove("pop"); void d.el.offsetWidth; d.el.classList.add("pop"); }
  }

  buildProps() {
    // light towers on the bench tops, a drill rig working a blast pattern, a dozer on the waste dump
    this.animated = [];
    const towers = [[6.4, 8.55], [27.6, 8.55], [5.2, 12.55], [28.8, 12.55], [15.5, 16.4], [2.2, -0.6], [31.6, -0.6]];
    for (const [x, z] of towers) {
      const t = makeLightTower(); t.group.scale.setScalar(MACHINE); t.group.position.set(x, this.ground(x, z), z);
      t.group.rotation.y = Math.atan2(-(this.H / 2 - z), this.W / 2 - x) + Math.PI / 2;
      t.group.traverse((o) => { o.castShadow = true; });
      this.scene.add(t.group);
      const pl = new THREE.PointLight(0xffd9a0, 2.2, 7, 1.6); pl.position.set(x, this.ground(x, z) + .7, z); this.scene.add(pl);
    }
    const drill = makeDrillRig(); drill.group.scale.setScalar(MACHINE);
    drill.group.position.set(19.4, this.ground(19.4, 8.5), 8.5); drill.group.rotation.y = .4;
    this.scene.add(drill.group); this.animated.push(drill);
    const holes = new THREE.InstancedMesh(new THREE.CylinderGeometry(.018, .018, .01, 8), new THREE.MeshBasicMaterial({ color: 0x1a120b }), 24);
    const m = new THREE.Matrix4();
    for (let i = 0; i < 24; i++) { const hx = 17.6 + (i % 8) * .22, hz = 8.25 + Math.floor(i / 8) * .22; m.makeTranslation(hx, this.ground(hx, hz) + .004, hz); holes.setMatrixAt(i, m); }
    this.scene.add(holes);
    const dozer = makeDozer(); dozer.group.scale.setScalar(MACHINE);
    const dk = this.map.docks.DK3;
    dozer.group.position.set(dk[0] + .4, this.ground(dk[0] + .4, dk[1] - 2.3) + .02, dk[1] - 2.3); dozer.group.rotation.y = .3;
    this.scene.add(dozer.group); this.animated.push(dozer);
  }

  pileMat(color) {
    this.pileMats = this.pileMats || new Map();
    if (!this.pileMats.has(color)) {
      const t = grain().clone(); t.needsUpdate = true; t.repeat.set(3, 3);
      this.pileMats.set(color, new THREE.MeshStandardMaterial({ color, map: t, roughness: 1, flatShading: false }));
    }
    return this.pileMats.get(color);
  }

  pile(x, z, r, h, color, seed = 0) {
    const m = new THREE.Mesh(pileGeometry(r, h, seed || x * 3.1 + z), this.pileMat(color));
    m.position.set(x, this.ground(x, z) + h / 2 - .04, z);
    m.rotation.y = seed * 1.7;
    m.castShadow = m.receiveShadow = true;
    this.scene.add(m);
    return m;
  }

  buildPiles() {
    // a stockpile yard on the crest behind the dumps, sand heaps along the west plateau, spoil by the ramps
    const yard = [[2.6, -4.2, .95, .7, MAT.sand], [5.2, -5.1, 1.2, .85, MAT.sand], [8.4, -4.4, .9, .6, MAT.lowgrade],
      [14.6, -4.8, 1.3, .9, MAT.ore], [17.9, -5.3, 1.0, .7, MAT.ore], [24.2, -4.6, 1.1, .75, MAT.sand], [27.7, -5.4, 1.35, .95, MAT.sand],
      [31.2, -4.3, .85, .55, MAT.lowgrade], [36.5, 3.5, 1.2, .8, MAT.sand], [37.2, 8.8, .9, .6, MAT.sand], [36.8, 15.5, 1.4, .9, MAT.waste],
      [-3.8, 4.2, 1.1, .75, MAT.sand], [-4.6, 9.6, 1.3, .8, MAT.sand], [-3.6, 15.8, .95, .6, MAT.lowgrade], [-4.2, 20.4, 1.2, .7, MAT.waste],
      [6.5, 25.2, 1.2, .8, MAT.sand], [13.5, 25.6, 1.0, .6, MAT.sand], [21.5, 25.0, 1.3, .85, MAT.waste], [29.5, 25.5, .9, .55, MAT.sand]];
    yard.forEach(([x, z, r, h, c], i) => this.pile(x, z, r, h, c, i + 1));
    // the wider lease around the pit: rows of stockpiles and spoil heaps on the plateau in every direction
    const cols = [MAT.sand, MAT.sand, MAT.lowgrade, MAT.ore, MAT.waste, MAT.sand];
    let k = 0;
    for (let x = -12; x <= this.W + 12; x += 5.5) for (const z of [-11.5, this.H + 9.5]) {
      k++; const jx = (hash(x, z) - .5) * 1.6, jz = (hash(z, x) - .5) * 1.4, r = .8 + hash(k, 3) * .9;
      this.pile(x + jx, z + jz, r, r * (.55 + hash(k, 7) * .25), cols[k % cols.length], k + 40);
    }
    for (let z = -4; z <= this.H + 4; z += 5.5) for (const x of [-11, this.W + 10]) {
      k++; const jx = (hash(x, z) - .5) * 1.4, jz = (hash(z, x) - .5) * 1.6, r = .8 + hash(k, 5) * .9;
      this.pile(x + jx, z + jz, r, r * (.55 + hash(k, 9) * .25), cols[k % cols.length], k + 40);
    }
    // small sand heaps pushed up by the graders on bench corners
    for (const [x, z] of [[1.3, 1.6], [32.7, 1.6], [1.3, 19.4], [32.7, 19.4], [16.2, 8.5], [24.8, 12.5], [9.2, 16.8]]) {
      if (this.terrain.flat(Math.floor(x), Math.floor(z))) continue;
      this.pile(x, z, .32, .18, MAT.sand, x + z);
    }
  }

  buildPark() {
    for (const [x, y] of this.map.homes || []) {
      const p = new THREE.Mesh(new THREE.PlaneGeometry(.86, .86), new THREE.MeshBasicMaterial({ color: 0xf5b800, transparent: true, opacity: .12, depthWrite: false }));
      p.rotation.x = -Math.PI / 2; p.position.set(x + .5, this.ground(x + .5, y + .5) + .01, y + .5); this.scene.add(p);
    }
    for (const [x, y] of this.map.garage || []) {
      const t = hazard({ w: 256, h: 256, stripe: 22 }).clone(); t.needsUpdate = true; t.repeat.set(1.6, 1.6);
      const p = new THREE.Mesh(new THREE.PlaneGeometry(.9, .9), new THREE.MeshStandardMaterial({ map: t, transparent: true, opacity: .4 }));
      p.rotation.x = -Math.PI / 2; p.position.set(x + .5, this.ground(x + .5, y + .5) + .01, y + .5); this.scene.add(p);
    }
    const g = this.map.garage || [];
    if (g.length) {   // the workshop shed behind the bays
      const shed = new THREE.Group();
      const wall = new THREE.MeshStandardMaterial({ color: 0x3b3128, roughness: .8, metalness: .3 });
      const roof = new THREE.MeshStandardMaterial({ color: 0x8b8578, roughness: .5, metalness: .6 });
      const body = new THREE.Mesh(new THREE.BoxGeometry(3.2, .75, .9), wall); body.position.y = .37;
      const top = new THREE.Mesh(new THREE.BoxGeometry(3.3, .06, 1.0), roof); top.position.y = .77;
      shed.add(body, top); shed.traverse((o) => { o.castShadow = true; o.receiveShadow = true; });
      shed.position.set(g[1][0] + .5, this.ground(g[1][0] + .5, g[1][1] + 1.2), g[1][1] + 1.25);
      this.scene.add(shed);
      const el = document.createElement("div"); el.className = "tag3d crib"; el.innerHTML = `<b>WORKSHOP</b><span>fitters · bays G1–G${g.length}</span>`;
      const lab = new CSS2DObject(el); lab.position.set(shed.position.x, shed.position.y + 1.1, shed.position.z); this.scene.add(lab);
    }
  }

  ambient() {
    // dust hanging in the pit air: a haze of slow motes drifting with the wind and settling
    this.motes = [];
    for (let i = 0; i < 260; i++) this.motes.push({ x: Math.random() * this.W, z: Math.random() * this.H, y: Math.random() * 3 - 2.5, s: Math.random() });
  }

  // ------------------------------------------------------------ excavators
  addExcavator(slot, s) {
    const [x, y] = s.cell, [ax, ay] = s.access;
    const ex = new Excavator({ id: slot, label: slot });
    ex.group.scale.setScalar(MACHINE);
    const dirSpot = [ax - x, ay - y];                        // the loading spot is on this side
    const cx = x + .5 + dirSpot[0] * .28, cz = y + .5 + dirSpot[1] * .28;
    ex.group.position.set(cx, this.ground(cx, cz), cz);
    ex.group.rotation.y = dirSpot[1] > 0 ? Math.PI / 2 : -Math.PI / 2;   // +X faces the dig face, away from the spot
    ex.group.traverse((o) => { if (o.isMesh) o.castShadow = true; });
    if (ex.hit) ex.hit.userData.excavator = slot;
    this.scene.add(ex.group);
    const el = document.createElement("div"); el.className = "tag3d exc";
    el.innerHTML = `<span class="dot"></span><b>${slot}</b><span class="st">digging</span>`;
    el.addEventListener("pointerup", (e) => { e.stopPropagation(); this.onFace && this.onFace(slot); });
    const tag = new CSS2DObject(el); tag.position.set(0, 1.35 / MACHINE * M * 20, 0); ex.group.add(tag);
    const ring = new THREE.Mesh(new THREE.RingGeometry(.62, .68, 64), new THREE.MeshBasicMaterial({ color: 0xf5b800, transparent: true, opacity: 0, depthWrite: false }));
    ring.rotation.x = -Math.PI / 2; ring.position.set(cx, this.ground(cx, cz) + .03, cz); this.scene.add(ring);
    const spot = new THREE.Vector3(ax + .5, this.ground(ax + .5, ay + .5), ay + .5);
    // the shot muck pile at the toe of the face that the shovel digs into
    const mx = x + .5 - dirSpot[0] * .36, mz = y + .5 - dirSpot[1] * .36;
    const muck = this.pile(mx, mz, .46, .3, MAT[s.cls] || MAT.ore, x * 7 + y);
    muck.scale.set(1.5, 1, .8); muck.position.y = this.ground(cx, cz) + .1;
    const lvl = .5 + .5 * ((this.excavators.size * .618 + .1) % 1);   // faces start at different points in the drill-and-blast cycle
    this.excavators.set(slot, { ex, el, ring, spot, mode: "dig", pile: 0, truck: null, face: s, labelEl: el,
      muck, muckY: this.ground(cx, cz) - .05, level: lvl, shown: lvl, drill: this.drillSite(x, y, dirSpot) });
  }

  // drill and blast behind each face (visual dressing): a rig on the bench top drills a 4 x 3 pattern hole by hole,
  // backs off, the shot lifts a dust cloud and the muck pile the shovel has dug down is full again
  drillSite(x, y, dirSpot) {
    const fx = -dirSpot[0], fz = -dirSpot[1];                             // toward the face
    const ox = x + .5, oz = y + .5, top = HM(this.terrain.level(x + fx, y + fz));
    let lo = null, hi = null;                                              // the level ground behind the face, short of any drop or road
    for (let d = .8; d <= 1.44; d += .02) {
      const ok = Math.abs(this.ground(ox + fx * d, oz + fz * d) - top) < HM(.6);
      if (ok) { lo = lo ?? d; hi = d; } else if (lo !== null) break;
    }
    // the rig drives along the rows; its body sits off the row away from the crest under a highwall, toward the face on an
    // open bench, where the pattern also keeps back from the drop (the muck pile is low by the time the drill comes)
    const s = top - this.ground(ox, oz) > HM(4) ? 1 : -1, lx = -fz * s, lz = fx * s;
    const mid = lo === null ? 1.05 : Math.min(1.2, Math.max(.9, s > 0 ? (lo + hi) / 2 : Math.min((lo + hi) / 2, hi - .2)));
    const rig = makeDrillRig(); rig.group.scale.setScalar(MACHINE);
    const yaw = Math.atan2(-lz, lx), off = rig.bit.clone().applyAxisAngle(UP, yaw).multiplyScalar(MACHINE);
    rig.group.rotation.y = yaw;
    this.scene.add(rig.group);
    const C = this.collars || (this.collars = {
      hole: new THREE.CircleGeometry(.024, 12).rotateX(-Math.PI / 2), cut: new THREE.RingGeometry(.02, .056, 20).rotateX(-Math.PI / 2),
      dark: new THREE.MeshBasicMaterial({ color: 0x1a120b, polygonOffset: true, polygonOffsetFactor: -4, polygonOffsetUnits: -4 }),
      pale: new THREE.MeshStandardMaterial({ color: 0xe4d4b0, roughness: 1, polygonOffset: true, polygonOffsetFactor: -3, polygonOffsetUnits: -3 }),
    });
    const holeM = new THREE.InstancedMesh(C.hole, C.dark, 12), cutM = new THREE.InstancedMesh(C.cut, C.pale, 12);
    for (const m of [holeM, cutM]) { m.count = 0; m.frustumCulled = false; m.receiveShadow = true; this.scene.add(m); }
    const holes = [];
    for (const d of [mid - .1, mid, mid + .1]) for (const l of [.3, .1, -.1, -.3]) {   // rig faces along the row: drilled holes end up ahead of the mast
      const hx = ox + fx * d + lx * l, hz = oz + fz * d + lz * l, e = .05;
      const n = new THREE.Vector3(this.ground(hx - e, hz) - this.ground(hx + e, hz), 2 * e, this.ground(hx, hz - e) - this.ground(hx, hz + e)).normalize();
      holes.push({ x: hx, z: hz, y: this.ground(hx, hz), q: new THREE.Quaternion().setFromUnitVectors(UP, n), rig: new THREE.Vector2(hx - off.x, hz - off.z) });
    }
    const park = new THREE.Vector2(ox + fx * mid - lx * .78 - off.x, oz + fz * mid - lz * .78 - off.z);
    const cz = oz + fz * mid;
    return { rig, holeM, cutM, holes, park, at: park.clone(), from: park.clone(), to: park, phase: "idle", i: 0, t: 0, dur: 1, puff: 0, regrow: 0,
      center: new THREE.Vector3(ox + fx * mid, this.ground(ox + fx * mid, cz), cz) };
  }

  collar(D, i, k) {   // hole i opening up: the hole at once, the ring of cuttings growing as the string goes down
    const h = D.holes[i], m = this._m4 || (this._m4 = new THREE.Matrix4()), p = new THREE.Vector3(h.x, h.y + .006, h.z);
    D.holeM.setMatrixAt(i, m.compose(p, h.q, new THREE.Vector3(Math.min(1, k * 5), 1, Math.min(1, k * 5))));
    D.cutM.setMatrixAt(i, m.compose(p.setY(h.y + .004), h.q, new THREE.Vector3(.3 + .7 * k, 1, .3 + .7 * k)));
    D.holeM.count = D.cutM.count = Math.max(D.holeM.count, i + 1);
    D.holeM.instanceMatrix.needsUpdate = D.cutM.instanceMatrix.needsUpdate = true;
  }

  animateDrill(e, dt, now) {
    const D = e.drill, t = now / 1000, BORE = 2.4;
    D.t += dt;
    const go = (to) => { D.from.copy(D.at); D.to = to; D.dur = Math.max(.6, D.from.distanceTo(to) / .2); D.t = 0; D.phase = "tram"; };
    if (D.phase === "idle" && e.level <= .55) { D.i = 0; go(D.holes[0].rig); }
    let k = null;
    if (D.phase === "tram") {
      D.at.lerpVectors(D.from, D.to, easeInOut(Math.min(1, D.t / D.dur)));
      if (D.t >= D.dur) { D.t = 0; D.phase = D.i < D.holes.length ? "bore" : "clear"; }
    } else if (D.phase === "bore") {
      const b = Math.min(1, D.t / BORE), down = Math.min(1, b / .8);
      k = b < .8 ? down : (1 - b) / .2;                                   // feed down through the bench, then pull the string
      this.collar(D, D.i, down);
      const h = D.holes[D.i];
      if (b < .8 && this.layers.dust && now - D.puff > 140) {
        D.puff = now;
        this.dust.emit(h.x, h.y + .01, h.z, { n: 2, spread: .04, size: .16, grow: .3, life: 2, alpha: .4, vel: [0, .07, 0], jitter: .08, color: 0xdcc8a2 });
      }
      if (b >= 1) { D.i++; go(D.i < D.holes.length ? D.holes[D.i].rig : D.park); }
    } else if (D.phase === "clear" && D.t > 2.2) {                        // pattern charged, rig backed off: fire
      D.phase = "fired"; D.t = 0; D.regrow = now + 350;
      this.fire(e);
    } else if (D.phase === "fired" && D.t > 2.5) D.phase = "idle";
    if (D.regrow && now >= D.regrow) { D.regrow = 0; e.level = 1; }
    D.rig.group.position.set(D.at.x, this.ground(D.at.x, D.at.y), D.at.y);
    D.rig.bore(dt, k, t);
  }

  fire(e) {
    const D = e.drill;
    D.holeM.count = D.cutM.count = 0;
    if (!this.layers.dust) return;
    for (const h of D.holes) {
      this.dust.emit(h.x, h.y + .02, h.z, { n: 3, spread: .05, size: .14, grow: .25, life: .5, alpha: .95, vel: [0, 1.1, 0], jitter: .5, color: 0xfff0c8 });
      this.dust.emit(h.x, h.y + .05, h.z, { n: 12, spread: .2, size: .9, grow: 1.4, life: 7, alpha: .55, vel: [0, .6, 0], jitter: .45, color: 0xc49a66 });
    }
    const c = D.center, m = e.muck.position;
    this.dust.emit(c.x, c.y + .15, c.z, { n: 36, spread: .8, size: 1.5, grow: 1.9, life: 9, alpha: .42, vel: [0, .8, 0], jitter: .5, color: 0xd6b88a });
    this.dust.emit(m.x, m.y, m.z, { n: 26, spread: .7, size: 1.1, grow: 1.4, life: 6, alpha: .5, vel: [0, .22, 0], jitter: .55, color: 0xbf9464 });
  }

  setExcavatorLabel(slot, name, text, kind) {
    const e = this.excavators.get(slot); if (!e) return;
    e.el.querySelector("b").textContent = name;
    e.el.querySelector(".st").textContent = text;
    e.el.dataset.kind = kind;
  }

  animateExcavators(dt, now, A) {
    const loadingAt = new Map();
    for (const r of (A && A.robots) || []) {
      if (r.st !== "loading" && r.st !== "grade_check") continue;
      for (const [slot, e] of this.excavators) {
        const [ax, ay] = e.face.access;
        if (Math.floor(r.x / 20000) === ax && Math.floor(r.y / 20000) === ay) loadingAt.set(slot, r.id);
      }
    }
    for (const [slot, e] of this.excavators) {
      const tid = loadingAt.get(slot) || null;
      e.truck = tid;
      let mode = "dig", ang = 0;
      if (tid && this.robots.has(tid)) {
        mode = "load";
        const tp = this.robots.get(tid).group.position.clone();
        const local = e.ex.group.worldToLocal(tp.clone());
        ang = Math.atan2(-local.z, local.x);
      }
      const ev = e.ex.update(dt, mode, ang) || {};
      // each pass takes a bite out of the muck pile; when it runs low the drill starts the next pattern
      if (ev.dug) e.level = Math.max(.3, e.level - .045);
      this.animateDrill(e, dt, now);
      const lv = e.shown += (e.level - e.shown) * Math.min(1, dt * (e.level > e.shown ? 1.6 : 3));
      e.muck.scale.set(1.5 * (.55 + .45 * lv), lv, .8 * (.6 + .4 * lv)); e.muck.position.y = e.muckY + .15 * lv;
      if (ev.dug || ev.dumped) {
        const b = e.ex.bucketWorld(new THREE.Vector3());
        if (this.layers.dust) this.dust.emit(b.x, b.y, b.z, { n: ev.dumped ? 26 : 14, spread: .18, size: .5, grow: .7, life: 3.6, alpha: .42,
          vel: [0, .12, 0], jitter: .2, color: 0xdcc096 });
      }
      if (ev.dumped && tid) {
        const t = this.robots.get(tid);
        t.fill = Math.min(1, (t.fill || 0) + .26);
      }
      const k = (Math.sin(now / 300) + 1) / 2;
      e.ring.material.opacity = e.alone ? .25 + .45 * k : 0;
    }
  }

  // ------------------------------------------------------------ trucks and their overlays
  addRobot(id) {
    const acc = this.accents[id] || "#f4f1e8";
    const t = new HaulTruck({ id, accent: acc });
    t.group.scale.setScalar(TRUCK);
    t.group.rotation.order = "YZX";
    this.scene.add(t.group);
    const wrap = { id, t, group: t.group, yaw: 0, targetYaw: 0, speed: 0, fill: 0, tip: 0, status: "idle", tipT: 0, hole: null, lidarDim: 1 };
    if (t.hit) t.hit.userData.robot = id;
    t.group.traverse((o) => { o.userData.robot = id; });
    const el = document.createElement("div");
    el.className = "tag3d robot";
    el.innerHTML = `<span class="dot"></span><b>${id}</b><span class="st">idle</span>`;
    el.style.setProperty("--acc", acc);
    el.addEventListener("pointerup", (e) => { e.stopPropagation(); this.onPick && this.onPick(id); });
    const tag = new CSS2DObject(el); tag.position.set(0, 9.5, 0); t.group.add(tag);
    wrap.tagEl = el;
    const ask = document.createElement("button");
    ask.className = "tag3d askpin"; ask.innerHTML = `<span class="orb">✦</span>Ask ${id}`;
    ask.addEventListener("pointerup", (e) => { e.stopPropagation(); this.onAsk && this.onAsk(id); });
    const anchor = document.createElement("div"); anchor.className = "anchor3d"; anchor.append(ask);
    const pin = new CSS2DObject(anchor); pin.position.set(0, 13.5, 0); pin.visible = false; t.group.add(pin);
    wrap.askPin = pin;
    // the video's overlay: a green planned path ahead of every truck, a white safety ring around it
    const route = new THREE.Mesh(new THREE.BufferGeometry(), new THREE.MeshBasicMaterial({
      map: chevrons("rgba(255,255,255,0.95)").clone(), color: 0x3dff7a, transparent: true, opacity: .85, depthWrite: false }));
    route.material.map.needsUpdate = true; route.renderOrder = 3;
    const glowRoute = new THREE.Mesh(new THREE.BufferGeometry(), new THREE.MeshBasicMaterial({ color: 0x3dff7a, transparent: true, opacity: .16, depthWrite: false }));
    glowRoute.renderOrder = 2;
    const ring = new THREE.Mesh(new THREE.RingGeometry(.58, .64, 72), new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: .55, depthWrite: false, side: THREE.DoubleSide }));
    ring.rotation.x = -Math.PI / 2; ring.scale.set(1.2, .82, 1); ring.renderOrder = 4;
    const ringGlow = new THREE.Mesh(new THREE.RingGeometry(.5, .72, 72), new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: .08, depthWrite: false }));
    ringGlow.rotation.x = -Math.PI / 2; ringGlow.scale.set(1.2, .82, 1);
    const ringHolder = new THREE.Group(); ringHolder.add(ring, ringGlow);
    const claims = new THREE.InstancedMesh(new THREE.BoxGeometry(.94, .004, .94), new THREE.MeshBasicMaterial({ color: new THREE.Color(acc), transparent: true, opacity: .16, depthWrite: false }), 8);
    claims.count = 0; claims.renderOrder = 1;
    this.scene.add(route, glowRoute, ringHolder, claims);
    this.robots.set(id, wrap);
    this.overlays.set(id, { route, glowRoute, ring, ringGlow, ringHolder, claims, routeKey: "", link: null });
  }

  setRoute(id, pts) {
    const o = this.overlays.get(id);
    const key = pts.map((p) => `${p.x.toFixed(2)},${p.z.toFixed(2)}`).join(";");
    if (key === o.routeKey) return;
    o.routeKey = key;
    const build = (w, lift) => {
      const pos = [], uv = [], idx = [];
      // densify so the ribbon follows the terrain on ramps
      const dense = [];
      for (let i = 0; i < pts.length; i++) {
        if (i === 0) { dense.push(pts[0]); continue; }
        const a = pts[i - 1], b = pts[i], n = Math.max(1, Math.ceil(a.distanceTo(b) / .25));
        for (let k = 1; k <= n; k++) dense.push(a.clone().lerp(b, k / n));
      }
      let len = 0;
      for (let i = 0; i < dense.length; i++) {
        const a = dense[Math.max(0, i - 1)], b = dense[Math.min(dense.length - 1, i + 1)];
        const dir = new THREE.Vector3().subVectors(b, a).setY(0).normalize(), nrm = new THREE.Vector3(-dir.z, 0, dir.x).multiplyScalar(w / 2);
        if (i > 0) len += dense[i].distanceTo(dense[i - 1]);
        const y = this.ground(dense[i].x, dense[i].z) + lift;
        pos.push(dense[i].x + nrm.x, y, dense[i].z + nrm.z, dense[i].x - nrm.x, y, dense[i].z - nrm.z);
        uv.push(len / .3, 0, len / .3, 1);
        if (i > 0) { const k = i * 2; idx.push(k - 2, k - 1, k, k - 1, k + 1, k); }
      }
      const g = new THREE.BufferGeometry();
      g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
      g.setAttribute("uv", new THREE.Float32BufferAttribute(uv, 2));
      g.setIndex(idx);
      return g;
    };
    o.route.geometry.dispose(); o.route.geometry = build(.14, .03);
    o.glowRoute.geometry.dispose(); o.glowRoute.geometry = build(.36, .025);
  }

  setClaims(id, cells) {
    const o = this.overlays.get(id), m = new THREE.Matrix4();
    const n = Math.min(8, cells.length);
    for (let i = 0; i < n; i++) { const x = cells[i][0] + .5, z = cells[i][1] + .5; m.makeTranslation(x, this.ground(x, z) + .015, z); o.claims.setMatrixAt(i, m); }
    o.claims.count = n; o.claims.instanceMatrix.needsUpdate = true;
  }

  setLink(id, other, kind) {
    const o = this.overlays.get(id);
    if (!other) { if (o.link) { this.scene.remove(o.link.line); o.link = null; } return; }
    if (!o.link || o.link.other !== other || o.link.kind !== kind) {
      if (o.link) this.scene.remove(o.link.line);
      const mat = new THREE.LineDashedMaterial({ color: kind === "standoff" ? 0xff3b4f : 0xff9a1f, dashSize: .12, gapSize: .08, transparent: true, opacity: .95 });
      const line = new THREE.Line(new THREE.BufferGeometry(), mat); line.renderOrder = 7;
      this.scene.add(line); o.link = { other, kind, line };
    }
    const a = this.robots.get(id).group.position, b = this.robots.get(other).group.position;
    const mid = new THREE.Vector3().addVectors(a, b).multiplyScalar(0.5); mid.y = Math.max(a.y, b.y) + .9 + a.distanceTo(b) * .15;
    const curve = new THREE.QuadraticBezierCurve3(a.clone().setY(a.y + .45), mid, b.clone().setY(b.y + .45));
    o.link.line.geometry.dispose(); o.link.line.geometry = new THREE.BufferGeometry().setFromPoints(curve.getPoints(24));
    o.link.line.computeLineDistances();
  }

  // ------------------------------------------------------------ per-frame update
  update(A, B, alpha, now, info = {}) {
    const dt = this.last ? Math.min(0.1, (now - this.last) / 1000) : 0.016;
    this.last = now;
    const hazards = [];
    if (A && A.t != null) {   // the sim's lidar survey beat: every truck re-surveys the road every 5 s
      const beat = Math.floor(A.t / this.surveyTicks);
      if (this.beat !== undefined && beat !== this.beat) this.survey.start(now);
      this.beat = beat;
    }
    if (A) {
      const holes = new Map((A.holes || []).map((h) => [h[4], h]));
      for (const h of A.holes || []) if (h[3]) hazards.push({ x: h[0] + .5, z: h[1] + .5, r: .32 });
      for (const c of A.rocks || []) hazards.push({ x: c[0] + .5, z: c[1] + .5, r: .38 });
      for (const ra of A.robots) {
        const w = this.robots.get(ra.id); if (!w) continue;
        const rb = B && B.robots.find((x) => x.id === ra.id);
        const x = (rb ? ra.x + (rb.x - ra.x) * alpha : ra.x) / 20000, z = (rb ? ra.y + (rb.y - ra.y) * alpha : ra.y) / 20000;
        this.moveTruck(w, ra, x, z, dt, now, holes);
        const o = this.overlays.get(ra.id);
        const pts = [new THREE.Vector3(x, 0, z), ...(ra.p || []).map((c) => new THREE.Vector3(c[0] + .5, 0, c[1] + .5))];
        const remote = ra.sv === "remote";
        o.route.visible = o.glowRoute.visible = (this.layers.routes || remote || this.follow === ra.id) && pts.length > 1
          && !["idle", "fault", "standby", "loading", "dumping", "grade_check"].includes(ra.st);
        if (o.route.visible) { this.setRoute(ra.id, pts); o.route.material.map.offset.x -= dt * (ra.v > 0 ? 2.2 : .3); }
        o.route.material.color.set(remote ? 0xffc93c : 0x3dff7a); o.glowRoute.material.color.set(remote ? 0xffc93c : 0x3dff7a);
        const ringCol = ra.f || ra.st === "estop" ? 0xff3b4f : ["waiting", "held", "blocked", "queued"].includes(ra.st) ? 0xffb020 : 0xffffff;
        o.ring.material.color.setHex(ringCol); o.ringGlow.material.color.setHex(ringCol);
        o.ringHolder.visible = this.layers.rings && ra.st !== "standby";
        o.ringHolder.position.set(x, this.ground(x, z) + .03, z); o.ringHolder.rotation.y = w.yaw;
        const pulse = ra.f ? .5 + .4 * Math.sin(now / 120) : .55;
        o.ring.material.opacity = pulse;
        if (ra.f && now - (this.pings.get(ra.id) || 0) > 1100) { this.pings.set(ra.id, now); this.ring(new THREE.Vector3(x, this.ground(x, z), z), 0xff3b4f, now, 1.4); }
        o.claims.visible = this.layers.claims;
        this.setClaims(ra.id, ra.r || []);
        const standoff = ra.w && A.robots.find((q) => q.id === ra.w && q.w === ra.id);
        this.setLink(ra.id, ra.st === "waiting" && ra.w ? ra.w : null, standoff ? "standoff" : "hold");
        const el = w.tagEl;
        el.dataset.st = remote ? "remote" : ra.f ? "fault" : ra.st;
        el.querySelector(".st").textContent = remote ? "AI control" : ra.f ? `broken down · ${ra.f.type === "tire" ? "tyre" : "lidar"}`
          : ra.st === "waiting" && ra.w ? `holding for ${ra.w}` : ra.st === "queued" && ra.w ? `queued behind ${ra.w}` : info.label ? info.label(ra) : ra.st;
        // parked trucks keep their labels to themselves (a row of them in the park would stack up); hover or select shows it
        el.style.display = (this.layers.labels && !["idle", "standby"].includes(ra.st)) || w.selected || this.hovered === ra.id ? "" : "none";
      }
      this.syncRocks(A.rocks || [], now);
      this.syncHoles(A.holes || [], now);
      this.syncClosed(A.zones || []);
    }
    const trucks = [...this.robots.values()].filter((w) => w.status !== "standby");
    this.lidar.update(trucks, now, hazards, this.follow, this.layers.lidar);
    this.animateExcavators(dt, now, A);
    this.animateFeatures(now);
    this.survey.update(now, this.layers.lidar);
    this.sand.update(dt, now, this.layers.dust);
    for (const a of this.animated) a.update && a.update(dt, now / 1000);
    this.animateTechs(dt, now);
    this.animateAmbient(dt, now);
    this.dust.points.visible = this.layers.dust;
    this.dust.renderHeight = this.host.clientHeight;
    this.dust.update(dt, this.camera);
    this.animateEffects(dt, now);
    this.animateCamera(dt);
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
    this.css.render(this.scene, this.camera);
  }

  moveTruck(w, ra, x, z, dt, now, holes) {
    const t = w.t;
    // yaw: follow the heading; a truck tipping at a dump has reversed up to the edge
    let yaw = YAW[ra.d] ?? w.targetYaw;
    const tipping = ra.st === "dumping";
    if (tipping) yaw += Math.PI;
    w.targetYaw = yaw;
    let d = w.targetYaw - w.yaw; d = Math.atan2(Math.sin(d), Math.cos(d));
    w.yaw += d * Math.min(1, dt * (Math.abs(d) > 2 ? 1.6 : 5));
    // height and pitch from the terrain under the axles
    const fx = Math.cos(w.yaw), fz = -Math.sin(w.yaw);
    const hf = this.ground(x + fx * .3, z + fz * .3), hb = this.ground(x - fx * .3, z - fz * .3);
    let y = (hf + hb) / 2, pitch = Math.atan2(hf - hb, .6);
    // potholes: ease down into it and climb out
    let dip = 0;
    if (ra.hl && holes.has(ra.hl)) {
      const h = holes.get(ra.hl), dd = Math.hypot(h[0] + .5 - x, h[1] + .5 - z), kind = h[5] || "p";
      const k = Math.max(0, 1 - dd / .25);
      if (kind === "b") {          // over the ridges: the truck rocks and bounces
        dip = -Math.abs(Math.sin(dd * 40)) * (h[2] / 1000) * 1.4;
        pitch += Math.sin(dd * 40) * .05;
      } else if (kind === "s") {   // ploughing through a drift: sits a little lower, throws sand
        dip = k * (h[2] / 1000) * .35;
        if (this.layers.dust && ra.v > 60 && Math.random() < dt * 40) {
          this.dust.emit(x - fx * .3, y + .05, z - fz * .3, { n: 2, spread: .3, size: .4, grow: .7, life: 2.6, alpha: .45,
            vel: [-fx * .3, .16, -fz * .3], jitter: .25, color: 0xf0dcb2 });
        }
      } else {
        dip = Math.sin(k * Math.PI / 2) * (h[2] / 1000);
        pitch += Math.sin((1 - dd / .25) * Math.PI) * .06 * Math.sign(((h[0] + .5 - x) * fx + (h[1] + .5 - z) * fz));
      }
    }
    if (w.jump) {                  // hit a bump too fast: airborne for a moment
      const jk = (now - w.jump.t0) / 700;
      if (jk >= 1) w.jump = null; else { dip -= Math.sin(jk * Math.PI) * w.jump.amp; pitch += Math.sin(jk * Math.PI * 2) * .08; }
    }
    t.group.position.set(x, y, z);
    t.group.rotation.set(0, w.yaw, pitch);
    t.setDip && t.setDip(dip * .9, 0);
    const moved = Math.hypot(x - (w.lx ?? x), z - (w.lz ?? z));
    w.lx = x; w.lz = z;
    t.roll && t.roll(moved / TRUCK * Math.sign(Math.cos(d)) || moved / TRUCK);
    w.speed = ra.v;
    const kmh = ra.v * 0.036;
    if (w.status !== ra.st) { w.status = ra.st; w.statusSince = now; }
    t.setStatusColor && t.setStatusColor({ moving: 0x3ddc84, waiting: 0xffb020, held: 0xffb020, queued: 0xffb020, blocked: 0xff3b4f,
      estop: 0xff3b4f, loading: 0xf5b800, grade_check: 0xf5b800, dumping: 0xf5b800, fault: 0xff3b4f, standby: 0x5d5546, idle: 0x9a8f7c }[ra.st] ?? 0x9a8f7c);
    t.setBrake && t.setBrake(["waiting", "held", "blocked", "estop", "queued"].includes(ra.st) || (kmh > 1 && ra.v < (w.pv ?? 0)));
    w.pv = ra.v;
    t.setLights && t.setLights(ra.st !== "standby");
    t.setBeacon && t.setBeacon(!!ra.f, now);
    t.spinLidar && t.spinLidar(ra.st === "standby" ? 0 : dt);
    w.lidarDim = ra.f && ra.f.type === "sensor" ? .25 : 1;
    // load: the excavator fills it pass by pass; it rides full; it tips at the dump
    const cls = ra.cs && ra.cs[0] ? (this.map.slots[String(ra.cs[0]).replace("MAT-", "")] || {}).cls : null;
    if (ra.st === "loading") {
      const slot = Object.entries(this.map.slots).find(([, s]) => s.access[0] === Math.floor(ra.x / 20000) && s.access[1] === Math.floor(ra.y / 20000));
      w.loadCls = slot ? slot[1].cls : w.loadCls;
    } else if (cls) { w.loadCls = cls; if (!tipping) w.fill = 1; }
    else if (!tipping && ra.c === 0 && ra.st !== "grade_check") w.fill = Math.max(0, w.fill - dt * .6);
    if (tipping) {
      const since = (now - (w.statusSince || now)) / 1000;
      w.tip = Math.min(1, since / 1.8);
      if (w.tip > .45) {
        w.fill = Math.max(0, w.fill - dt * .45);
        if (this.layers.dust && w.fill > .02) {
          const back = new THREE.Vector3(-fx * .7, 0, -fz * .7).add(t.group.position);
          this.dust.emit(back.x, back.y + .25, back.z, { n: 3, spread: .25, size: .55, grow: .9, life: 4, alpha: .4, vel: [-fx * .15, .05, -fz * .15], jitter: .15, color: MAT[w.loadCls] ? MAT[w.loadCls] + 0x151515 : 0xb99a74 });
        }
      }
    } else w.tip = Math.max(0, w.tip - dt / 1.6);
    t.setTip && t.setTip(easeInOut(w.tip));
    t.setLoad && t.setLoad(w.fill, MAT[w.loadCls] || MAT.ore);
    // dust from the rear wheels, stronger with speed and when loaded
    if (this.layers.dust && kmh > 4 && Math.random() < Math.min(1, kmh / 30) * dt * 30) {
      const back = new THREE.Vector3(-fx * .52, 0, -fz * .52).add(t.group.position);
      const side = new THREE.Vector3(-fz, 0, fx).multiplyScalar((Math.random() - .5) * .58);
      this.dust.emit(back.x + side.x, back.y + .03, back.z + side.z, { n: 1, spread: .08, size: .32 + kmh / 90, grow: .55 + kmh / 60, life: 3.2 + kmh / 20,
        alpha: .22 + kmh / 160, vel: [-fx * kmh / 90, .08, -fz * kmh / 90], jitter: .1, color: 0xdcc39a });
    }
  }

  syncRocks(cells, now) {
    const keys = new Set(cells.map((c) => c.join(",")));
    for (const [k, obj] of this.rocks) if (!keys.has(k)) { this.scene.remove(obj.group); this.rocks.delete(k); }
    for (const c of cells) {
      const k = c.join(",");
      if (this.rocks.has(k)) continue;
      const g = new THREE.Group();
      const mat = new THREE.MeshStandardMaterial({ color: 0x6e4a33, roughness: .95, flatShading: true });
      for (let i = 0; i < 4; i++) {
        const geo = new THREE.DodecahedronGeometry(.1 + i * .02, 0);
        const p = geo.attributes.position;
        for (let v = 0; v < p.count; v++) p.setXYZ(v, p.getX(v) * (1 + (hash(v, i) - .5) * .5), p.getY(v) * (.7 + hash(i, v) * .4), p.getZ(v) * (1 + (hash(v + 3, i) - .5) * .5));
        geo.computeVertexNormals();
        const m = new THREE.Mesh(geo, mat); m.position.set((i % 2 - .5) * .16, .06 + i * .02, (Math.floor(i / 2) - .5) * .14); m.rotation.set(i, i * 2, 0);
        m.castShadow = m.receiveShadow = true; g.add(m);
      }
      const x = c[0] + .5, z = c[1] + .5, gy = this.ground(x, z);
      g.position.set(x, gy + 2.4, z);
      this.scene.add(g);
      this.rocks.set(k, { group: g, t0: now });
      this.effects.push({ kind: "fall", obj: g, t0: now, x, z, gy });
    }
  }

  // road features the sim tracks: potholes (dips), bumps (ridges trucks bounce over) and sand drifts. A new one
  // appears when the next lidar survey sweeps over it; a cleared one sinks away.
  syncHoles(list, now) {
    const first = !this.holesSeen; this.holesSeen = true;
    const want = new Set(list.map((h) => h[4]));
    for (const [id, o] of this.holes) if (!want.has(id) && !o.leaving) { o.leaving = now; }
    for (const [x, y, depth, known, id, kind] of list) {
      let o = this.holes.get(id);
      if (!o) {
        o = this.makeFeature(x, y, depth, kind || "p", id);
        o.reveal = first ? 0 : null;           // null: wait for the survey sheet to pass over it
        o.born = now;
        this.holes.set(id, o);
      }
      o.known = !!known;
    }
  }

  makeFeature(x, y, depth, kind, id) {
    const g = new THREE.Group(), cx = x + .5, cz = y + .5, gy = this.ground(cx, cz);
    const alongX = this.terrain.flat(x - 1, y) && this.terrain.flat(x + 1, y);
    const parts = [];
    if (kind === "p") {
      const r = .13 + depth / 7000;
      const pit = new THREE.Mesh(new THREE.CircleGeometry(r, 28), new THREE.MeshBasicMaterial({ map: radialGlow("70,46,24"), transparent: true, opacity: .95, depthWrite: false }));
      pit.rotation.x = -Math.PI / 2; pit.scale.set(1.4, 1, 1); pit.position.y = .012; pit.renderOrder = 2;
      const rim = new THREE.Mesh(new THREE.TorusGeometry(r * 1.05, .024, 6, 28), this.pileMat(0xc9a57c));
      rim.rotation.x = -Math.PI / 2; rim.scale.set(1.4, 1, .6); rim.position.y = .01;
      g.add(pit, rim); parts.push(rim);
    } else if (kind === "b") {
      // washboard: three low ridges across the road
      const h = .03 + depth / 9000;
      const geo = new THREE.CylinderGeometry(.06, .06, .82, 12, 1, false, 0, Math.PI);
      geo.rotateZ(Math.PI / 2); geo.rotateX(-Math.PI / 2);
      for (const off of [-.2, 0, .2]) {
        const ridge = new THREE.Mesh(geo, this.pileMat(0xc8a276));
        ridge.scale.set(1, h / .06, 1); ridge.rotation.y = alongX ? Math.PI / 2 : 0;
        ridge.position.set(alongX ? off : 0, 0, alongX ? 0 : off);
        ridge.castShadow = ridge.receiveShadow = true; g.add(ridge); parts.push(ridge);
      }
    } else {
      // a sand drift: a low rippled tongue of pale sand lying across the road with the wind
      const geo = new THREE.SphereGeometry(1, 30, 12, 0, Math.PI * 2, 0, Math.PI / 2);
      const p = geo.attributes.position;
      for (let k = 0; k < p.count; k++) {
        const px = p.getX(k), pz = p.getZ(k), py = p.getY(k), n = (fbm(px * 2.5 + x, pz * 2.5 + y) - .5) * .5;
        p.setXYZ(k, px * (1 + n), py, pz * (1 + n * .6));
      }
      geo.computeVertexNormals();
      const t = sandRipples().clone(); t.needsUpdate = true; t.wrapS = t.wrapT = THREE.RepeatWrapping; t.repeat.set(2, 2);
      const drift = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ map: t, color: 0xfff3dc, roughness: 1 }));
      drift.scale.set(.5, .04 + depth / 8000, .34); drift.rotation.y = .5 + (x + y) % 3 * .3;
      drift.receiveShadow = true; drift.castShadow = true; g.add(drift); parts.push(drift);
    }
    const mark = new THREE.Mesh(new THREE.RingGeometry(.4, .44, 40), new THREE.MeshBasicMaterial({ color: kind === "s" ? 0xfff0c0 : 0xffb020, transparent: true, opacity: 0, depthWrite: false }));
    mark.rotation.x = -Math.PI / 2; mark.position.y = .025; g.add(mark);
    const twins = parts.map((m) => { const t = new THREE.Mesh(m.geometry, this.survey.mat); t.position.copy(m.position); t.rotation.copy(m.rotation); t.scale.copy(m.scale).multiplyScalar(1.02); g.add(t); return t; });
    g.position.set(cx, gy, cz);
    g.scale.setScalar(.001);
    this.scene.add(g);
    return { group: g, mark, twins, known: false, id, kind, x: cx, z: cz, depth };
  }

  animateFeatures(now) {
    for (const [id, o] of this.holes) {
      if (o.leaving) {
        const k = Math.min(1, (now - o.leaving) / 900);
        o.group.scale.setScalar(Math.max(.001, 1 - k)); o.group.position.y = this.ground(o.x, o.z) - k * .05;
        if (k >= 1) { this.drop(o.group); this.holes.delete(id); }
        continue;
      }
      if (o.reveal === null && ((this.survey.active(now) && this.survey.front(now) >= o.z) || now - o.born > 7000)) {
        o.reveal = now;
        const p = new THREE.Vector3(o.x, this.ground(o.x, o.z), o.z);
        this.ring(p, o.kind === "s" ? 0xfff0c0 : 0xffb020, now, .8);
        if (this.layers.dust) this.dust.emit(o.x, p.y + .03, o.z, { n: o.kind === "s" ? 26 : 12, spread: .4, size: .45, grow: .6, life: 3.2, alpha: .35,
          vel: [.12, .08, .04], jitter: .2, color: o.kind === "s" ? 0xf2dfb8 : 0xcaa77c });
      }
      if (o.reveal !== null) o.group.scale.setScalar(Math.max(.001, easeInOut(Math.min(1, (now - o.reveal) / 650))));
      const k = (Math.sin(now / 280) + 1) / 2;
      o.mark.material.opacity = o.known && o.reveal !== null ? .28 + .35 * k : 0;
      for (const t of o.twins) t.visible = this.survey.grid.visible;
    }
  }

  syncClosed(zones) {
    const want = new Set(zones);
    for (const [z, g] of this.closed) if (!want.has(z)) { this.drop(g); this.closed.delete(z); }
    for (const z of want) {
      if (this.closed.has(z)) continue;
      const cells = this.map.zones[z] || [];
      const g = new THREE.Group();
      const mat = new THREE.MeshBasicMaterial({ color: 0xff3b4f, transparent: true, opacity: .2, depthWrite: false });
      for (const [x, y] of cells) { const p = new THREE.Mesh(new THREE.PlaneGeometry(.98, .98), mat); p.rotation.x = -Math.PI / 2; p.position.set(x + .5, this.ground(x + .5, y + .5) + .02, y + .5); g.add(p); }
      if (cells.length) {
        const xs = cells.map((c) => c[0]), ys = cells.map((c) => c[1]);
        const y0 = ys[Math.floor(ys.length / 2)];
        for (const x of [Math.min(...xs), Math.max(...xs) + 1]) {   // barricades across both ends
          const gy = this.ground(x, y0 + .5);
          const bar = new THREE.Mesh(new THREE.BoxGeometry(.05, .05, .9), new THREE.MeshStandardMaterial({ map: hazard({ w: 128, h: 32, stripe: 10 }) }));
          bar.position.set(x, gy + .22, y0 + .5); g.add(bar);
          for (const dz of [.08, .92]) { const post = new THREE.Mesh(new THREE.CylinderGeometry(.02, .03, .26, 8), new THREE.MeshStandardMaterial({ color: 0xff3b4f, emissive: 0x551010 })); post.position.set(x, gy + .13, y0 + dz); g.add(post); }
        }
        const mx = (Math.min(...xs) + Math.max(...xs) + 1) / 2;
        const el = document.createElement("div"); el.className = "tag3d alert";
        el.innerHTML = `<b>BLAST ZONE · CLOSED</b><span>${z.replace(/_/g, " ")} · exclusion in force</span>`;
        const lab = new CSS2DObject(el); lab.position.set(mx, this.ground(mx, y0 + .5) + 1.2, y0 + .5); g.add(lab);
      }
      this.scene.add(g); this.closed.set(z, g);
    }
  }

  // ------------------------------------------------------------ fitters in service utes
  setTechs(list, serverOffset) {
    this.clockOffset = serverOffset;
    const want = new Set(list.map((t) => t.id));
    for (const [id, w] of this.techs) if (!want.has(id)) { this.drop(w.group); this.techs.delete(id); }
    for (const t of list) {
      let w = this.techs.get(t.id);
      if (!w) {
        const ute = makeServiceUte(); ute.group.scale.setScalar(MACHINE);
        const group = new THREE.Group(); group.add(ute.group);
        const el = document.createElement("div"); el.className = "tag3d tech";
        el.innerHTML = `<b></b><span class="what"></span><i class="bar"><u></u></i>`;
        const anchor = document.createElement("div"); anchor.className = "anchor3d"; anchor.append(el);
        const lab = new CSS2DObject(anchor); lab.position.set(0, .7, 0); group.add(lab);
        this.scene.add(group);
        w = { group, ute, el, spark: 0 };
        this.techs.set(t.id, w);
      }
      w.t = t;
      w.el.querySelector("b").textContent = `${t.name} · ${t.role}`;
    }
  }

  animateTechs(dt, now) {
    const clock = Date.now() / 1000 - this.clockOffset;
    for (const w of this.techs.values()) {
      const t = w.t, route = t.route || [];
      if (!route.length) continue;
      const k = Math.min(1, Math.max(0, (clock - t.depart_at) / Math.max(0.1, t.arrive_at - t.depart_at)));
      const f = k * (route.length - 1), i = Math.min(route.length - 2, Math.max(0, Math.floor(f))), frac = f - i;
      const a = route[Math.max(0, i)], b = route[Math.min(route.length - 1, i + 1)];
      const x = a[0] + 0.5 + (b[0] - a[0]) * frac + .28, z = a[1] + 0.5 + (b[1] - a[1]) * frac + .28;
      w.group.position.set(x, this.ground(x, z), z);
      const driving = k < 1;
      if (driving && (b[0] !== a[0] || b[1] !== a[1])) w.group.rotation.y = Math.atan2(-(b[1] - a[1]), b[0] - a[0]);
      w.ute.setBeacon && w.ute.setBeacon(true, now);
      const working = !driving && t.repair_until;
      const total = t.repair_until ? t.repair_until - t.arrive_at : 1;
      const prog = working ? Math.min(1, Math.max(0, (clock - t.arrive_at) / total)) : 0;
      w.el.querySelector(".what").textContent = driving ? `driving out · ETA ${Math.max(0, Math.ceil(t.arrive_at - clock))} s`
        : working ? `${t.task || "repairing"} · ${Math.round(prog * 100)}%` : "on site";
      w.el.querySelector("u").style.width = `${driving ? k * 100 : prog * 100}%`;
      const target = this.robots.get(t.robot);
      if (working && now - w.spark > 260 && target) {
        w.spark = now;
        const p = target.group.position;
        this.dust.emit(p.x + (Math.random() - .5) * .3, p.y + .15, p.z + (Math.random() - .5) * .3, { n: 2, size: .06, grow: .02, life: .5, alpha: .9, vel: [0, .5, 0], jitter: .6, color: 0xfff2a8 });
      }
    }
  }

  animateAmbient(dt, now) {
    if (!this.layers.dust) return;
    // hanging dust drifting across the pit, settling slowly
    for (const m of this.motes) {
      m.x += this.dust.wind.x * dt * 2; m.z += this.dust.wind.z * dt * 2; m.y -= dt * .02;
      if (m.x > this.W + 2 || m.y < this.ground(m.x, m.z)) { m.x = -1 - Math.random() * 2; m.z = Math.random() * this.H; m.y = Math.random() * 2; }
    }
    if (Math.random() < dt * 14) {
      const m = this.motes[Math.floor(Math.random() * this.motes.length)];
      this.dust.emit(m.x, Math.max(m.y, this.ground(m.x, m.z) + .2), m.z, { n: 1, spread: 1.2, size: 1.3 + Math.random() * 1.4, grow: .3, life: 9, alpha: .08, vel: [.15, -.012, .05], jitter: .03, color: 0xe6d0a6 });
    }
    // gusts lift fine dust off the benches and the dumps
    if (Math.random() < dt * 1.2) {
      const x = Math.random() * this.W, z = Math.random() * this.H;
      this.dust.emit(x, this.ground(x, z) + .02, z, { n: 6, spread: .6, size: .45, grow: .5, life: 5, alpha: .13, vel: [.25, .04, .1], jitter: .1, color: 0xe0c89e });
    }
  }

  // ------------------------------------------------------------ events -> effects
  event(e, frame, now) {
    const w = e.robot && this.robots.get(e.robot);
    const at = w ? w.group.position.clone() : null;
    const cellPos = (c) => new THREE.Vector3(c[0] + .5, this.ground(c[0] + .5, c[1] + .5), c[1] + .5);
    switch (e.type) {
      case "standoff": { if (at) { this.float(at.clone().setY(at.y + 1.3), `HEAD-ON · ${e.robot} ⇄ ${e.with}`, "bad", now, "asking the traffic AI who goes first…"); this.ring(at, 0xff3b4f, now); } break; }
      case "yield": {
        if (!at) break;
        const ai = e.rule === "ai";
        const why = ai ? (e.reason || "traffic AI ruling") : { head_on: "fallback rule: higher number yields", head_on_retry: "fallback: lower number yields",
          parked: "blocked by a parked truck: re-route", queue: "waited too long: re-route", make_way: "letting the loaded truck out" }[e.rule] || e.rule;
        this.float(at.clone().setY(at.y + 1.45), `${ai ? "✦ TRAFFIC AI · " : ""}${e.robot} yields → ${e.to}`, ai ? "ai" : "arbiter", now, why);
        this.ring(at, ai ? 0xffc93c : 0xff9a1f, now);
        break;
      }
      case "contact": case "struck": {
        if (at) {
          this.ring(at, 0xff3b4f, now, 2.2); this.float(at.clone().setY(at.y + 1.3), "COLLISION", "bad", now, `${e.robot}${e.with ? ` × ${e.with}` : ""}`);
          this.dust.emit(at.x, at.y + .1, at.z, { n: 40, spread: .5, size: .6, grow: 1, life: 4, alpha: .5, vel: [0, .25, 0], jitter: .5, color: 0xb99470 });
        }
        break;
      }
      case "pothole_detected": {
        const p = cellPos(e.cell), kind = e.kind || "pothole";
        const what = kind === "sand" ? `SAND DRIFT · ${(e.depth / 1000).toFixed(1)} m` : kind === "bump" ? `BUMP · ${(e.depth / 1000).toFixed(2)} m ridge` : `POTHOLE · ${(e.depth / 1000).toFixed(1)} m deep`;
        const slow = kind === "sand" ? "18" : kind === "bump" ? "14" : "7";
        this.float(p.clone().setY(p.y + .7), what, kind === "sand" ? "info" : "warn", now, `${e.survey ? "lidar survey" : `${e.robot}'s lidar`} · ${Math.round(e.dist / 1000)} m out · ${slow} km/h there`);
        this.ring(p, kind === "sand" ? 0xfff0c0 : 0xffb020, now, .9);
        break;
      }
      case "bump_jump": { if (at) { w.jump = { t0: now, amp: Math.min(.9, e.v / 1400) }; this.float(at.clone().setY(at.y + 1.2), `${e.robot} · hit a bump at ${(e.v * .036).toFixed(0)} km/h`, "warn", now, "not mapped yet: the next survey maps it"); } break; }
      case "pothole_formed": { const p = cellPos(e.cell); this.dust.emit(p.x, p.y + .03, p.z, { n: 20, spread: .3, size: .35, grow: .5, life: 3, alpha: .4, vel: [0, .15, 0], jitter: .25, color: 0x8a6848 }); break; }
      case "pothole_enter": {
        if (!at) break;
        this.dust.emit(at.x, at.y + .03, at.z, { n: 10, spread: .35, size: .35, grow: .45, life: 2.6, alpha: .35, vel: [0, .1, 0], jitter: .22, color: 0xa9875f });
        if (e.v <= 250) this.float(at.clone().setY(at.y + 1.1), `${e.robot} · easing through a pothole`, "warn", now, `${(e.v * .036).toFixed(0)} km/h · ${(e.depth / 1000).toFixed(1)} m deep`);
        break;
      }
      case "pothole_strike": { if (at) this.float(at.clone().setY(at.y + 1.2), `POTHOLE STRIKE · ${e.robot}`, "bad", now, `hit at ${(e.v * .036).toFixed(0)} km/h: lidar did not see it`); break; }
      case "fault_alarm": { if (at) { this.ring(at, 0xff3b4f, now, 2); this.float(at.clone().setY(at.y + 1.5), `${e.robot} IS NOT WORKING`, "bad", now, `${e.code} · crew paged`); } break; }
      case "service_move": { if (at) this.float(at.clone().setY(at.y + 1.5), `✦ AI CONTROL · ${e.robot}`, "ai", now, `driving it to c${e.cell[0]}_${e.cell[1]} at limp speed`); break; }
      case "deployed": { if (at) { this.ring(at, 0x3ddc84, now, 1.6); this.float(at.clone().setY(at.y + 1.4), `${e.robot} DEPLOYED`, "ok", now, "standby truck joins the fleet"); } break; }
      case "repaired": { if (at) { this.ring(at, 0x3ddc84, now, 2); this.float(at.clone().setY(at.y + 1.5), `${e.robot} FIXED`, "ok", now, e.by ? `marked done by ${e.by}` : ""); } break; }
      case "scan_mismatch": { const s = this.map.slots[e.slot]; if (s) { const p = cellPos(s.cell); this.float(p.clone().setY(p.y + 1.6), `GRADE MISMATCH · face ${e.slot}`, "bad", now, "load refused · grade control notified"); } break; }
      case "grade_mislabeled": { const s = this.map.slots[e.slot]; if (s) { const p = cellPos(s.cell); this.float(p.clone().setY(p.y + 1.6), `FACE ${e.slot} GRADE TAG WRONG`, "warn", now, "the block model says ore; it isn't"); } break; }
      case "dock_scan": {
        const d = this.docks[e.dock]; if (!d) break;
        if (!e.ok) this.float(d.pos.clone().setY(d.pos.y + 1.4), "WRONG MATERIAL", "bad", now, `${e.dock}: expected ${(e.expected || []).join(", ")}`);
        break;
      }
      case "queue": { if (at) this.float(at.clone().setY(at.y + 1.1), `${e.robot} queues behind ${e.behind}`, "warn", now, "waiting for the excavator"); break; }
      default: break;
    }
  }

  // an AI decision drawn in the pit: dispatch = a gold arc from the truck to its excavator
  decision(d, now) {
    if (d.kind === "dispatch" && d.phase === "done") {
      const w = this.robots.get(d.robot), e = this.excavators.get(d.face);
      if (!w || !e) return;
      const a = w.group.position.clone(), b = e.spot.clone();
      const mid = a.clone().add(b).multiplyScalar(.5); mid.y = Math.max(a.y, b.y) + 1.4 + a.distanceTo(b) * .08;
      const curve = new THREE.QuadraticBezierCurve3(a.clone().setY(a.y + .5), mid, b.clone().setY(b.y + .3));
      const geo = new THREE.TubeGeometry(curve, 48, .018, 6, false);
      const m = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ color: d.by === "ai" ? 0xffc93c : 0xe8c77a, transparent: true, opacity: .9, depthWrite: false }));
      m.renderOrder = 8; this.scene.add(m);
      this.effects.push({ kind: "arc", obj: m, t0: now, dur: 5200 });
      this.float(a.clone().setY(a.y + 1.25), `${d.by === "ai" ? "✦ DISPATCH AI" : "RULES"} · ${d.robot} → ${d.face}`, d.by === "ai" ? "ai" : "arbiter", now,
        `${d.road_m ?? "?"} m by road${d.alone_s ? ` · digger alone ${d.alone_s} s` : ""}`);
    }
    if (d.kind === "traffic" && d.phase === "done" && d.cell) {
      const p = new THREE.Vector3(d.cell[0] + .5, this.ground(d.cell[0] + .5, d.cell[1] + .5), d.cell[1] + .5);
      this.float(p.clone().setY(p.y + 1.9), `${d.by === "ai" ? "✦ TRAFFIC AI" : "TRAFFIC RULES"} · ${d.first} goes first`, d.by === "ai" ? "ai" : "arbiter", now,
        `${d.yield} ${d.how === "reroute" ? "re-routes" : "pulls aside"}${d.ms ? ` · ${(d.ms / 1000).toFixed(1)} s` : ""}`);
    }
  }

  ring(pos, color, now, size = 1.1) {
    const m = new THREE.Mesh(new THREE.RingGeometry(0.3, 0.36, 48), new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.9, depthWrite: false, side: THREE.DoubleSide }));
    m.rotation.x = -Math.PI / 2; m.position.set(pos.x, pos.y + .04, pos.z); this.scene.add(m);
    this.effects.push({ kind: "ring", obj: m, t0: now, dur: 1100, size });
  }

  float(pos, title, kind, now, sub = "") {
    const el = document.createElement("div");
    el.className = `tag3d float ${kind}`;
    el.innerHTML = `<b></b>${sub ? "<span></span>" : ""}`;
    el.querySelector("b").textContent = title;
    if (sub) el.querySelector("span").textContent = sub;
    const anchor = document.createElement("div"); anchor.className = "anchor3d"; anchor.append(el);
    const o = new CSS2DObject(anchor); o.position.copy(pos); this.scene.add(o);
    this.effects.push({ kind: "float", obj: o, el, t0: now, dur: 4200, y0: pos.y });
  }

  animateEffects(dt, now) {
    this.effects = this.effects.filter((f) => {
      const k = (now - f.t0) / (f.dur || 1000);
      if (k < 0) return true;
      if (f.kind === "ring") {
        f.obj.scale.setScalar(1 + k * 3 * f.size); f.obj.material.opacity = Math.max(0, 0.9 * (1 - k));
        if (k >= 1) { this.scene.remove(f.obj); return false; } return true;
      }
      if (f.kind === "float") {
        f.obj.position.y = f.y0 + Math.min(1, k) * 0.35;
        f.el.style.opacity = k < 0.06 ? k / 0.06 : k > 0.82 ? Math.max(0, (1 - k) / 0.18) : 1;
        if (k >= 1) { this.drop(f.obj); return false; } return true;
      }
      if (f.kind === "arc") {
        f.obj.material.opacity = .9 * (k < .1 ? k / .1 : k > .7 ? (1 - k) / .3 : 1);
        if (k >= 1) { this.scene.remove(f.obj); f.obj.geometry.dispose(); return false; } return true;
      }
      if (f.kind === "fall") {
        const t = Math.min(1, (now - f.t0) / 700);
        f.obj.position.y = f.gy + 2.4 * (1 - t * t);
        f.obj.rotation.x += dt * 3 * (1 - t);
        if (t >= 1) {
          this.ring(f.obj.position.clone(), 0xc07a35, now, 1.3);
          this.dust.emit(f.x, f.gy + .05, f.z, { n: 60, spread: .5, size: .7, grow: 1.1, life: 5, alpha: .5, vel: [0, .22, 0], jitter: .55, color: 0xb08a63 });
          return false;
        }
        return true;
      }
      return false;
    });
  }

  // ------------------------------------------------------------ camera
  pick(cx, cy) {
    const rect = this.renderer.domElement.getBoundingClientRect();
    this.pointer.set(((cx - rect.left) / rect.width) * 2 - 1, -((cy - rect.top) / rect.height) * 2 + 1);
    this.ray.setFromCamera(this.pointer, this.camera);
    const targets = [...[...this.robots.values()].map((w) => w.t.hit).filter(Boolean), ...[...this.excavators.values()].map((e) => e.ex.hit).filter(Boolean)];
    const hits = this.ray.intersectObjects(targets, false);
    if (!hits.length) return null;
    const u = hits[0].object.userData;
    return u.excavator ? { face: u.excavator } : { robot: u.robot };
  }

  chasePose(w) {
    const p = w.group.position;
    const target = p.clone().setY(p.y + .35);
    const back = new THREE.Vector3(-Math.cos(w.yaw), 0, Math.sin(w.yaw));
    const still = w.speed === 0;
    const pos = target.clone().addScaledVector(back, still ? 2.4 : 3.6).setY(target.y + (still ? 2.6 : 1.9));
    const floor = this.ground(pos.x, pos.z) + .8;
    if (pos.y < floor) pos.y = floor;
    return { target, pos };
  }

  focus(id) {
    for (const w of this.robots.values()) { w.selected = w.id === id; w.askPin.visible = w.id === id; }
    this.follow = id; this.chase = true;
    const w = this.robots.get(id); if (!w) return;
    this.fly = { t0: performance.now(), dur: 1300, fromPos: this.camera.position.clone(), fromTarget: this.controls.target.clone() };
  }

  focusFace(slot) {
    const e = this.excavators.get(slot); if (!e) return;
    for (const w of this.robots.values()) { w.selected = false; w.askPin.visible = false; }
    this.follow = null; this.chase = false;
    const p = e.ex.group.position;
    const toPos = p.clone().add(new THREE.Vector3(2.2, 2.4, 2.6)), toTarget = p.clone().setY(p.y + .3);
    this.fly = { t0: performance.now(), dur: 1200, fromPos: this.camera.position.clone(), fromTarget: this.controls.target.clone(), toPos, toTarget };
  }

  overview() {
    for (const w of this.robots.values()) { w.selected = false; w.askPin.visible = false; }
    this.follow = null; this.chase = false;
    this.fly = { t0: performance.now(), dur: 1200, fromPos: this.camera.position.clone(), fromTarget: this.controls.target.clone(), toPos: this.home.pos.clone(), toTarget: this.home.target.clone() };
  }

  animateCamera(dt) {
    const w = this.follow && this.robots.get(this.follow);
    const now = performance.now();
    if (this.fly) {
      const k = Math.min(1, (now - this.fly.t0) / this.fly.dur), e = easeInOut(k);
      const pose = w ? this.chasePose(w) : { target: this.fly.toTarget, pos: this.fly.toPos };
      this.controls.target.lerpVectors(this.fly.fromTarget, pose.target, e);
      this.camera.position.lerpVectors(this.fly.fromPos, pose.pos, e);
      if (k >= 1) { this.fly = null; if (w) this.lastFollow = pose.target.clone(); }
      return;
    }
    if (!w) return;
    if (this.chase) {
      const pose = this.chasePose(w), a = 1 - Math.exp(-dt * 2.2);
      this.controls.target.lerp(pose.target, 1 - Math.exp(-dt * 9));
      this.camera.position.lerp(pose.pos, a);
      this.lastFollow = pose.target.clone();
      return;
    }
    const target = w.group.position.clone().setY(w.group.position.y + .35);
    const delta = target.clone().sub(this.lastFollow || target);
    this.camera.position.add(delta); this.controls.target.add(delta);
    this.lastFollow = target;
  }

  resize() {
    const w = this.host.clientWidth, h = this.host.clientHeight;
    if (!w || !h) return;
    this.renderer.setSize(w, h); this.css.setSize(w, h);
    this.camera.aspect = w / h; this.camera.updateProjectionMatrix();
  }
}
