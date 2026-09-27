// The live fulfilment centre in 3D: built from the sim's own map (1 unit = 1 m, cell (x, y) -> (x + .5, y + .5)),
// driven by the live 10 Hz frames, interpolated at display rate. Nothing here is scripted: every move,
// hold, standoff, yield, pick and delivery is the fleet on VM B.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { CSS2DRenderer, CSS2DObject } from "three/addons/renderers/CSS2DRenderer.js";
import { RoundedBoxGeometry } from "three/addons/geometries/RoundedBoxGeometry.js";
import { concrete, hazard, cardboard, paintText, plate, chevrons, radialGlow } from "./textures.js";
import { Trailer, cargoBox, makeWorker } from "./robot.js";

const CELL = 1;
const SHELF_LEVELS = [0.08, 0.6];
const RACK_H = 1.12;
const easeInOut = (k) => (k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2);
const cellCenter = (c) => new THREE.Vector3(c[0] + 0.5, 0, c[1] + 0.5);

export class Site3D {
  constructor(host, map, { accents, onPick, onHover, onAsk }) {
    this.host = host; this.map = map; this.accents = accents; this.onPick = onPick; this.onHover = onHover; this.onAsk = onAsk;
    this.robots = new Map(); this.overlays = new Map(); this.effects = []; this.labels = new Map();
    this.layers = { routes: true, claims: true, sensors: true, labels: true };
    this.follow = null; this.fly = null; this.last = 0;
    this.W = map.width; this.H = map.height;

    const r = this.renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
    r.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
    r.shadowMap.enabled = true; r.shadowMap.type = THREE.PCFShadowMap;
    r.outputColorSpace = THREE.SRGBColorSpace;
    r.toneMapping = THREE.ACESFilmicToneMapping; r.toneMappingExposure = 1.05;
    host.append(r.domElement);
    r.domElement.className = "gl";
    this.css = new CSS2DRenderer();
    this.css.domElement.className = "css2d";
    host.append(this.css.domElement);

    const scene = this.scene = new THREE.Scene();
    scene.background = new THREE.Color(0x030405);
    scene.fog = new THREE.Fog(0x030405, 30, 70);
    this.camera = new THREE.PerspectiveCamera(38, 1, 0.05, 200);
    // default view: high from the south-west, steep enough to see into the aisles, docks angled toward it
    this.home = { pos: new THREE.Vector3(this.W / 2 - 7.4, 22, this.H + 7.4), target: new THREE.Vector3(this.W / 2 - 2.6, 0, this.H / 2 + 1.7) };
    this.camera.position.copy(this.home.pos);
    const c = this.controls = new OrbitControls(this.camera, r.domElement);
    c.target.copy(this.home.target); c.enableDamping = true; c.dampingFactor = 0.08;
    c.maxPolarAngle = 1.36; c.minDistance = 2.2; c.maxDistance = 48; c.screenSpacePanning = true;
    c.addEventListener("start", () => { this.fly = null; this.chase = false; });   // the operator took the camera

    this.lights();
    this.buildFloor(); this.buildLanes(); this.buildRacks(); this.buildWalls(); this.buildDocks(); this.buildHomes(); this.buildGarage();
    for (const spec of [...map.robots, ...(map.spares || [])]) this.addRobot(spec.id);
    this.techs = new Map(); this.targets = new Map(); this.pings = new Map(); this.clockOffset = 0;
    this.pallets = new Map(); this.closed = new Map();

    this.ray = new THREE.Raycaster(); this.pointer = new THREE.Vector2();
    let down = null;
    r.domElement.addEventListener("pointerdown", (e) => { down = { x: e.clientX, y: e.clientY, t: performance.now() }; });
    r.domElement.addEventListener("pointerup", (e) => {
      if (!down) return;
      const moved = Math.hypot(e.clientX - down.x, e.clientY - down.y), quick = performance.now() - down.t < 500;
      down = null;
      if (moved < 7 && quick) { const id = this.pick(e.clientX, e.clientY); this.onPick && this.onPick(id); }
    });
    r.domElement.addEventListener("pointermove", (e) => {
      const id = this.pick(e.clientX, e.clientY);
      r.domElement.style.cursor = id ? "pointer" : "grab";
      if (id !== this.hovered) { this.hovered = id; this.onHover && this.onHover(id); }
    });
    this.resizeObs = new ResizeObserver(() => this.resize());
    this.resizeObs.observe(host);
    this.resize();
  }

  // Remove an object and any HTML labels inside it (three only cleans up a label that is itself removed).
  drop(obj) {
    this.scene.remove(obj);
    obj.traverse((o) => { if (o.isCSS2DObject && o.element) o.element.remove(); });
  }

  // ------------------------------------------------------------ static world
  lights() {
    const s = this.scene;
    s.add(new THREE.HemisphereLight(0xcfe3ff, 0x0b0c0e, 0.55));
    const key = new THREE.DirectionalLight(0xffffff, 2.1);
    key.position.set(this.W / 2 - 6, 22, this.H / 2 + 8);
    key.target.position.set(this.W / 2, 0, this.H / 2);
    key.castShadow = true;
    key.shadow.mapSize.set(2048, 2048);
    Object.assign(key.shadow.camera, { left: -15, right: 15, top: 10, bottom: -10, near: 1, far: 60 });
    key.shadow.bias = -0.0004; key.shadow.normalBias = 0.02;
    s.add(key, key.target);
    const fill = new THREE.DirectionalLight(0x88b8ff, 0.35);
    fill.position.set(this.W + 6, 8, -6); s.add(fill);
    // high-bay light pools on the floor
    const glow = radialGlow("255,244,220");
    for (let x = 3; x < this.W; x += 5) for (let y = 2.5; y < this.H; y += 4) {
      const p = new THREE.Mesh(new THREE.PlaneGeometry(4.5, 4.5), new THREE.MeshBasicMaterial({ map: glow, transparent: true, opacity: 0.05, depthWrite: false }));
      p.rotation.x = -Math.PI / 2; p.position.set(x, 0.006, y); s.add(p);
    }
  }

  buildFloor() {
    const g = new THREE.PlaneGeometry(this.W + 60, this.H + 60);
    const t = concrete().clone(); t.needsUpdate = true; t.repeat.set((this.W + 60) / 4, (this.H + 60) / 4);
    const floor = new THREE.Mesh(g, new THREE.MeshStandardMaterial({ map: t, roughness: 0.92, metalness: 0.05, color: 0x9aa3ad }));
    floor.rotation.x = -Math.PI / 2; floor.position.set(this.W / 2, 0, this.H / 2); floor.receiveShadow = true;
    this.scene.add(floor);
    const outside = new THREE.Mesh(new THREE.RingGeometry(0, 1, 4), new THREE.MeshBasicMaterial({ color: 0x000000 }));
    outside.visible = false; this.scene.add(outside);
  }

  passable(x, y) { const ch = (this.map.layout[y] || "")[x]; return ch && ch !== "#" && !/[A-F]/.test(ch); }

