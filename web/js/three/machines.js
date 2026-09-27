// Mining machines for the live pit, modelled in metres with +X forward, +Y up, origin on the ground.
// The scene scales each group into cell units. Geometry and most materials are shared between instances;
// only what a truck changes at run time (lights, load, status lamp) is per instance.
import * as THREE from "three";
import { RoundedBoxGeometry } from "three/addons/geometries/RoundedBoxGeometry.js";

const YELLOW = 0xf0b000, DARK = 0x2a2520, STEEL = 0x5f5a53, RUBBER = 0x141210, WHITE = 0xe9e3d6;

const mats = {};
function mat(key, make) { return mats[key] || (mats[key] = make()); }
const M = {
  paint: () => mat("paint", () => new THREE.MeshStandardMaterial({ color: YELLOW, roughness: .55, metalness: .25 })),
  paintWorn: () => mat("paintWorn", () => new THREE.MeshStandardMaterial({ color: 0xc99a1c, roughness: .8, metalness: .15 })),
  grime: () => mat("grime", () => new THREE.MeshStandardMaterial({ color: 0x9a7a48, roughness: .95, metalness: .1 })),   // road dust caked on the paint
  tow: () => mat("tow", () => new THREE.MeshStandardMaterial({ color: 0xc8261c, roughness: .5, metalness: .3 })),
  dark: () => mat("dark", () => new THREE.MeshStandardMaterial({ color: DARK, roughness: .7, metalness: .45 })),
  steel: () => mat("steel", () => new THREE.MeshStandardMaterial({ color: STEEL, roughness: .5, metalness: .7 })),
  chrome: () => mat("chrome", () => new THREE.MeshStandardMaterial({ color: 0xc9c2b4, roughness: .25, metalness: .95 })),
  rubber: () => mat("rubber", () => new THREE.MeshStandardMaterial({ color: RUBBER, roughness: .95, metalness: 0, flatShading: true })),
  rim: () => mat("rim", () => new THREE.MeshStandardMaterial({ color: 0x8a8173, roughness: .5, metalness: .6 })),
  glass: () => mat("glass", () => new THREE.MeshStandardMaterial({ color: 0x17130e, roughness: .08, metalness: .9, emissive: 0x2a1c08, emissiveIntensity: .35 })),
  white: () => mat("white", () => new THREE.MeshStandardMaterial({ color: WHITE, roughness: .5, metalness: .2 })),
  rail: () => mat("rail", () => new THREE.MeshStandardMaterial({ color: 0xffc629, roughness: .4, metalness: .3 })),
  concrete: () => mat("concrete", () => new THREE.MeshStandardMaterial({ color: 0x8c8274, roughness: .95 })),
  cladding: () => mat("cladding", () => new THREE.MeshStandardMaterial({ color: 0x6f6a61, roughness: .6, metalness: .55 })),
  belt: () => mat("belt", () => new THREE.MeshStandardMaterial({ color: 0x1b1916, roughness: .9 })),
  dirt: () => mat("dirt", () => new THREE.MeshStandardMaterial({ color: 0x7a5a3c, roughness: 1, flatShading: true })),
  lamp: () => mat("lamp", () => new THREE.MeshBasicMaterial({ color: 0xfff1c8 })),
  hit: () => mat("hit", () => new THREE.MeshBasicMaterial({ visible: false })),
};

const geo = {};
function g(key, make) { return geo[key] || (geo[key] = make()); }
const box = (w, h, d) => g(`b${w},${h},${d}`, () => new THREE.BoxGeometry(w, h, d));
const rbox = (w, h, d, r) => g(`r${w},${h},${d},${r}`, () => new RoundedBoxGeometry(w, h, d, 2, r));
const cyl = (rt, rb, h, s = 16) => g(`c${rt},${rb},${h},${s}`, () => new THREE.CylinderGeometry(rt, rb, h, s));
// many small static parts baked into one shared geometry: one draw call per material, however much detail
// parts: [geometry, x, y, z, rx, ry, rz]
const _m = new THREE.Matrix4(), _e = new THREE.Euler(), _p = new THREE.Vector3(), _s = new THREE.Vector3(1, 1, 1), _r = new THREE.Quaternion();
function merged(key, parts) {
  return g(key, () => {
    const pos = [], nrm = [], idx = [];
    for (const [src, x = 0, y = 0, z = 0, rx = 0, ry = 0, rz = 0] of parts) {
      const c = src.clone().applyMatrix4(_m.compose(_p.set(x, y, z), _r.setFromEuler(_e.set(rx, ry, rz)), _s));
      const p = c.attributes.position, n = c.attributes.normal, base = pos.length / 3;
      for (let i = 0; i < p.count; i++) { pos.push(p.getX(i), p.getY(i), p.getZ(i)); nrm.push(n.getX(i), n.getY(i), n.getZ(i)); }
      if (c.index) for (let i = 0; i < c.index.count; i++) idx.push(base + c.index.getX(i));
      else for (let i = 0; i < p.count; i++) idx.push(base + i);
      c.dispose();
    }
    const out = new THREE.BufferGeometry();
    out.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    out.setAttribute("normal", new THREE.Float32BufferAttribute(nrm, 3));
    out.setIndex(idx);
    return out;
  });
}

function mesh(geometry, material, x = 0, y = 0, z = 0, parent) {
  const m = new THREE.Mesh(geometry, material);
  m.position.set(x, y, z);
  m.castShadow = true; m.receiveShadow = true;
  if (parent) parent.add(m);
  return m;
}

// a cylinder stretched between two points (hydraulic rams, handrail posts, conveyor legs)
function strut(material, r, parent) {
  const m = new THREE.Mesh(cyl(r, r, 1, 10), material);
  m.castShadow = true; parent.add(m);
  return m;
}
const _a = new THREE.Vector3(), _b = new THREE.Vector3(), _up = new THREE.Vector3(0, 1, 0), _q = new THREE.Quaternion();
function stretch(m, a, b) {
  _a.subVectors(b, a); const len = _a.length() || 1e-3;
  m.position.addVectors(a, b).multiplyScalar(.5);
  m.quaternion.setFromUnitVectors(_up, _a.divideScalar(len));
  m.scale.set(1, len, 1);
}