  buildLanes() {
    // White lane lines along every drivable connection: bright on the aisles, cross-aisle and dock lane,
    // a quieter grid on the open floor, node marks at every cell centre.
    const zonesAt = {};
    for (const [z, cells] of Object.entries(this.map.zones)) for (const c of cells) (zonesAt[c.join(",")] ||= new Set()).add(z);
    const open = (x, y) => { const z = zonesAt[`${x},${y}`]; return !z || z.has("south_floor") || z.has("home_area"); };
    const main = [], soft = [];
    for (let y = 0; y < this.H; y++) for (let x = 0; x < this.W; x++) {
      if (!this.passable(x, y)) continue;
      for (const [dx, dz] of [[1, 0], [0, 1]]) {
        if (!this.passable(x + dx, y + dz)) continue;
        ((open(x, y) && open(x + dx, y + dz)) ? soft : main).push([x + 0.5, y + 0.5, dx, dz]);
      }
    }
    const m = new THREE.Matrix4(), q = new THREE.Quaternion(), up = new THREE.Vector3(0, 1, 0);
    const lay = (segs, width, mat) => {
      const inst = new THREE.InstancedMesh(new THREE.BoxGeometry(1, 0.004, width), mat, segs.length);
      segs.forEach(([x, z, dx, dz], i) => {
        q.setFromAxisAngle(up, dx ? 0 : Math.PI / 2);
        m.compose(new THREE.Vector3(x + dx / 2, 0.003, z + dz / 2), q, new THREE.Vector3(1, 1, 1));
        inst.setMatrixAt(i, m);
      });
      inst.receiveShadow = true;
      this.scene.add(inst);
    };
    lay(main, 0.06, new THREE.MeshStandardMaterial({ color: 0xf1f4f7, roughness: 0.55, emissive: 0xb8c2cc, emissiveIntensity: 0.28 }));
    lay(soft, 0.028, new THREE.MeshBasicMaterial({ color: 0xc9d1d9, transparent: true, opacity: 0.32, depthWrite: false }));
    const nodes = [], quiet = [];
    for (let y = 0; y < this.H; y++) for (let x = 0; x < this.W; x++) if (this.passable(x, y)) (open(x, y) ? quiet : nodes).push([x + 0.5, y + 0.5]);
    const dot = (list, r, mat) => {
      const d = new THREE.InstancedMesh(new THREE.CylinderGeometry(r, r, 0.005, 20), mat, list.length);
      list.forEach(([x, z], i) => { m.makeTranslation(x, 0.004, z); d.setMatrixAt(i, m); });
      this.scene.add(d);
    };
    dot(nodes, 0.08, new THREE.MeshBasicMaterial({ color: 0xf1f4f7 }));
    dot(quiet, 0.045, new THREE.MeshBasicMaterial({ color: 0xc9d1d9, transparent: true, opacity: 0.45 }));
    // painted lane names
    const paint = (text, x, z, { size = 0.34, rot = 0, color } = {}) => {
      const { texture, aspect } = paintText(text, color ? { color } : {});
      const p = new THREE.Mesh(new THREE.PlaneGeometry(size * aspect, size),
        new THREE.MeshBasicMaterial({ map: texture, transparent: true, depthWrite: false, opacity: 0.9 }));
      p.rotation.x = -Math.PI / 2; p.rotation.z = rot; p.position.set(x, 0.007, z);
      this.scene.add(p);
    };
    const R = -Math.PI / 2;
    for (const row of [1, 3, 5, 7]) {
      paint(`AISLE ${row}W`, 2.9, row + 0.5 + 0.02, { size: 0.3 });
      paint(`AISLE ${row}E`, 12.9, row + 0.5 + 0.02, { size: 0.3 });
    }
    paint("CROSS AISLE", 10.5, 5.0, { size: 0.2, rot: R });
    paint("DOCK LANE", 20.5, 5.0, { size: 0.24, rot: R });
    paint("SOUTH FLOOR", 12.5, 9.5, { size: 0.5 });
    paint("TO DOCKS  →", 17.4, 10.5, { size: 0.3 });
    paint("1.2 m/s MAX", 7.2, 8.5, { size: 0.24, color: "rgba(242,194,48,0.9)" });
  }

  buildRacks() {
    // Pallet racking: graphite uprights, safety-yellow beams, full of cartons. Picked from the aisle below.
    const upMat = new THREE.MeshStandardMaterial({ color: 0x2b3038, roughness: 0.5, metalness: 0.6 });
    const beamMat = new THREE.MeshStandardMaterial({ color: 0xe0b12a, roughness: 0.45, metalness: 0.4 });
    const deckMat = new THREE.MeshStandardMaterial({ color: 0x3a3f47, roughness: 0.7, metalness: 0.5 });
    const bays = Object.entries(this.map.slots);
    const up = new THREE.InstancedMesh(new THREE.BoxGeometry(0.07, RACK_H, 0.07), upMat, bays.length * 4);
    const beams = new THREE.InstancedMesh(new THREE.BoxGeometry(1, 0.07, 0.05), beamMat, bays.length * SHELF_LEVELS.length * 2);
    const decks = new THREE.InstancedMesh(new THREE.BoxGeometry(0.98, 0.025, 0.86), deckMat, bays.length * SHELF_LEVELS.length);
    const m = new THREE.Matrix4();
    let iu = 0, ib = 0, id = 0;
    this.bayPos = {};
    const boxes = [];
    for (const [slot, s] of bays) {
      const [x, y] = s.cell;
      for (const dx of [0.04, 0.96]) for (const dz of [0.07, 0.93]) { m.makeTranslation(x + dx, RACK_H / 2, y + dz); up.setMatrixAt(iu++, m); }
      SHELF_LEVELS.forEach((h, li) => {
        for (const dz of [0.07, 0.93]) { m.makeTranslation(x + 0.5, h + 0.02, y + dz); beams.setMatrixAt(ib++, m); }
        m.makeTranslation(x + 0.5, h + 0.05, y + 0.5); decks.setMatrixAt(id++, m);
        const n = 2 + ((x * 7 + y * 3 + li) % 2);
        for (let k = 0; k < n; k++) {
          const w = 0.26 + ((x + k + li) % 3) * 0.05, hh = 0.2 + ((x * k + li) % 3) * 0.08;
          boxes.push([x + 0.2 + k * (0.62 / Math.max(1, n - 1)) * (n > 1 ? 1 : 0), h + 0.07 + hh / 2, y + 0.5 + ((k + li) % 2 ? 0.14 : -0.12), w, hh, 0.36]);
        }
      });
      this.bayPos[slot] = new THREE.Vector3(x + 0.5, SHELF_LEVELS[1] + 0.2, y + 0.85);
      // bay label on the pick face (aisle side, +z)
      const lab = new THREE.Mesh(new THREE.PlaneGeometry(0.34, 0.17), new THREE.MeshBasicMaterial({ map: plate(slot, { accent: "#e0b12a" }) }));
      lab.position.set(x + 0.5, SHELF_LEVELS[1] - 0.06, y + 0.96); this.scene.add(lab);
    }
    up.castShadow = beams.castShadow = decks.castShadow = true; decks.receiveShadow = true;
    this.scene.add(up, beams, decks);
    this.addBoxes(boxes);
    // rack end caps: big letters
    for (const [row, left, right] of [[2, "A", "B"], [4, "C", "D"], [6, "E", "F"]]) {
      for (const [letter, x] of [[left, 1.95], [right, 11.95], [left, 10.05], [right, 20.05]]) {
        const cap = new THREE.Mesh(new THREE.PlaneGeometry(0.62, 0.62), new THREE.MeshBasicMaterial({ map: plate(letter, { w: 128, h: 128, size: 92, accent: "#e0b12a" }) }));
        cap.position.set(x, 0.78, row + 0.5); cap.rotation.y = x < 11 && x > 5 ? Math.PI / 2 : x > 15 ? Math.PI / 2 : -Math.PI / 2;
        this.scene.add(cap);
      }
    }
    // highlight used for picks and mislabels
    this.bayHi = new THREE.Mesh(new THREE.BoxGeometry(1.02, 0.7, 0.92), new THREE.MeshBasicMaterial({ color: 0x4da3ff, transparent: true, opacity: 0, depthWrite: false }));
    this.scene.add(this.bayHi);
  }