function rail(parent, x0, x1, y, z, h = 1.0) {   // a yellow handrail along X
  mesh(box(Math.abs(x1 - x0), .07, .07), M.rail(), (x0 + x1) / 2, y + h, z, parent);
  mesh(box(Math.abs(x1 - x0), .05, .05), M.rail(), (x0 + x1) / 2, y + h / 2, z, parent);
  for (let x = Math.min(x0, x1); x <= Math.max(x0, x1) + 1e-6; x += Math.max(.8, Math.abs(x1 - x0) / 4)) mesh(box(.07, h, .07), M.rail(), x, y + h / 2, z, parent);
}
function railZ(parent, z0, z1, y, x, h = 1.0) {
  mesh(box(.07, .07, Math.abs(z1 - z0)), M.rail(), x, y + h, (z0 + z1) / 2, parent);
  for (let z = Math.min(z0, z1); z <= Math.max(z0, z1) + 1e-6; z += Math.max(.8, Math.abs(z1 - z0) / 4)) mesh(box(.07, h, .07), M.rail(), x, y + h / 2, z, parent);
}

function numberPlate(text, accent) {
  const c = document.createElement("canvas"); c.width = 256; c.height = 128;
  const x = c.getContext("2d");
  x.fillStyle = "#16120c"; x.fillRect(0, 0, 256, 128);
  x.strokeStyle = accent; x.lineWidth = 10; x.strokeRect(5, 5, 246, 118);
  x.fillStyle = accent; x.font = "900 84px Inter, 'Helvetica Neue', Arial, sans-serif";
  x.textAlign = "center"; x.textBaseline = "middle"; x.fillText(text, 128, 70);
  const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = 4;
  return t;
}

// a heaped load: a dome with rocky noise, coloured by material
function loadGeometry() {
  return g("load", () => {
    const s = new THREE.SphereGeometry(1, 22, 10, 0, Math.PI * 2, 0, Math.PI / 2);
    const p = s.attributes.position;
    for (let i = 0; i < p.count; i++) {
      const x = p.getX(i), y = p.getY(i), z = p.getZ(i);
      const n = Math.sin(x * 9.1 + z * 4.3) * Math.cos(z * 7.7 - x * 3.1) * .08 + Math.sin(x * 23 + z * 19) * .03;
      p.setXYZ(i, x * (1 + n * .5), y * (1 + n * 1.8), z * (1 + n * .5));
    }
    s.computeVertexNormals();
    return s;
  });
}

function wheel(r, w) {
  // four draw calls a wheel: tyre with its sidewall treads, rim, hub with its lugs, chrome hub caps on both faces
  const grp = new THREE.Group(), R = Math.PI / 2;
  const side = g(`tread${r}`, () => new THREE.TorusGeometry(r * .93, r * .09, 6, 26));
  mesh(merged(`tyre${r},${w}`, [[cyl(r, r, w, 26), 0, 0, 0, R], [side, 0, 0, w / 2 - .02], [side, 0, 0, -w / 2 + .02]]), M.rubber(), 0, 0, 0, grp);
  const rim = mesh(cyl(r * .56, r * .56, w + .06, 20), M.rim(), 0, 0, 0, grp); rim.rotation.x = R;
  const hub = [[cyl(r * .22, r * .26, w + .22, 12), 0, 0, 0, R]];
  for (let i = 0; i < 6; i++) { const a = i / 6 * Math.PI * 2; hub.push([box(.28, .28, w + .12), Math.cos(a) * r * .4, Math.sin(a) * r * .4, 0]); }   // lugs, so a turning wheel reads as turning
  const nuts = mesh(merged(`hub${r},${w}`, hub), M.dark(), 0, 0, 0, grp);
  const caps = mesh(merged(`cap${r},${w}`, [-1, 1].map((s) => [cyl(r * .3, r * .36, .14, 16), 0, 0, s * (w / 2 + .17), R])), M.chrome(), 0, 0, 0, grp);
  rim.castShadow = nuts.castShadow = caps.castShadow = false;
  return grp;
}

// ------------------------------------------------------------------ haul truck (Komatsu 930E class)
export class HaulTruck {
  constructor({ id = "", accent = "#f5b800" } = {}) {
    this.group = new THREE.Group();
    this.inner = new THREE.Group(); this.group.add(this.inner);
    const I = this.inner;
    this.R = 1.95;

    // chassis rails, rear axle housing, front suspension
    mesh(box(11.2, .9, 2.2), M.dark(), -.2, 1.9, 0, I);
    mesh(box(1.6, 1.3, 5.0), M.dark(), -3.0, 1.95, 0, I);
    mesh(box(1.4, 1.1, 5.4), M.dark(), 4.2, 2.1, 0, I);
    for (const z of [-2.2, 2.2]) mesh(cyl(.35, .35, 1.6, 10), M.chrome(), 4.2, 2.9, z, I);

    // wheels: single fronts, dual rears
    this.wheels = [];
    const add = (x, z, w) => { const wh = wheel(this.R, w); wh.position.set(x, this.R, z); I.add(wh); this.wheels.push(wh); };
    add(4.2, -3.25, 1.45); add(4.2, 3.25, 1.45);
    for (const s of [-1, 1]) { add(-3.0, s * 2.35, 1.4); add(-3.0, s * 3.85, 1.4); }

    // front: radiator house, upper deck, handrails, diagonal ladder, cab, grille
    mesh(rbox(2.6, 2.6, 3.0, .12), M.paint(), 5.7, 3.3, 0, I);
    const grille = mesh(box(.12, 1.9, 2.5), M.dark(), 7.02, 3.2, 0, I);
    for (let i = 0; i < 7; i++) mesh(box(.14, .06, 2.5), M.steel(), 7.06, 2.4 + i * .27, 0, I);
    mesh(box(3.2, .22, 8.4), M.paint(), 5.6, 4.72, 0, I);
    mesh(box(.3, .28, 8.4), M.paintWorn(), 7.15, 4.6, 0, I);
    rail(I, 4.2, 7.15, 4.83, 4.1); rail(I, 4.2, 7.15, 4.83, -4.1); railZ(I, -4.1, 4.1, 4.83, 7.15);
    const ladder = mesh(box(.12, .12, 5.6), M.rail(), 7.35, 2.5, 0, I); ladder.rotation.x = -.62;
    const ladder2 = mesh(box(.9, .06, 5.4), M.steel(), 7.3, 2.45, 0, I); ladder2.rotation.x = -.62;
    // cab (left side), empty: the truck drives itself
    mesh(rbox(2.5, 2.3, 2.5, .15), M.white(), 5.3, 6.0, -2.6, I);
    mesh(box(2.54, 1.0, 2.2), M.glass(), 5.3, 6.45, -2.6, I);
    mesh(box(2.2, 1.0, 2.54), M.glass(), 5.3, 6.45, -2.6, I);
    mesh(box(1.2, 1.9, 1.2), M.dark(), 5.6, 5.8, 2.6, I);                    // electrical cabinet, right
    // fuel tank and hydraulic tank on the sides, exhaust stacks
    mesh(rbox(2.6, 1.3, 1.2, .2), M.paint(), 1.6, 2.2, -2.2, I);
    mesh(rbox(2.2, 1.3, 1.1, .2), M.dark(), 1.6, 2.2, 2.2, I);
    for (const z of [-.9, .9]) mesh(cyl(.16, .16, 1.2, 10), M.dark(), 4.4, 5.3, z, I);
    // detail, baked per material (one draw call each): mirrors on arms, front bumper and tow points, rock ejectors
    // hanging between the rear duals, grab handles under the deck edge, a GPS/radio mast on the cab, caked road dust
    const arm = box(.1, 1.4, .1), mirror = box(.12, .8, .55), ejector = box(.16, 2.4, .1), grab = box(.5, .06, .06);
    mesh(merged("truckSteel", [[arm, 7.1, 5.6, 4.42, .4], [arm, 7.1, 5.6, -4.42, -.4], [cyl(.05, .05, 1.9, 6), 5.7, 8.0, -1.75],
      [ejector, -4.85, 2.3, 3.1, 0, 0, .39], [ejector, -4.85, 2.3, -3.1, 0, 0, .39]]), M.steel(), 0, 0, 0, I);
    mesh(merged("truckDark", [[mirror, 7.1, 6.55, 4.78], [mirror, 7.1, 6.55, -4.78], [box(.55, .8, 6.6), 6.95, 1.6, 0],
      [box(1.4, .6, 2.0), 6.1, 1.7, 0], [cyl(.025, .025, 1.6, 5), 5.7, 9.85, -1.75]]), M.dark(), 0, 0, 0, I);
    mesh(merged("truckTow", [-1, 1].map((s) => [box(.4, .42, .42), 7.42, 1.45, s * 2.3])), M.tow(), 0, 0, 0, I);
    mesh(merged("truckGrab", [4.7, 5.4, 6.1, 6.8].flatMap((x) => [[grab, x, 4.42, 4.27], [grab, x, 4.42, -4.27]])), M.rail(), 0, 0, 0, I);
    mesh(cyl(.3, .3, .12, 16), M.white(), 5.7, 9.0, -1.75, I);                   // GPS / radio puck
    mesh(merged("truckGrime", [[box(2.64, .6, 3.04), 5.7, 2.3, 0], [box(2.64, .5, 1.24), 1.6, 1.78, -2.2]]), M.grime(), 0, 0, 0, I);

    // accent stripe across the front deck so each truck is readable from the air
    this.accentMat = new THREE.MeshStandardMaterial({ color: new THREE.Color(accent), roughness: .45, metalness: .1, emissive: new THREE.Color(accent), emissiveIntensity: .25 });
    mesh(box(.16, .34, 8.1), this.accentMat, 7.25, 4.25, 0, I);

    // headlights, work lights, brake lights
    this.headMat = new THREE.MeshStandardMaterial({ color: 0xfff3d6, emissive: 0xfff0c8, emissiveIntensity: 2.2 });
    for (const z of [-3.6, -2.9, 2.9, 3.6]) mesh(box(.12, .32, .5), this.headMat, 7.3, 4.2, z, I);
    this.beams = [];
    const beamMat = new THREE.MeshBasicMaterial({ color: 0xffe2a6, transparent: true, opacity: .07, depthWrite: false, blending: THREE.AdditiveBlending, side: THREE.DoubleSide });
    for (const z of [-3.2, 3.2]) {
      const cone = new THREE.Mesh(g("beam", () => { const c = new THREE.ConeGeometry(3.4, 16, 20, 1, true); c.translate(0, -8, 0); return c; }), beamMat);
      cone.position.set(7.4, 4.1, z); cone.rotation.z = Math.PI / 2 + .12; I.add(cone); this.beams.push(cone);
    }
    this.brakeMat = new THREE.MeshStandardMaterial({ color: 0x4a0d0d, emissive: 0xff2a2a, emissiveIntensity: .15 });
    for (const z of [-3.3, 3.3]) mesh(box(.14, .35, .6), this.brakeMat, -6.35, 2.55, z, I);

    // dump body on a hinge at the rear of the frame; the canopy reaches over the cab
    this.bodyPivot = new THREE.Group(); this.bodyPivot.position.set(-6.2, 2.75, 0); I.add(this.bodyPivot);
    const B = this.bodyPivot;
    const floor = mesh(box(11.6, .35, 7.2), M.paint(), 5.6, .45, 0, B); floor.rotation.z = -.04;
    for (const s of [-1, 1]) {
      mesh(box(11.4, 2.7, .28), M.paint(), 5.7, 1.85, s * 3.7, B);
      mesh(box(11.6, .22, .5), M.paintWorn(), 5.7, 3.25, s * 3.72, B);                // top rail
      for (let x = 1; x < 11; x += 2.2) mesh(box(.28, 2.6, .22), M.paintWorn(), x, 1.8, s * 3.92, B);   // ribs
    }
    mesh(box(.4, 3.3, 7.6), M.paint(), 11.4, 2.0, 0, B);                 // front wall
    mesh(box(3.6, .3, 8.4), M.paint(), 13.0, 3.55, 0, B);               // canopy
    for (let x = 11.6; x < 14.6; x += .9) mesh(box(.2, .22, 8.4), M.paintWorn(), x, 3.35, 0, B);
    const tail = mesh(box(1.8, .3, 7.2), M.paint(), -.6, .9, 0, B); tail.rotation.z = .5;   // duck tail
    mesh(merged("bodyGrime", [-1, 1].map((s) => [box(11.44, .8, .08), 5.7, .92, s * 3.86])), M.grime(), 0, 0, 0, B);   // dust band low on the sides
    // number plates on both sides
    const plate = new THREE.MeshStandardMaterial({ map: numberPlate(id, accent), roughness: .5 });
    for (const s of [-1, 1]) {
      const p = new THREE.Mesh(g("plate", () => new THREE.PlaneGeometry(3.2, 1.6)), plate);
      p.position.set(6.2, 1.9, s * 4.06); p.rotation.y = s > 0 ? 0 : Math.PI; B.add(p);
    }
    const top = new THREE.Mesh(g("plateTop", () => new THREE.PlaneGeometry(3.6, 1.8)), plate);
    top.rotation.x = -Math.PI / 2; top.rotation.z = -Math.PI / 2; top.position.set(13.0, 3.72, 0); B.add(top);
    // the load, heaped inside the body
    this.loadMat = new THREE.MeshStandardMaterial({ color: 0x8c6a48, roughness: 1, flatShading: true });
    this.load = new THREE.Mesh(loadGeometry(), this.loadMat);
    this.load.position.set(5.6, .6, 0); this.load.castShadow = true; this.load.visible = false; B.add(this.load);
    this.body = B;

    // hoist rams: frame to body, restretched when the body tips
    this.rams = [-1.35, 1.35].map((z) => ({ z, m: strut(M.chrome(), .22, I), sleeve: strut(M.dark(), .3, I) }));

    // roof lidar on the canopy front corner, and the status lamp and beacon
    this.lidar = new THREE.Group(); this.lidar.position.set(7.2, 5.25, 3.4); I.add(this.lidar);
    mesh(cyl(.3, .34, .45, 16), M.dark(), 0, 0, 0, this.lidar);
    this.lidarHead = new THREE.Group(); this.lidar.add(this.lidarHead);
    const lens = mesh(box(.12, .2, .3), new THREE.MeshBasicMaterial({ color: 0xffc23a }), .3, .05, 0, this.lidarHead);
    mesh(cyl(.26, .26, .12, 16), M.steel(), 0, .28, 0, this.lidarHead);
    this.lidarLens = lens;
    this.statusMat = new THREE.MeshBasicMaterial({ color: 0x3ddc84 });
    mesh(g("lamp", () => new THREE.SphereGeometry(.28, 12, 8)), this.statusMat, 6.8, 7.4, -2.6, I);
    this.beaconMat = new THREE.MeshStandardMaterial({ color: 0x5a3a06, emissive: 0xffa000, emissiveIntensity: .2 });
    this.beacon = mesh(cyl(.22, .26, .4, 12), this.beaconMat, 4.4, 7.4, -2.6, I);
    this.beaconGlow = new THREE.Sprite(new THREE.SpriteMaterial({ color: 0xff3b2f, transparent: true, opacity: 0, depthWrite: false, blending: THREE.AdditiveBlending }));
    this.beaconGlow.scale.set(5, 5, 1); this.beaconGlow.position.set(4.4, 7.5, -2.6); I.add(this.beaconGlow);

    this.hit = new THREE.Mesh(box(16, 8, 9), M.hit()); this.hit.position.set(0, 4, 0); this.group.add(this.hit);
    this.tip = 0;
    this.setTip(0);
  }