  addBoxes(list) {
    const geo = new RoundedBoxGeometry(1, 1, 1, 2, 0.04);
    const mat = new THREE.MeshStandardMaterial({ map: cardboard(), roughness: 0.86 });
    const inst = new THREE.InstancedMesh(geo, mat, list.length);
    const m = new THREE.Matrix4(), q = new THREE.Quaternion(), col = new THREE.Color();
    list.forEach(([x, y, z, w, h, d], i) => {
      q.setFromAxisAngle(new THREE.Vector3(0, 1, 0), (((i * 37) % 7) - 3) * 0.02);
      m.compose(new THREE.Vector3(x, y, z), q, new THREE.Vector3(w, h, d));
      inst.setMatrixAt(i, m);
      col.setHSL(0.075 + ((i * 13) % 9) * 0.003, 0.42, 0.52 + ((i * 7) % 5) * 0.03);
      inst.setColorAt(i, col);
    });
    inst.castShadow = true; inst.receiveShadow = true;
    this.scene.add(inst);
  }

  buildWalls() {
    // Perimeter: tall wall shelving full of cartons (north and west), a low guard rail on the south side
    // so the camera sees in, dock doors on the east.
    const shelfMat = new THREE.MeshStandardMaterial({ color: 0x252a31, roughness: 0.55, metalness: 0.6 });
    const wallMat = new THREE.MeshStandardMaterial({ color: 0x0d0f12, roughness: 0.95 });
    const boxes = [], parts = [];
    const levels = [0.1, 0.78, 1.46];
    const shelfRun = (x, z, alongX) => {
      for (const h of levels) {
        parts.push([x, h, z, alongX ? 1 : 0.7, 0.04, alongX ? 0.7 : 1]);
        for (let k = 0; k < 3; k++) {
          const off = -0.3 + k * 0.3, w = 0.24 + ((x + k) % 3) * 0.03, hh = 0.24 + ((x * 3 + z + k + h * 10) % 3) * 0.08;
          if ((x * 5 + z * 3 + k + h * 7) % 11 === 0) continue;   // the odd gap: it's a working store
          boxes.push(alongX ? [x + off, h + 0.02 + hh / 2, z, w, hh, 0.44] : [x, h + 0.02 + hh / 2, z + off, 0.44, hh, w]);
        }
      }
      parts.push([x, 1.1, z, alongX ? 0.05 : 0.7, 2.2, alongX ? 0.7 : 0.05]);
    };
    for (let x = 1; x < this.W - 1; x++) shelfRun(x + 0.5, 0.55, true);
    for (let y = 1; y < this.H - 1; y++) {
      const ch = this.map.layout[y][this.W - 2];
      const nearDock = [y - 1, y, y + 1].some((yy) => /[1-3]/.test((this.map.layout[yy] || "")[this.W - 2] || ""));
      if (!nearDock && ch !== undefined) shelfRun(this.W - 0.55, y + 0.5, false);
    }
    const inst = new THREE.InstancedMesh(new THREE.BoxGeometry(1, 1, 1), shelfMat, parts.length);
    const m = new THREE.Matrix4();
    parts.forEach(([x, y, z, w, h, d], i) => { m.compose(new THREE.Vector3(x, y, z), new THREE.Quaternion(), new THREE.Vector3(w, h, d)); inst.setMatrixAt(i, m); });
    inst.castShadow = true; inst.receiveShadow = true;
    this.scene.add(inst);
    this.addBoxes(boxes);
    // back walls behind the shelving, and the east wall around the dock doors
    const north = new THREE.Mesh(new THREE.BoxGeometry(this.W, 3.2, 0.2), wallMat); north.position.set(this.W / 2, 1.6, 0.1);
    this.scene.add(north);
    for (let y = 1; y < this.H - 1; y++) {
      if (this.map.layout[y][this.W - 2] >= "1" && this.map.layout[y][this.W - 2] <= "3") continue;
      const seg = new THREE.Mesh(new THREE.BoxGeometry(0.2, 3.2, 1), wallMat); seg.position.set(this.W - 0.9, 1.6, y + 0.5); this.scene.add(seg);
    }
    // south and west: low yellow/black guard rails so the camera sees in
    const postMat = new THREE.MeshStandardMaterial({ color: 0xf2c230, roughness: 0.45 });
    const railRun = (x0, z0, len, alongX) => {
      const t = hazard({ w: 512, h: 64, stripe: 24 }).clone(); t.needsUpdate = true; t.repeat.set(len / 2, 1);
      const rail = new THREE.Mesh(alongX ? new THREE.BoxGeometry(len, 0.12, 0.08) : new THREE.BoxGeometry(0.08, 0.12, len),
        new THREE.MeshStandardMaterial({ map: t, roughness: 0.5 }));
      rail.position.set(alongX ? x0 + len / 2 : x0, 0.55, alongX ? z0 : z0 + len / 2); rail.castShadow = true;
      this.scene.add(rail);
      for (let k = 0; k <= len; k += 2) {
        const p = new THREE.Mesh(new THREE.CylinderGeometry(0.05, 0.05, 0.62, 12), postMat);
        p.position.set(alongX ? x0 + k : x0, 0.31, alongX ? z0 : z0 + k); p.castShadow = true; this.scene.add(p);
      }
    };
    railRun(0.6, this.H - 0.9, this.W - 1.2, true);
    railRun(0.9, 0.6, this.H - 1.5, false);
  }

  buildDocks() {
    // Each dock: a hazard-hatched drop bay on the floor, a loading counter with a roller top, a roll-up door.
    this.docks = {};
    const counterMat = new THREE.MeshStandardMaterial({ color: 0x2a2f36, roughness: 0.5, metalness: 0.6 });
    const rollerMat = new THREE.MeshStandardMaterial({ color: 0x9aa3ad, roughness: 0.3, metalness: 0.9 });
    const doorMat = new THREE.MeshStandardMaterial({ color: 0x15181c, roughness: 0.6, metalness: 0.4 });
    for (const [id, cell] of Object.entries(this.map.docks)) {
      const [x, y] = cell;
      const hatch = hazard({ w: 256, h: 256, stripe: 26 }).clone(); hatch.needsUpdate = true; hatch.repeat.set(1.3, 1.3);
      const bay = new THREE.Mesh(new THREE.PlaneGeometry(0.96, 0.96), new THREE.MeshStandardMaterial({ map: hatch, roughness: 0.7, transparent: true, opacity: 0.55 }));
      bay.rotation.x = -Math.PI / 2; bay.position.set(x + 0.5, 0.005, y + 0.5); bay.receiveShadow = true;
      const counter = new THREE.Group();
      const body = new THREE.Mesh(new RoundedBoxGeometry(0.7, 0.72, 0.95, 2, 0.03), counterMat);
      body.position.y = 0.36; body.castShadow = true; body.receiveShadow = true;
      counter.add(body);
      for (let k = 0; k < 7; k++) { const r = new THREE.Mesh(new THREE.CylinderGeometry(0.025, 0.025, 0.66, 10), rollerMat); r.rotation.x = Math.PI / 2; r.position.set(-0.3 + k * 0.1, 0.745, 0); counter.add(r); }
      const edge = new THREE.Mesh(new THREE.BoxGeometry(0.72, 0.04, 0.02), new THREE.MeshStandardMaterial({ map: hazard(), roughness: 0.5 }));
      edge.position.set(0, 0.7, 0.48); counter.add(edge);
      counter.position.set(x + 1.35, 0, y + 0.5);
      const door = new THREE.Mesh(new THREE.BoxGeometry(0.08, 2.8, 1.5), doorMat);
      door.position.set(x + 1.78, 1.4, y + 0.5);
      const slats = new THREE.Mesh(new THREE.PlaneGeometry(1.4, 2.7), new THREE.MeshStandardMaterial({ color: 0x2a3038, roughness: 0.5, metalness: 0.5 }));
      slats.rotation.y = -Math.PI / 2; slats.position.set(x + 1.73, 1.4, y + 0.5);
      const glow = new THREE.PointLight(0xffe2a8, 1.4, 3.2, 2); glow.position.set(x + 1.1, 2.2, y + 0.5);
      this.scene.add(bay, counter, door, slats, glow);
      const stack = new THREE.Group(); stack.position.set(x + 1.35, 0.78, y + 0.5); this.scene.add(stack);
      // LED board above the door: dock, carrier, and the running count of units loaded
      const cv = document.createElement("canvas"); cv.width = 512; cv.height = 256;
      const tex = new THREE.CanvasTexture(cv); tex.colorSpace = THREE.SRGBColorSpace; tex.anisotropy = 8;
      const board = new THREE.Mesh(new THREE.BoxGeometry(0.06, 0.8, 1.6), [
        new THREE.MeshStandardMaterial({ color: 0x0b0c0e }), new THREE.MeshBasicMaterial({ map: tex }),
        ...Array(4).fill(new THREE.MeshStandardMaterial({ color: 0x0b0c0e, metalness: 0.5, roughness: 0.4 }))]);
      board.position.set(x + 1.7, 2.45, y + 0.5); board.rotation.y = 0.55;   // angled toward the floor
      this.scene.add(board);
      this.docks[id] = { counter, stack, board, cv, tex, pos: new THREE.Vector3(x + 1.35, 0.8, y + 0.5), count: 0, shown: -1, carrier: "", bump: 0, id };
      this.drawBoard(this.docks[id]);
    }
  }