  setLoad(fill, color) {
    const f = Math.max(0, Math.min(1, fill || 0));
    this.load.visible = f > .02;
    this.load.scale.set(5.1, .4 + 2.3 * f, 3.25);
    this.load.position.y = .55 + f * .4;
    if (color != null && this._loadColor !== color) { this._loadColor = color; this.loadMat.color.setHex(color); }
  }

  setTip(k) {
    this.tip = k;
    this.bodyPivot.rotation.z = k * .88;
    this.bodyPivot.updateMatrix();
    for (const r of this.rams) {
      const a = new THREE.Vector3(.6, 1.9, r.z);
      const b = new THREE.Vector3(6.4, .3, r.z).applyMatrix4(this.bodyPivot.matrix);
      stretch(r.m, a, b);
      const mid = a.clone().lerp(b, Math.min(.62, 2.8 / a.distanceTo(b)));
      stretch(r.sleeve, a, mid);
    }
  }

  roll(d) { const a = d / this.R; for (const w of this.wheels) w.rotation.z -= a; }

  setBrake(on) { this.brakeMat.emissiveIntensity = on ? 2.4 : .15; }

  setLights(on) {
    this.headMat.emissiveIntensity = on ? 2.2 : 0;
    for (const b of this.beams) b.visible = on;
  }

  setStatusColor(hex) { if (this._status !== hex) { this._status = hex; this.statusMat.color.setHex(hex); } }

  setBeacon(on, now = performance.now()) {
    if (!on) { this.beaconMat.emissive.setHex(0xffa000); this.beaconMat.emissiveIntensity = .2; this.beaconGlow.material.opacity = 0; return; }
    const k = (Math.sin(now / 90) + 1) / 2;
    this.beaconMat.emissive.setHex(0xff2a1a);
    this.beaconMat.emissiveIntensity = .6 + 3 * k;
    this.beaconGlow.material.opacity = .25 + .6 * k;
  }

  setDip(m) { this.inner.position.y = -(m || 0); }

  spinLidar(dt) { if (dt) this.lidarHead.rotation.y += dt * 11; }
}

// ------------------------------------------------------------------ hydraulic face shovel (PC7000 class)
const L1 = 8.6, L2 = 6.2, P0 = new THREE.Vector2(2.6, 3.4);   // boom and stick lengths, boom foot in the upper frame
const smooth = (t) => t * t * (3 - 2 * t);
const lerpAng = (a, b, t) => { let d = b - a; d = Math.atan2(Math.sin(d), Math.cos(d)); return a + d * t; };

export class Excavator {
  constructor({ id = "", label = "" } = {}) {
    this.id = id; this.label = label;
    this.group = new THREE.Group();
    const G = this.group;
    // undercarriage: two crawler tracks and the car body
    for (const s of [-1, 1]) {
      mesh(rbox(9.4, 2.1, 1.9, .5), M.dark(), 0, 1.05, s * 3.3, G);
      for (let x = -4; x <= 4; x += .5) mesh(box(.16, .16, 1.95), M.steel(), x, 2.1, s * 3.3, G);
      for (const x of [-4.1, 4.1]) { const sp = mesh(cyl(.95, .95, 1.95, 14), M.steel(), x, 1.05, s * 3.3, G); sp.rotation.x = Math.PI / 2; }
    }
    mesh(box(4.6, 1.3, 4.8), M.dark(), 0, 1.5, 0, G);
    mesh(cyl(2.4, 2.4, .5, 28), M.steel(), 0, 2.35, 0, G);            // slew ring

    // the upper works swing on the ring
    this.upper = new THREE.Group(); this.upper.position.y = 2.6; G.add(this.upper);
    const U = this.upper;
    mesh(rbox(8.2, 3.3, 6.6, .2), M.paint(), -1.3, 1.7, 0, U);           // machinery house
    mesh(rbox(2.0, 2.8, 6.8, .3), M.dark(), -5.9, 1.5, 0, U);            // counterweight
    mesh(box(2.2, .25, 6.8), M.paintWorn(), -5.9, 3.0, 0, U);
    for (let x = -4.5; x < 2.4; x += .7) mesh(box(.12, .5, 5.6), M.dark(), x, 3.5, 0, U);   // radiator louvres
    mesh(box(8.4, .18, 7.4), M.steel(), -1.3, 3.42, 0, U);
    rail(U, -5.4, 2.8, 3.5, 3.6); rail(U, -5.4, 2.8, 3.5, -3.6);
    for (const z of [-1.2, 1.2]) mesh(cyl(.2, .2, 1.6, 10), M.dark(), -3.8, 4.3, z, U);  // exhaust stacks
    // raised cab on the left front
    mesh(box(1.6, 2.0, 1.6), M.dark(), 2.3, 4.3, -2.4, U);
    mesh(rbox(2.8, 2.6, 2.6, .15), M.white(), 2.6, 6.4, -2.4, U);
    mesh(box(2.84, 1.1, 2.3), M.glass(), 2.6, 6.8, -2.4, U);
    mesh(box(2.5, 1.1, 2.64), M.glass(), 2.6, 6.8, -2.4, U);
    // work lights on the house
    for (const z of [-3, 0, 3]) mesh(box(.2, .35, .6), M.lamp(), 2.95, 3.9, z, U);
    // number board on the counterweight
    const plate = new THREE.MeshStandardMaterial({ map: numberPlate(label || id, "#f5b800"), roughness: .5 });
    const p = new THREE.Mesh(g("exPlate", () => new THREE.PlaneGeometry(3.6, 1.8)), plate);
    p.position.set(-6.92, 1.6, 0); p.rotation.y = -Math.PI / 2; U.add(p);

    // front: boom, stick, bucket and their rams, all laid out in the upper frame's XY plane
    this.boom = mesh(rbox(L1, 1.5, 1.9, .2), M.paint(), 0, 0, 0, U);
    this.stick = mesh(rbox(L2 + 1.0, 1.1, 1.5, .15), M.paint(), 0, 0, 0, U);
    this.bucket = new THREE.Group(); U.add(this.bucket);
    const K = this.bucket;
    mesh(box(.3, 2.6, 3.4), M.paint(), 0, -1.3, 0, K);                    // back
    mesh(box(2.9, .3, 3.4), M.paintWorn(), 1.45, -2.6, 0, K);              // floor
    for (const s of [-1, 1]) mesh(box(2.9, 2.6, .22), M.paint(), 1.45, -1.3, s * 1.7, K);
    mesh(box(1.2, .25, 3.4), M.paint(), .6, 0, 0, K);                      // roof lip
    for (let i = 0; i < 6; i++) mesh(box(.55, .22, .24), M.steel(), 3.1, -2.6, -1.4 + i * .56, K);   // teeth
    this.content = new THREE.Mesh(loadGeometry(), M.dirt());
    this.content.scale.set(1.3, .9, 1.55); this.content.position.set(1.4, -2.3, 0); this.content.visible = false; K.add(this.content);
    this.rams = {
      boom: [-1, 1].map((s) => ({ s, m: strut(M.chrome(), .2, U), sl: strut(M.dark(), .3, U) })),
      stick: { m: strut(M.chrome(), .18, U), sl: strut(M.dark(), .27, U) },
      bucket: { m: strut(M.chrome(), .16, U), sl: strut(M.dark(), .24, U) },
    };

    this.hit = new THREE.Mesh(box(16, 12, 10), M.hit()); this.hit.position.set(1, 6, 0); G.add(this.hit);
    this.t = Math.random() * 7;
    this.swing = 0;
    this.pose(this.frame(0, "dig", 0));
  }