  drawBoard(d) {
    const g = d.cv.getContext("2d"), w = d.cv.width, h = d.cv.height;
    g.fillStyle = "#07080a"; g.fillRect(0, 0, w, h);
    g.strokeStyle = "#f2c230"; g.lineWidth = 6; g.strokeRect(6, 6, w - 12, h - 12);
    g.fillStyle = "#f2c230"; g.font = "800 44px Inter, Arial, sans-serif"; g.textBaseline = "top";
    g.fillText(d.id.replace("DK", "DOCK "), 30, 26);
    g.fillStyle = "#8f98a3"; g.font = "600 30px Inter, Arial, sans-serif";
    g.fillText((d.carrier || "").slice(0, 24), 30, 82);
    g.fillStyle = "#ffd76a"; g.shadowColor = "#ffb020"; g.shadowBlur = 18;
    g.font = "800 92px ui-monospace, Menlo, monospace"; g.textAlign = "right";
    g.fillText(String(Math.max(0, Math.round(d.shown))), w - 30, 120);
    g.shadowBlur = 0; g.textAlign = "left"; g.fillStyle = "#8f98a3"; g.font = "600 26px Inter, Arial, sans-serif";
    g.fillText("UNITS LOADED", 30, 196);
    d.tex.needsUpdate = true;
  }

  buildHomes() {
    const homes = this.map.homes || [];
    homes.forEach(([x, y], i) => {
      const t = hazard({ w: 256, h: 32, stripe: 12 });
      const mat = new THREE.MeshStandardMaterial({ map: t, roughness: 0.6 });
      for (const [w, d, dx, dz] of [[0.9, 0.05, 0, -0.45], [0.9, 0.05, 0, 0.45], [0.05, 0.9, -0.45, 0], [0.05, 0.9, 0.45, 0]]) {
        const b = new THREE.Mesh(new THREE.BoxGeometry(w, 0.004, d), mat); b.position.set(x + 0.5 + dx, 0.004, y + 0.5 + dz); this.scene.add(b);
      }
      const { texture, aspect } = paintText(`CHARGE ${i + 1}`, { color: "rgba(242,194,48,0.85)" });
      const p = new THREE.Mesh(new THREE.PlaneGeometry(0.16 * aspect, 0.16), new THREE.MeshBasicMaterial({ map: texture, transparent: true, depthWrite: false }));
      p.rotation.x = -Math.PI / 2; p.position.set(x + 0.5, 0.008, y + 0.88); this.scene.add(p);
      const pad = new THREE.Mesh(new THREE.BoxGeometry(0.3, 0.03, 0.14), new THREE.MeshStandardMaterial({ color: 0x1d2228, emissive: 0x0a3a24, emissiveIntensity: 0.9 }));
      pad.position.set(x + 0.5, 0.015, y + 0.12); this.scene.add(pad);
    });
  }

  buildGarage() {
    // Maintenance bays (the spare parks in G1), a tool crib and a service sign, south-west corner.
    const bays = this.map.garage || [];
    bays.forEach(([x, y], i) => {
      const t = hazard({ w: 256, h: 256, stripe: 22 }).clone(); t.needsUpdate = true; t.repeat.set(1.6, 1.6);
      const hatch = new THREE.Mesh(new THREE.PlaneGeometry(0.94, 0.94), new THREE.MeshStandardMaterial({ map: t, transparent: true, opacity: 0.35, roughness: 0.7 }));
      hatch.rotation.x = -Math.PI / 2; hatch.position.set(x + 0.5, 0.006, y + 0.5); this.scene.add(hatch);
      const { texture, aspect } = paintText(`BAY G${i + 1}`, { color: "rgba(242,194,48,0.95)" });
      const p = new THREE.Mesh(new THREE.PlaneGeometry(0.16 * aspect, 0.16), new THREE.MeshBasicMaterial({ map: texture, transparent: true, depthWrite: false }));
      p.rotation.x = -Math.PI / 2; p.position.set(x + 0.5, 0.009, y + 0.12); this.scene.add(p);
    });
    if (bays.length) {
      const { texture, aspect } = paintText("MAINTENANCE", { color: "rgba(242,194,48,0.9)" });
      const p = new THREE.Mesh(new THREE.PlaneGeometry(0.22 * aspect, 0.22), new THREE.MeshBasicMaterial({ map: texture, transparent: true, depthWrite: false }));
      p.rotation.x = -Math.PI / 2; p.position.set(bays[1][0] + 0.5, 0.009, bays[1][1] - 0.18); this.scene.add(p);
      // tool crib: a red roll cabinet where technicians start
      const cab = new THREE.Group();
      const red = new THREE.MeshStandardMaterial({ color: 0xc81e2a, roughness: 0.35, metalness: 0.4 });
      const body = new THREE.Mesh(new RoundedBoxGeometry(0.62, 0.95, 0.42, 2, 0.03), red); body.position.y = 0.52; cab.add(body);
      for (let k = 0; k < 5; k++) { const d = new THREE.Mesh(new THREE.BoxGeometry(0.56, 0.012, 0.01), new THREE.MeshStandardMaterial({ color: 0x2a2f36, metalness: 0.8 })); d.position.set(0, 0.2 + k * 0.16, 0.212); cab.add(d); }
      const top = new THREE.Mesh(new THREE.BoxGeometry(0.64, 0.03, 0.44), new THREE.MeshStandardMaterial({ color: 0x20252b, metalness: 0.6 })); top.position.y = 1.0; cab.add(top);
      cab.traverse((o) => { o.castShadow = true; });
      cab.position.set(4.5, 0, 11.72); this.scene.add(cab);
      const el = document.createElement("div"); el.className = "tag3d crib"; el.innerHTML = `<b>SERVICE</b><span>tool crib · garage G1–G${bays.length}</span>`;
      const lab = new CSS2DObject(el); lab.position.set(3.2, 1.35, 11.5); this.scene.add(lab);
      this.cribLabel = lab;
    }
  }