  // the dig cycle: toe of the face → crowd up through it → swing → dump → swing back
  frame(t, mode, ang) {
    const S = mode === "load" ? ang : 1.35;              // no truck: cast the dig to a spoil pile beside the face
    const keys = [
      { t: 0.0, s: 0, x: 9.6, y: .5, b: .05 },
      { t: 2.2, s: 0, x: 12.4, y: 4.2, b: .9 },
      { t: 2.8, s: 0, x: 11.0, y: 5.2, b: 1.25 },
      { t: 4.6, s: S, x: 10.8, y: 7.4, b: 1.2 },
      { t: 5.7, s: S, x: 11.2, y: 7.2, b: -1.05 },
      { t: 7.6, s: 0, x: 9.6, y: .5, b: .05 },
    ];
    const T = keys[keys.length - 1].t;
    t = ((t % T) + T) % T;
    let i = 0; while (i < keys.length - 2 && t > keys[i + 1].t) i++;
    const a = keys[i], b = keys[i + 1], k = smooth((t - a.t) / (b.t - a.t));
    return { s: lerpAng(a.s, b.s, k), x: a.x + (b.x - a.x) * k, y: a.y + (b.y - a.y) * k, b: a.b + (b.b - a.b) * k, t, T,
             full: t > 2.0 && t < 5.3 };
  }

  pose(f) {
    // two-link IK for the bucket pin, elbow up
    const tx = f.x - P0.x, ty = f.y - P0.y;
    const d = Math.min(L1 + L2 - .05, Math.max(Math.abs(L1 - L2) + .05, Math.hypot(tx, ty)));
    const base = Math.atan2(ty, tx), c = (L1 * L1 + d * d - L2 * L2) / (2 * L1 * d);
    const a1 = base + Math.acos(Math.max(-1, Math.min(1, c)));
    const E = new THREE.Vector2(P0.x + Math.cos(a1) * L1, P0.y + Math.sin(a1) * L1);
    const Tp = new THREE.Vector2(P0.x + Math.cos(base) * d, P0.y + Math.sin(base) * d);
    const a2 = Math.atan2(Tp.y - E.y, Tp.x - E.x);
    this.boom.position.set((P0.x + E.x) / 2, (P0.y + E.y) / 2, 0); this.boom.rotation.z = a1;
    const sx = E.x - Math.cos(a2) * .5, sy = E.y - Math.sin(a2) * .5;         // stick overhangs the boom tip
    this.stick.position.set((sx + Tp.x) / 2 + Math.cos(a2) * .5, (sy + Tp.y) / 2 + Math.sin(a2) * .5, 0); this.stick.rotation.z = a2;
    this.bucket.position.set(Tp.x, Tp.y, 0); this.bucket.rotation.z = f.b;
    this.content.visible = f.full;
    const V = (x, y, z = 0) => new THREE.Vector3(x, y, z);
    const onBoom = (u, off) => V(P0.x + Math.cos(a1) * u - Math.sin(a1) * off, P0.y + Math.sin(a1) * u + Math.cos(a1) * off);
    const onStick = (u, off) => V(E.x + Math.cos(a2) * u - Math.sin(a2) * off, E.y + Math.sin(a2) * u + Math.cos(a2) * off);
    for (const r of this.rams.boom) {
      const A = V(1.4, 1.0, r.s * 1.05), Bp = onBoom(L1 * .45, -.8); Bp.z = r.s * 1.05;
      stretch(r.m, A, Bp); stretch(r.sl, A, A.clone().lerp(Bp, .55));
    }
    { const A = onBoom(L1 * .62, .9), Bp = onStick(-.4, .7); const r = this.rams.stick; stretch(r.m, A, Bp); stretch(r.sl, A, A.clone().lerp(Bp, .5)); }
    { const A = onStick(L2 * .25, .7);
      const Bp = V(Tp.x + Math.cos(f.b) * .1 - Math.sin(f.b) * -.5, Tp.y + Math.sin(f.b) * .1 + Math.cos(f.b) * -.5);
      const r = this.rams.bucket; stretch(r.m, A, Bp); stretch(r.sl, A, A.clone().lerp(Bp, .5)); }
  }

  update(dt, mode = "dig", ang = 0) {
    const prev = this.t;
    this.t += Math.min(dt, .1) * (mode === "load" ? 1.15 : .8);
    const f = this.frame(this.t, mode, ang), T = f.T;
    // the swing eases toward the cycle's target so a new truck or a mode change never snaps the house round
    this.swing = lerpAng(this.swing, f.s, Math.min(1, dt * 4));
    this.upper.rotation.y = this.swing;
    this.pose(f);
    const p = ((prev % T) + T) % T, n = f.t;
    const crossed = (at) => (p <= n ? p < at && n >= at : p < at || n >= at);
    return { dug: crossed(1.2), dumped: crossed(5.35) };
  }

  bucketWorld(v = new THREE.Vector3()) {
    this.bucket.updateWorldMatrix(true, false);
    return this.bucket.localToWorld(v.set(1.6, -2.0, 0));
  }
}

// ------------------------------------------------------------------ support equipment
export function makeDozer() {
  const group = new THREE.Group(), inner = new THREE.Group(); group.add(inner);
  for (const s of [-1, 1]) {
    mesh(rbox(5.4, 1.5, 1.0, .4), M.dark(), 0, .75, s * 1.9, inner);
    for (let x = -2.4; x <= 2.4; x += .4) mesh(box(.12, .12, 1.05), M.steel(), x, 1.5, s * 1.9, inner);
  }
  mesh(rbox(4.6, 1.8, 2.6, .2), M.paint(), -.3, 2.2, 0, inner);
  mesh(rbox(1.9, 1.9, 2.3, .15), M.paint(), -1.4, 3.9, 0, inner);
  mesh(box(1.94, 1.0, 2.1), M.glass(), -1.4, 4.1, 0, inner);
  mesh(box(1.7, 1.0, 2.34), M.glass(), -1.4, 4.1, 0, inner);
  mesh(cyl(.13, .13, 1.4, 8), M.dark(), .9, 3.6, -.6, inner);
  const blade = new THREE.Group(); blade.position.set(3.4, 0, 0); inner.add(blade);
  const bl = mesh(box(.5, 1.9, 5.6), M.paint(), 0, 1.1, 0, blade); bl.rotation.z = .12;
  mesh(box(.3, .25, 5.6), M.steel(), .2, .15, 0, blade);
  for (const s of [-1, 1]) { const arm = mesh(box(3.0, .35, .35), M.dark(), -1.5, .9, s * 1.6, blade); arm.rotation.z = -.2; }
  const ripper = mesh(box(.3, 1.6, .3), M.steel(), -3.3, .7, 0, inner); ripper.rotation.z = .3;
  // a small berm of pushed material ahead of the blade
  const berm = new THREE.Mesh(loadGeometry(), M.dirt()); berm.scale.set(1.4, .9, 3.0); berm.position.set(4.5, 0, 0); inner.add(berm);
  return {
    group,
    update(dt, t) {
      const k = Math.sin(t * .35);
      inner.position.x = k * 3.2;
      blade.position.y = Math.max(0, -Math.cos(t * .35)) * .5;
      berm.visible = Math.cos(t * .35) > -0.2;
      berm.scale.y = .5 + .5 * Math.max(0, Math.cos(t * .35));
    },
  };
}

export function makeDrillRig() {
  const group = new THREE.Group();
  for (const s of [-1, 1]) mesh(rbox(6.4, 1.2, .9, .35), M.dark(), 0, .6, s * 1.7, group);
  mesh(rbox(6.2, 2.0, 3.2, .2), M.paint(), -.4, 2.0, 0, group);
  mesh(rbox(1.8, 2.0, 1.8, .15), M.white(), 2.0, 4.0, -1.0, group);
  mesh(box(1.84, .9, 1.6), M.glass(), 2.0, 4.3, -1.0, group);
  const mast = new THREE.Group(); mast.position.set(3.2, 1.2, .6); group.add(mast);
  for (const [dx, dz] of [[-.35, -.35], [.35, -.35], [-.35, .35], [.35, .35]]) mesh(box(.14, 17, .14), M.paint(), dx, 8.5, dz, mast);
  for (let y = 1; y < 17; y += 1.4) {
    mesh(box(.84, .08, .08), M.paintWorn(), 0, y, -.35, mast); mesh(box(.84, .08, .08), M.paintWorn(), 0, y, .35, mast);
    mesh(box(.08, .08, .84), M.paintWorn(), -.35, y, 0, mast); mesh(box(.08, .08, .84), M.paintWorn(), .35, y, 0, mast);
  }
  const rod = mesh(cyl(.12, .12, 14, 8), M.chrome(), 0, 7, 0, mast);
  const head = mesh(box(.9, 1.0, .9), M.dark(), 0, 12, 0, mast);
  const beacon = mesh(cyl(.18, .2, .3, 10), new THREE.MeshStandardMaterial({ color: 0x5a3a06, emissive: 0xffa000, emissiveIntensity: 1 }), 0, 17.2, 0, mast);
  const feed = (k) => { head.position.y = 14 - k * 9; rod.position.y = 7 - k * 3; };
  const flash = (t) => { beacon.material.emissiveIntensity = .4 + 1.6 * ((Math.sin(t * 5) + 1) / 2); };
  return {
    group,
    bit: new THREE.Vector3(3.2, 0, .6),   // where the string meets the ground, rig-local
    // k: 0..1 down the hole, the string spinning as the head feeds it; null: tramming, the string pulled up
    bore(dt, k, t) {
      if (k == null) feed(Math.max(0, (14 - head.position.y) / 9 - dt * 2));
      else { rod.rotation.y += dt * 9; feed(k); }
      flash(t);
    },
    update(dt, t) {
      rod.rotation.y += dt * 6;
      feed((t * .08) % 1);
      flash(t);
    },
  };
}

export function makeLightTower() {
  const group = new THREE.Group();
  mesh(rbox(3.2, 1.2, 1.7, .15), M.paint(), 0, .95, 0, group);
  for (const s of [-1, 1]) { const w = mesh(cyl(.35, .35, .3, 12), M.rubber(), -.4, .35, s * .95, group); w.rotation.x = Math.PI / 2; }
  for (const [x, z] of [[1.5, 1.2], [1.5, -1.2], [-1.5, 1.2], [-1.5, -1.2]]) mesh(box(.12, .5, .12), M.steel(), x, .25, z, group);
  mesh(cyl(.12, .16, 9, 8), M.steel(), .6, 5.8, 0, group);
  const head = new THREE.Group(); head.position.set(.6, 10.3, 0); group.add(head);
  mesh(box(.25, 1.1, 2.6), M.dark(), 0, 0, 0, head);
  for (const [y, z] of [[.28, -.65], [.28, .65], [-.28, -.65], [-.28, .65]]) mesh(box(.1, .45, .55), M.lamp(), .14, y, z, head);
  const glow = new THREE.Sprite(new THREE.SpriteMaterial({ color: 0xffd9a0, transparent: true, opacity: .35, depthWrite: false, blending: THREE.AdditiveBlending }));
  glow.scale.set(7, 5, 1); glow.position.set(.6, 0, 0); head.add(glow);
  return { group };
}