  // technicians walking to a trailer and repairing it (driven by service-case times from the control plane)
  setTechs(list, serverOffset) {
    this.clockOffset = serverOffset;
    const want = new Set(list.map((t) => t.id));
    for (const [id, w] of this.techs) if (!want.has(id)) { this.drop(w.group); this.techs.delete(id); }
    for (const t of list) {
      let w = this.techs.get(t.id);
      if (!w) {
        const group = makeWorker();
        const el = document.createElement("div"); el.className = "tag3d tech";
        el.innerHTML = `<b></b><span class="what"></span><i class="bar"><u></u></i>`;
        const anchor = document.createElement("div"); anchor.className = "anchor3d"; anchor.append(el);
        const lab = new CSS2DObject(anchor); lab.position.set(0, 1.75, 0); group.add(lab);
        const ring = new THREE.Mesh(new THREE.RingGeometry(0.22, 0.26, 32), new THREE.MeshBasicMaterial({ color: 0xc8f03c, transparent: true, opacity: 0.7, depthWrite: false }));
        ring.rotation.x = -Math.PI / 2; ring.position.y = 0.012; group.add(ring);
        this.scene.add(group);
        w = { group, el, spark: 0 };
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
      const f = k * (route.length - 1), i = Math.min(route.length - 2, Math.floor(f)), frac = f - i;
      const a = route[Math.max(0, i)], b = route[Math.min(route.length - 1, i + 1)];
      const x = a[0] + 0.5 + (b[0] - a[0]) * frac, z = a[1] + 0.5 + (b[1] - a[1]) * frac;
      const g = w.group, u = g.userData;
      g.position.set(x, 0, z);
      const walking = k < 1;
      if (walking && (b[0] !== a[0] || b[1] !== a[1])) g.rotation.y = Math.atan2(-(b[1] - a[1]), b[0] - a[0]);
      const swing = walking ? Math.sin(now / 130) * 0.45 : 0;
      u.legL.rotation.z = swing; u.legR.rotation.z = -swing;
      g.position.y = walking ? Math.abs(Math.sin(now / 130)) * 0.03 : 0;
      const target = this.robots.get(t.robot);
      const working = !walking && t.repair_until;
      if (!walking && target) g.rotation.y = Math.atan2(-(target.group.position.z - z), target.group.position.x - x);
      u.torso.rotation.z = working ? -0.35 : 0; u.torso.position.y = working ? 0.82 : 0.95;
      const total = t.repair_until ? t.repair_until - t.arrive_at : 1;
      const prog = working ? Math.min(1, Math.max(0, (clock - t.arrive_at) / total)) : 0;
      w.el.querySelector(".what").textContent = walking ? `walking over · ETA ${Math.max(0, Math.ceil(t.arrive_at - clock))} s`
        : working ? `${t.task || "repairing"} · ${Math.round(prog * 100)}%` : "on site";
      w.el.querySelector("u").style.width = `${walking ? k * 100 : prog * 100}%`;
      w.el.dataset.mode = walking ? "walk" : "work";
      if (working && now - w.spark > 260 && target) {   // sparks while the wrench turns
        w.spark = now;
        this.effects.push({ kind: "spark", obj: this.sparkAt(target.group.position), t0: now, dur: 420 });
      }
    }
  }

  sparkAt(pos) {
    const m = new THREE.Mesh(new THREE.SphereGeometry(0.025, 6, 6), new THREE.MeshBasicMaterial({ color: 0xfff2a8 }));
    m.position.set(pos.x + (Math.random() - 0.5) * 0.3, 0.12 + Math.random() * 0.15, pos.z + (Math.random() - 0.5) * 0.3);
    m.userData.v = new THREE.Vector3((Math.random() - 0.5) * 1.2, 1 + Math.random(), (Math.random() - 0.5) * 1.2);
    this.scene.add(m);
    return m;
  }

  // the box each trailer is about to pick: outlined in its colour, with the barcode it will scan
  setTargets(list) {
    const want = new Map(list.map((x) => [`${x.robot}:${x.slot}`, x]));
    for (const [k, m] of this.targets) if (!want.has(k)) { this.drop(m.group); this.targets.delete(k); }
    for (const [k, x] of want) {
      let m = this.targets.get(k);
      const p = this.bayPos[x.slot];
      if (!p) continue;
      if (!m) {
        const group = new THREE.Group();
        const col = new THREE.Color(this.accents[x.robot] || "#fff");
        const edges = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(0.4, 0.34, 0.36)),
          new THREE.LineBasicMaterial({ color: col, transparent: true, opacity: 0.95 }));
        const glow = new THREE.Mesh(new THREE.BoxGeometry(0.42, 0.36, 0.38), new THREE.MeshBasicMaterial({ color: col, transparent: true, opacity: 0.12, depthWrite: false }));
        group.add(edges, glow);
        group.position.set(p.x, SHELF_LEVELS[1] + 0.07 + 0.17, p.z - 0.12);
        const el = document.createElement("div"); el.className = "tag3d target"; el.style.setProperty("--acc", this.accents[x.robot] || "#fff");
        const anchor = document.createElement("div"); anchor.className = "anchor3d"; anchor.append(el);
        const lab = new CSS2DObject(anchor); lab.position.set(0, 0.42, 0); group.add(lab);
        this.scene.add(group);
        m = { group, el, edges, glow, html: "" };
        this.targets.set(k, m);
      }
      if (m.html !== x.html) { m.html = x.html; m.el.innerHTML = x.html; }
      m.el.classList.toggle("focus", !!x.focus);
    }
  }

  animateTargets(now) {
    const k = (Math.sin(now / 260) + 1) / 2;
    for (const m of this.targets.values()) { m.glow.material.opacity = 0.08 + 0.16 * k; m.edges.material.opacity = 0.6 + 0.4 * k; }
  }

  // ------------------------------------------------------------ robots and their overlays
  addRobot(id) {
    const t = new Trailer(id, this.accents[id] || "#ffffff");
    t.body.scale.setScalar(1.3);
    this.scene.add(t.group);
    const halo = new THREE.Mesh(new THREE.PlaneGeometry(1.7, 1.7), new THREE.MeshBasicMaterial({
      map: radialGlow(new THREE.Color(this.accents[id] || "#fff").toArray().map((v) => Math.round(v * 255)).join(",")),
      transparent: true, opacity: 0.42, depthWrite: false }));
    halo.rotation.x = -Math.PI / 2; halo.position.y = 0.009; halo.renderOrder = 1;
    t.group.add(halo);
    const el = document.createElement("div");
    el.className = "tag3d robot";
    el.innerHTML = `<span class="dot"></span><b>${id}</b><span class="st">idle</span>`;
    el.style.setProperty("--acc", this.accents[id] || "#fff");
    el.addEventListener("pointerup", (e) => { e.stopPropagation(); this.onPick && this.onPick(id); });
    const tag = new CSS2DObject(el); tag.position.set(0, 1.0, 0); t.group.add(tag);
    t.tagEl = el;
    // the "ask about this robot" pin, shown above the robot being followed
    const ask = document.createElement("button");
    ask.className = "tag3d askpin"; ask.innerHTML = `<span class="orb">✦</span>Ask ${id}`;
    ask.addEventListener("pointerup", (e) => { e.stopPropagation(); this.onAsk && this.onAsk(id); });
    const anchor = document.createElement("div"); anchor.className = "anchor3d"; anchor.append(ask);
    const pin = new CSS2DObject(anchor); pin.position.set(0, 1.42, 0); pin.visible = false; t.group.add(pin);
    t.askPin = pin;
    const acc = new THREE.Color(this.accents[id] || "#fff");
    const route = new THREE.Mesh(new THREE.BufferGeometry(), new THREE.MeshBasicMaterial({
      map: chevrons("rgba(255,255,255,0.95)").clone(), color: acc, transparent: true, opacity: 0.8, depthWrite: false }));
    route.material.map.needsUpdate = true;
    route.renderOrder = 2;
    const claims = new THREE.InstancedMesh(new THREE.BoxGeometry(0.92, 0.006, 0.92), new THREE.MeshBasicMaterial({ color: acc, transparent: true, opacity: 0.2, depthWrite: false }), 8);
    claims.count = 0; claims.renderOrder = 1;
    const claimEdges = new THREE.InstancedMesh(new THREE.BoxGeometry(0.96, 0.008, 0.96), new THREE.MeshBasicMaterial({ color: acc, transparent: true, opacity: 0.55, depthWrite: false, wireframe: true }), 8);
    claimEdges.count = 0;
    this.scene.add(route, claims, claimEdges);
    this.robots.set(id, t);
    this.overlays.set(id, { route, claims, claimEdges, routeKey: "", link: null });
  }

  setRoute(id, pts) {
    const o = this.overlays.get(id);
    const key = pts.map((p) => `${p.x.toFixed(2)},${p.z.toFixed(2)}`).join(";");
    if (key === o.routeKey) return;
    o.routeKey = key;
    // flat ribbon along the polyline; u runs along the length (chevrons scroll along it)
    const w = 0.16, pos = [], uv = [], idx = [];
    let len = 0;
    for (let i = 0; i < pts.length; i++) {
      const a = pts[Math.max(0, i - 1)], b = pts[Math.min(pts.length - 1, i + 1)];
      const dir = new THREE.Vector3().subVectors(b, a).normalize(), n = new THREE.Vector3(-dir.z, 0, dir.x).multiplyScalar(w / 2);
      if (i > 0) len += pts[i].distanceTo(pts[i - 1]);
      pos.push(pts[i].x + n.x, 0.012, pts[i].z + n.z, pts[i].x - n.x, 0.012, pts[i].z - n.z);
      uv.push(len / 0.28, 0, len / 0.28, 1);
      if (i > 0) { const k = i * 2; idx.push(k - 2, k - 1, k, k - 1, k + 1, k); }
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    g.setAttribute("uv", new THREE.Float32BufferAttribute(uv, 2));
    g.setIndex(idx);
    o.route.geometry.dispose(); o.route.geometry = g;
  }

  setClaims(id, cells) {
    const o = this.overlays.get(id), m = new THREE.Matrix4();
    const n = Math.min(8, cells.length);
    for (let i = 0; i < n; i++) { m.makeTranslation(cells[i][0] + 0.5, 0.008 + i * 0.0005, cells[i][1] + 0.5); o.claims.setMatrixAt(i, m); o.claimEdges.setMatrixAt(i, m); }
    o.claims.count = o.claimEdges.count = n;
    o.claims.instanceMatrix.needsUpdate = o.claimEdges.instanceMatrix.needsUpdate = true;
  }

  // hold link between a waiting robot and the one it waits for
  setLink(id, other, kind) {
    const o = this.overlays.get(id);
    if (!other) { if (o.link) { this.scene.remove(o.link.line); o.link = null; } return; }
    if (!o.link || o.link.other !== other || o.link.kind !== kind) {
      if (o.link) this.scene.remove(o.link.line);
      const mat = new THREE.LineDashedMaterial({ color: kind === "standoff" ? 0xff3b5c : 0xffb020, dashSize: 0.1, gapSize: 0.07, transparent: true, opacity: 0.95 });
      const line = new THREE.Line(new THREE.BufferGeometry(), mat);
      line.renderOrder = 3;
      this.scene.add(line);
      o.link = { other, kind, line };
    }
    const a = this.robots.get(id).group.position, b = this.robots.get(other).group.position;
    const mid = new THREE.Vector3().addVectors(a, b).multiplyScalar(0.5); mid.y = 0.9 + a.distanceTo(b) * 0.15;
    const curve = new THREE.QuadraticBezierCurve3(new THREE.Vector3(a.x, 0.55, a.z), mid, new THREE.Vector3(b.x, 0.55, b.z));
    o.link.line.geometry.dispose();
    o.link.line.geometry = new THREE.BufferGeometry().setFromPoints(curve.getPoints(24));
    o.link.line.computeLineDistances();
  }

  // ------------------------------------------------------------ per-frame update
  update(A, B, alpha, now, info) {
    const dt = this.last ? Math.min(0.1, (now - this.last) / 1000) : 0.016;
    this.last = now;
    if (A) {
      for (const ra of A.robots) {
        const t = this.robots.get(ra.id); if (!t) continue;
        const rb = B && B.robots.find((x) => x.id === ra.id);
        const x = (rb ? ra.x + (rb.x - ra.x) * alpha : ra.x) / 1000, z = (rb ? ra.y + (rb.y - ra.y) * alpha : ra.y) / 1000;
        t.set(x, z, ra.d, ra.v, ra.st, ra.c, dt, now);
        t.fan.visible = this.layers.sensors; t.stopBar.visible = this.layers.sensors;
        t.pulse(now);
        const o = this.overlays.get(ra.id);
        const pts = [new THREE.Vector3(x, 0, z), ...(ra.p || []).map((c) => cellCenter(c))];
        const remote = ra.sv === "remote";
        o.route.visible = (this.layers.routes || remote) && pts.length > 1 && ra.st !== "idle" && ra.st !== "fault";
        if (o.route.visible) { this.setRoute(ra.id, pts); o.route.material.map.offset.x -= dt * (ra.v > 0 ? 2.4 : 0.3); }
        o.route.material.color.set(remote ? 0xa78bfa : this.accents[ra.id] || "#fff");
        o.route.material.opacity = remote ? 1 : 0.8;
        t.setFault(!!ra.f, now);
        if (ra.f && now - (this.pings.get(ra.id) || 0) > 1100) {   // sonar ping on the floor under a faulted trailer
          this.pings.set(ra.id, now);
          this.ring(new THREE.Vector3(x, 0, z), 0xff3b5c, now, 1.25);
        }
        t.group.visible = true;
        o.claims.visible = o.claimEdges.visible = this.layers.claims;
        this.setClaims(ra.id, ra.r || []);
        const standoff = ra.w && A.robots.find((q) => q.id === ra.w && q.w === ra.id);
        this.setLink(ra.id, ra.st === "waiting" && ra.w ? ra.w : null, standoff ? "standoff" : "hold");
        const el = t.tagEl;
        el.dataset.st = remote ? "remote" : ra.f ? "fault" : ra.st;
        el.querySelector(".st").textContent = remote ? "AI control" : ra.f ? `fault · ${ra.f.type}` : ra.st === "waiting" && ra.w ? `holding for ${ra.w}` : ra.st;
        el.style.display = this.layers.labels || t.selected ? "" : "none";
      }
      this.syncPallets(A.pallets || [], now);
      this.syncClosed(A.zones || []);
    }
    this.animateTechs(dt, now);
    this.animateTargets(now);
    this.animateEffects(dt, now);
    this.animateDocks(dt);
    this.animateCamera(dt);
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
    this.css.render(this.scene, this.camera);
  }

  syncPallets(cells, now) {
    const keys = new Set(cells.map((c) => c.join(",")));
    for (const [k, obj] of this.pallets) if (!keys.has(k)) { this.scene.remove(obj.group); this.pallets.delete(k); }
    for (const c of cells) {
      const k = c.join(",");
      if (this.pallets.has(k)) continue;
      const g = new THREE.Group();
      const wood = new THREE.MeshStandardMaterial({ color: 0x8a6a45, roughness: 0.9 });
      for (let i = 0; i < 3; i++) { const s = new THREE.Mesh(new THREE.BoxGeometry(0.78, 0.04, 0.12), wood); s.position.set(0, 0.1, -0.3 + i * 0.3); g.add(s); }
      for (let i = 0; i < 3; i++) { const b = new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.08, 0.1), wood); b.position.set(-0.32 + i * 0.32, 0.04, 0); g.add(b); }
      for (let i = 0; i < 4; i++) { const b = cargoBox(1.6); b.position.set(-0.17 + (i % 2) * 0.34, 0.26, -0.17 + Math.floor(i / 2) * 0.34); b.rotation.y = (i - 1.5) * 0.3; g.add(b); }
      const wrap = new THREE.Mesh(new THREE.BoxGeometry(0.76, 0.3, 0.76), new THREE.MeshPhysicalMaterial({ color: 0xffffff, transparent: true, opacity: 0.12, roughness: 0.1 }));
      wrap.position.y = 0.26; g.add(wrap);
      g.traverse((o) => { o.castShadow = true; });
      g.position.set(c[0] + 0.5, 3, c[1] + 0.5);
      g.rotation.y = 0.2;
      this.scene.add(g);
      this.pallets.set(k, { group: g, t0: now });
      this.effects.push({ kind: "fall", obj: g, t0: now, x: c[0] + 0.5, z: c[1] + 0.5 });
    }
  }

  syncClosed(zones) {
    const want = new Set(zones);
    for (const [z, g] of this.closed) if (!want.has(z)) { this.drop(g); this.closed.delete(z); }
    for (const z of want) {
      if (this.closed.has(z)) continue;
      const cells = this.map.zones[z] || [];
      const g = new THREE.Group();
      const mat = new THREE.MeshBasicMaterial({ color: 0xff3b5c, transparent: true, opacity: 0.18, depthWrite: false });
      for (const [x, y] of cells) { const p = new THREE.Mesh(new THREE.PlaneGeometry(0.98, 0.98), mat); p.rotation.x = -Math.PI / 2; p.position.set(x + 0.5, 0.02, y + 0.5); g.add(p); }
      if (cells.length) {
        const xs = cells.map((c) => c[0]), y = cells[0][1];
        for (const x of [Math.min(...xs), Math.max(...xs) + 1]) {
          const bar = new THREE.Mesh(new THREE.BoxGeometry(0.06, 0.06, 0.9), new THREE.MeshStandardMaterial({ map: hazard({ w: 128, h: 32, stripe: 10 }) }));
          bar.position.set(x, 0.7, y + 0.5); bar.rotation.y = 0; g.add(bar);
          for (const dz of [0.08, 0.92]) { const post = new THREE.Mesh(new THREE.CylinderGeometry(0.03, 0.05, 0.72, 10), new THREE.MeshStandardMaterial({ color: 0xff3b5c })); post.position.set(x, 0.36, y + dz); g.add(post); }
        }
        // the worker who closed it
        const person = new THREE.Group();
        const vest = new THREE.Mesh(new THREE.CapsuleGeometry(0.16, 0.5, 6, 12), new THREE.MeshStandardMaterial({ color: 0xff7a1a, roughness: 0.6 }));
        vest.position.y = 0.72; const head = new THREE.Mesh(new THREE.SphereGeometry(0.11, 16, 16), new THREE.MeshStandardMaterial({ color: 0xc58c6a }));
        head.position.y = 1.2; const hat = new THREE.Mesh(new THREE.SphereGeometry(0.12, 16, 8, 0, Math.PI * 2, 0, Math.PI / 2), new THREE.MeshStandardMaterial({ color: 0xf2c230 }));
        hat.position.y = 1.24; const legs = new THREE.Mesh(new THREE.BoxGeometry(0.22, 0.46, 0.14), new THREE.MeshStandardMaterial({ color: 0x1f2a3a }));
        legs.position.y = 0.23;
        person.add(vest, head, hat, legs); person.traverse((o) => { o.castShadow = true; });
        const mx = xs.reduce((a, b) => a + b, 0) / xs.length;
        person.position.set(mx + 0.5, 0, y + 0.5); g.add(person);
        const el = document.createElement("div"); el.className = "tag3d alert"; el.innerHTML = `<b>${z.replace("aisle_", "AISLE ").toUpperCase()} CLOSED</b><span>worker in aisle</span>`;
        const lab = new CSS2DObject(el); lab.position.set(mx + 0.5, 1.7, y + 0.5); g.add(lab);
      }
      this.scene.add(g); this.closed.set(z, g);
    }
  }

  // ------------------------------------------------------------ events -> effects
  event(e, frame, now) {
    const r = e.robot && this.robots.get(e.robot);
    const at = r ? r.group.position.clone() : null;
    switch (e.type) {
      case "pick": {
        const from = this.bayPos[e.slot]; if (!from || !r) break;
        this.flash(from.clone().setY(1.1), 0x4da3ff);
        const b = cargoBox(1); b.position.copy(from); this.scene.add(b);
        this.effects.push({ kind: "carry", obj: b, t0: now, dur: 750, from: from.clone(), to: () => r.group.position.clone().setY(0.45) });
        break;
      }
      case "dock_scan": {
        const d = this.docks[e.dock]; if (!d || !r) break;
        const n = Math.max(1, (e.actual || []).length);
        for (let i = 0; i < n; i++) {
          const b = cargoBox(1); b.position.copy(r.group.position).setY(0.4); this.scene.add(b);
          this.effects.push({ kind: "carry", obj: b, t0: now + i * 140, dur: 700, from: r.group.position.clone().setY(0.4), to: () => d.pos.clone(), done: () => this.stackOnCounter(e.dock, b) });
        }
        if (e.ok) { d.count += n; this.popCounter(e.dock); } else this.float(d.pos.clone().setY(1.9), "WRONG ITEM", "bad", now);
        break;
      }
      case "standoff": { if (at) this.float(at.clone().setY(1.25), `STANDOFF · ${e.robot} ⇄ ${e.with}`, "bad", now, "arbiter deciding…"); this.ring(at, 0xff3b5c, now); break; }
      case "yield": {
        if (!at) break;
        const why = { head_on: "head-on: higher id yields", head_on_retry: "still blocked: lower id yields", parked: "blocked by a parked robot", queue: "queued too long: re-route" }[e.rule] || e.rule;
        this.float(at.clone().setY(1.35), `ARBITER · ${e.robot} yields → ${e.to}`, "arbiter", now, why);
        this.ring(at, 0xf2c230, now);
        break;
      }
      case "contact": case "struck": { if (at) { this.ring(at, 0xff3b5c, now, 2.2); this.float(at.clone().setY(1.3), "COLLISION", "bad", now, `${e.robot}${e.with ? ` × ${e.with}` : ""}`); } break; }
      case "fault_alarm": { if (at) { this.ring(at, 0xff3b5c, now, 2); this.float(at.clone().setY(1.45), `${e.robot} · FAULT`, "bad", now, e.code); } break; }
      case "service_move": { if (at) this.float(at.clone().setY(1.45), `AI CONTROL · ${e.robot}`, "ai", now, `driving to c${e.cell[0]}_${e.cell[1]}`); break; }
      case "deployed": { if (at) { this.ring(at, 0x2dff8f, now, 1.6); this.float(at.clone().setY(1.4), `${e.robot} DEPLOYED`, "ok", now, "spare joins the fleet"); } break; }
      case "repaired": { if (at) { this.ring(at, 0x2dff8f, now, 2); this.float(at.clone().setY(1.45), `${e.robot} REPAIRED`, "ok", now, e.by ? `marked done by ${e.by}` : ""); } break; }
      case "scan_mismatch": { const p = this.bayPos[e.slot]; if (p) this.float(p.clone().setY(2.1), `LABEL MISMATCH · ${e.slot}`, "bad", now, "handed to a person"); break; }
      case "bin_mislabeled": { const p = this.bayPos[e.slot]; if (p) { this.flash(p.clone().setY(1.1), 0xff3b5c); this.float(p.clone().setY(2.1), `BIN ${e.slot} MISLABELED`, "warn", now); } break; }
      default: break;
    }
  }

  stackOnCounter(dock, box) {
    const d = this.docks[dock];
    this.scene.remove(box);
    const b = cargoBox(1.2);
    const i = d.stack.children.length;
    b.position.set(-0.18 + (i % 3) * 0.18, 0.1 + Math.floor(i / 3 % 2) * 0.2, ((i * 7) % 3 - 1) * 0.2);
    b.rotation.y = (i % 5) * 0.2;
    d.stack.add(b);
    while (d.stack.children.length > 6) d.stack.remove(d.stack.children[0]);   // the conveyor takes them away
  }

  popCounter(dock) { this.docks[dock].bump = 1; }

  setDockInfo(dock, carrier, count) {
    const d = this.docks[dock]; if (!d) return;
    if (carrier && carrier !== d.carrier) { d.carrier = carrier; this.drawBoard(d); }
    if (count != null && count > d.count) d.count = count;
  }

  animateDocks(dt) {
    for (const d of Object.values(this.docks)) {
      if (d.shown !== d.count) {
        if (d.shown < 0) d.shown = d.count;
        else d.shown += Math.sign(d.count - d.shown) * Math.max(1, Math.round(Math.abs(d.count - d.shown) * dt * 6));
        if (Math.abs(d.count - d.shown) < 1) d.shown = d.count;
        this.drawBoard(d);
      }
      d.bump = Math.max(0, d.bump - dt * 2.2);
      d.board.scale.setScalar(1 + 0.12 * Math.sin(Math.PI * d.bump));
    }
  }

  flash(pos, color) {
    this.bayHi.position.copy(pos); this.bayHi.material.color.setHex(color); this.bayHi.material.opacity = 0.35;
  }

  ring(pos, color, now, size = 1.1) {
    const m = new THREE.Mesh(new THREE.RingGeometry(0.3, 0.36, 48), new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.9, depthWrite: false, side: THREE.DoubleSide }));
    m.rotation.x = -Math.PI / 2; m.position.set(pos.x, 0.03, pos.z); this.scene.add(m);
    this.effects.push({ kind: "ring", obj: m, t0: now, dur: 1100, size });
  }

  float(pos, title, kind, now, sub = "") {
    const el = document.createElement("div");
    el.className = `tag3d float ${kind}`;
    el.innerHTML = `<b>${title}</b>${sub ? `<span>${sub}</span>` : ""}`;
    const anchor = document.createElement("div"); anchor.className = "anchor3d"; anchor.append(el);
    const o = new CSS2DObject(anchor); o.position.copy(pos); this.scene.add(o);
    this.effects.push({ kind: "float", obj: o, el, t0: now, dur: 3600, y0: pos.y });
  }

  animateEffects(dt, now) {
    this.bayHi.material.opacity = Math.max(0, this.bayHi.material.opacity - dt * 0.5);
    this.effects = this.effects.filter((f) => {
      const k = (now - f.t0) / (f.dur || 1000);
      if (k < 0) return true;
      if (f.kind === "carry") {
        const to = f.to(), e = easeInOut(Math.min(1, k));
        f.obj.position.lerpVectors(f.from, to, e); f.obj.position.y += Math.sin(Math.PI * Math.min(1, k)) * 0.45;
        f.obj.rotation.y += dt * 3;
        if (k >= 1) { f.done ? f.done() : this.scene.remove(f.obj); return false; }
        return true;
      }
      if (f.kind === "ring") {
        f.obj.scale.setScalar(1 + k * 3 * f.size); f.obj.material.opacity = Math.max(0, 0.9 * (1 - k));
        if (k >= 1) { this.scene.remove(f.obj); return false; } return true;
      }
      if (f.kind === "float") {
        f.obj.position.y = f.y0 + Math.min(1, k) * 0.35;
        f.el.style.opacity = k < 0.08 ? k / 0.08 : k > 0.8 ? Math.max(0, (1 - k) / 0.2) : 1;
        if (k >= 1) { this.scene.remove(f.obj); f.el.remove(); return false; } return true;
      }
      if (f.kind === "spark") {
        f.obj.position.addScaledVector(f.obj.userData.v, dt); f.obj.userData.v.y -= dt * 4;
        f.obj.material.opacity = 1 - k; f.obj.material.transparent = true;
        if (k >= 1) { this.scene.remove(f.obj); return false; } return true;
      }
      if (f.kind === "fall") {
        const t = Math.min(1, (now - f.t0) / 650), y = 3 * (1 - t * t);
        f.obj.position.y = Math.max(0, y);
        if (t >= 1) { this.ring(f.obj.position.clone(), 0xc07a35, now, 1.4); return false; } return true;
      }
      return false;
    });
  }

  // ------------------------------------------------------------ camera
  pick(cx, cy) {
    const rect = this.renderer.domElement.getBoundingClientRect();
    this.pointer.set(((cx - rect.left) / rect.width) * 2 - 1, -((cy - rect.top) / rect.height) * 2 + 1);
    this.ray.setFromCamera(this.pointer, this.camera);
    const hits = this.ray.intersectObjects([...this.robots.values()].map((t) => t.hit), false);
    return hits.length ? hits[0].object.userData.robot : null;
  }

  chasePose(t) {
    // behind and above the trailer, looking along its direction of travel (down the aisle, not over the racks)
    const target = t.group.position.clone().setY(0.35);
    const back = new THREE.Vector3(-Math.cos(t.yaw), 0, Math.sin(t.yaw));
    // stopped (working, faulted, being repaired): rise to a steeper view so people beside it don't block it
    const still = t.speed === 0;
    const pos = target.clone().addScaledVector(back, still ? 1.7 : 3.3).setY(still ? 4.1 : 2.75);
    // never put the camera inside the wall shelving: pull it in, and up, when a wall is behind the robot
    const lo = 1.2, hiX = this.W - 1.3, hiZ = this.H - 1.2;
    const cx = Math.min(hiX, Math.max(lo, pos.x)), cz = Math.min(hiZ, Math.max(lo, pos.z));
    const pulled = Math.hypot(pos.x - cx, pos.z - cz);
    pos.set(cx, pos.y + pulled * 0.9, cz);
    return { target, pos };
  }

  focus(id) {
    for (const t of this.robots.values()) { t.select(t.id === id); t.askPin.visible = t.id === id; }
    this.follow = id; this.chase = true;
    const t = this.robots.get(id); if (!t) return;
    this.fly = { t0: performance.now(), dur: 1200, fromPos: this.camera.position.clone(), fromTarget: this.controls.target.clone() };
    this.lastFollow = t.group.position.clone().setY(0.35);
  }

  overview() {
    for (const t of this.robots.values()) { t.select(false); t.askPin.visible = false; }
    this.follow = null; this.chase = false;
    this.fly = { t0: performance.now(), dur: 1100, fromPos: this.camera.position.clone(), fromTarget: this.controls.target.clone(), toPos: this.home.pos.clone(), toTarget: this.home.target.clone() };
  }

  animateCamera(dt) {
    const t = this.follow && this.robots.get(this.follow);
    const now = performance.now();
    if (this.fly) {
      const k = Math.min(1, (now - this.fly.t0) / this.fly.dur), e = easeInOut(k);
      const pose = t ? this.chasePose(t) : { target: this.fly.toTarget, pos: this.fly.toPos };
      this.controls.target.lerpVectors(this.fly.fromTarget, pose.target, e);
      this.camera.position.lerpVectors(this.fly.fromPos, pose.pos, e);
      if (k >= 1) { this.fly = null; if (t) this.lastFollow = pose.target.clone(); }
      return;
    }
    if (!t) return;
    if (this.chase) {  // ease toward the chase pose, so turns swing the camera smoothly
      const pose = this.chasePose(t), a = 1 - Math.exp(-dt * 2.2);
      this.controls.target.lerp(pose.target, 1 - Math.exp(-dt * 9));
      this.camera.position.lerp(pose.pos, a);
      this.lastFollow = pose.target.clone();
      return;
    }
    // free follow: the operator orbits; carry the camera along with the robot
    const target = t.group.position.clone().setY(0.35);
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