export function makeServiceUte() {
  const group = new THREE.Group();
  for (const [x, z] of [[1.6, -.85], [1.6, .85], [-1.5, -.85], [-1.5, .85]]) {
    const w = mesh(cyl(.42, .42, .32, 14), M.rubber(), x, .42, z, group); w.rotation.x = Math.PI / 2;
  }
  mesh(rbox(5.2, .8, 1.9, .12), M.white(), 0, .95, 0, group);
  mesh(rbox(2.3, .9, 1.8, .15), M.white(), .7, 1.75, 0, group);
  mesh(box(2.34, .55, 1.7), M.glass(), .72, 1.85, 0, group);
  mesh(box(2.1, .55, 1.84), M.glass(), .72, 1.85, 0, group);
  mesh(box(2.0, .45, 1.8), M.dark(), -1.5, 1.55, 0, group);          // tray with toolboxes
  mesh(box(.8, .5, 1.7), M.steel(), -1.2, 1.9, 0, group);
  mesh(box(.3, .15, 1.9), M.dark(), 2.65, .75, 0, group);            // bull bar
  mesh(box(.08, .18, 1.5), M.lamp(), 2.62, 1.05, 0, group);
  const bar = new THREE.MeshStandardMaterial({ color: 0x5a3a06, emissive: 0xffa000, emissiveIntensity: .3 });
  const lb = mesh(box(.3, .16, 1.4), bar, .9, 2.28, 0, group);
  const whip = mesh(cyl(.02, .02, 3.2, 5), M.steel(), -2.3, 3.0, .8, group);
  const flag = mesh(box(.02, .35, .5), new THREE.MeshStandardMaterial({ color: 0xff6a00, emissive: 0xff5a00, emissiveIntensity: .4 }), -2.3, 4.45, 1.05, group);
  const glow = new THREE.Sprite(new THREE.SpriteMaterial({ color: 0xffa000, transparent: true, opacity: 0, depthWrite: false, blending: THREE.AdditiveBlending }));
  glow.scale.set(4, 4, 1); glow.position.set(.9, 2.4, 0); group.add(glow);
  return {
    group,
    setBeacon(on, now = performance.now()) {
      const k = on ? (Math.sin(now / 110) + 1) / 2 : 0;
      bar.emissiveIntensity = on ? .5 + 2.5 * k : .3;
      glow.material.opacity = on ? .15 + .5 * k : 0;
      flag.rotation.y = Math.sin(now / 240) * .3;
      whip.rotation.x = Math.sin(now / 300) * .02;
      lb.visible = true;
    },
  };
}

export function makeCrusher() {
  // primary gyratory crusher: dump pocket facing +X, crusher house, apron feeder and an inclined conveyor to the ROM pad
  const group = new THREE.Group();
  mesh(box(30, 1.2, 26), M.concrete(), -2, .6, 0, group);                        // pad
  for (const s of [-1, 1]) mesh(box(14, 5.5, 1.4), M.concrete(), 7, 2.75, s * 10, group);   // wing walls
  // hopper: an open steel funnel at the tipping edge
  const hopper = new THREE.Mesh(g("hopper", () => new THREE.CylinderGeometry(7.5, 3.2, 6, 4, 1, true)), new THREE.MeshStandardMaterial({ color: 0x5b5249, roughness: .7, metalness: .5, side: THREE.DoubleSide }));
  hopper.rotation.y = Math.PI / 4; hopper.position.set(7.5, 3.2, 0); hopper.castShadow = true; group.add(hopper);
  mesh(box(1.2, 1.4, 16), M.concrete(), 13.2, .7, 0, group);                     // tipping kerb
  const rom = new THREE.Mesh(loadGeometry(), M.dirt()); rom.scale.set(4.5, 1.6, 4.5); rom.position.set(7.5, 2.8, 0); group.add(rom);
  // crusher house
  mesh(box(12, 18, 16), M.cladding(), -6, 9, 0, group);
  for (let y = 2; y < 18; y += 1.1) mesh(box(12.1, .12, 16.1), M.dark(), -6, y, 0, group);  // cladding seams
  mesh(box(12.6, .6, 16.6), M.paint(), -6, 18.3, 0, group);                       // roof edge band
  mesh(box(6, 5, 6), M.cladding(), -6, 21, -3, group);                            // crane house
  for (let i = 0; i < 4; i++) mesh(box(.15, 1.2, 2.4), M.lamp(), .06, 12 - i * 3, -5 + i * 3.3, group);
  // rock breaker on a pedestal over the hopper
  const rb = new THREE.Group(); rb.position.set(2, 5.5, -8.5); group.add(rb);
  mesh(cyl(.8, 1.0, 2.5, 12), M.paint(), 0, 1.2, 0, rb);
  const arm = mesh(box(7, .6, .6), M.paint(), 3.2, 3.2, 1.5, rb); arm.rotation.set(0, -.6, -.25);
  // stairs and walkway up the house
  for (let i = 0; i < 12; i++) mesh(box(1.2, .12, 1.4), M.rail(), .4 + i * .02, 1.5 + i * 1.2, 8.8 - i * .9, group);
  rail(group, -12, 0, 12, 8.4);
  // conveyor leaving the back of the house, climbing toward the stockpile
  const conv = new THREE.Group(); conv.position.set(-12, 6, 2); conv.rotation.z = -.22; group.add(conv);
  mesh(box(46, 1.0, 2.2), M.belt(), -23, 0, 0, conv);
  mesh(box(46, .5, 2.8), M.paintWorn(), -23, -.7, 0, conv);
  rail(conv, -46, 0, 0, 1.4, .9);
  for (let x = -40; x <= -4; x += 9) { const leg = mesh(box(.5, 1, .5), M.steel(), x, 0, 0, conv); leg.scale.y = 6 + (-x) * .22; leg.position.y = -leg.scale.y / 2 - .6; }
  const stream = new THREE.Mesh(box(40, .35, 1.5), M.dirt()); stream.position.set(-24, .6, 0); conv.add(stream);
  return { group };
}
