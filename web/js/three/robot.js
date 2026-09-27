// The fleet trailer: yellow body, black bumper and hazard band, cargo tray, lidar, status ring, lights.
// Front faces +X in local space. One unit = one metre.
import * as THREE from "three";
import { RoundedBoxGeometry } from "three/addons/geometries/RoundedBoxGeometry.js";
import { hazard, cardboard, radialGlow } from "./textures.js";

export const STATUS_COLOR = {
  moving: 0x2dff8f, waiting: 0xffb020, held: 0xffb020, blocked: 0xff3b5c, estop: 0xff3b5c,
  picking: 0x4da3ff, dropping: 0x4da3ff, scanning: 0x4da3ff, idle: 0x8a94a1, fault: 0xff3b5c, standby: 0x55606d,
};
const SENSOR_RANGE = 0.8;

const YELLOW = new THREE.MeshStandardMaterial({ color: 0xf2c230, roughness: 0.42, metalness: 0.15 });
const BLACK = new THREE.MeshStandardMaterial({ color: 0x121315, roughness: 0.6, metalness: 0.2 });
const DECK = new THREE.MeshStandardMaterial({ color: 0x1b1d20, roughness: 0.75, metalness: 0.35 });
const RUBBER = new THREE.MeshStandardMaterial({ color: 0x0a0a0a, roughness: 0.9 });

let boxGeo = null;

export function cargoBox(scale = 1) {
  boxGeo ||= new RoundedBoxGeometry(0.2, 0.16, 0.2, 2, 0.012);
  const hue = 0.075 + Math.random() * 0.02, light = 0.36 + Math.random() * 0.08;
  const m = new THREE.MeshStandardMaterial({ map: cardboard(), color: new THREE.Color().setHSL(hue, 0.45, light * 1.55), roughness: 0.85 });
  const b = new THREE.Mesh(boxGeo, m);
  b.scale.setScalar(scale);
  b.castShadow = true; b.receiveShadow = true;
  return b;
}

// A floor technician: hi-vis vest, white hard hat, toolbox. Walks along a route; kneels to work.
export function makeWorker() {
  const g = new THREE.Group();
  const vest = new THREE.MeshStandardMaterial({ color: 0xc8f03c, roughness: 0.55, emissive: 0x2a3a00, emissiveIntensity: 0.4 });
  const dark = new THREE.MeshStandardMaterial({ color: 0x1d2633, roughness: 0.8 });
  const torso = new THREE.Mesh(new THREE.CapsuleGeometry(0.15, 0.36, 6, 12), vest); torso.position.y = 0.95;
  const stripe = new THREE.Mesh(new THREE.CylinderGeometry(0.158, 0.158, 0.035, 20), new THREE.MeshBasicMaterial({ color: 0xe8eef3 }));
  stripe.position.y = 0.9;
  const head = new THREE.Mesh(new THREE.SphereGeometry(0.1, 16, 16), new THREE.MeshStandardMaterial({ color: 0xb98563, roughness: 0.7 }));
  head.position.y = 1.36;
  const hat = new THREE.Mesh(new THREE.SphereGeometry(0.115, 18, 10, 0, Math.PI * 2, 0, Math.PI / 2), new THREE.MeshStandardMaterial({ color: 0xf4f6f8, roughness: 0.35 }));
  hat.position.y = 1.39;
  const brim = new THREE.Mesh(new THREE.CylinderGeometry(0.13, 0.13, 0.012, 20), hat.material); brim.position.y = 1.395;
  const legL = new THREE.Mesh(new THREE.BoxGeometry(0.09, 0.62, 0.11), dark), legR = legL.clone();
  legL.position.set(0, 0.31, -0.06); legR.position.set(0, 0.31, 0.06);
  const box = new THREE.Mesh(new THREE.BoxGeometry(0.24, 0.12, 0.1), new THREE.MeshStandardMaterial({ color: 0xd8262f, roughness: 0.4, metalness: 0.3 }));
  box.position.set(0.02, 0.55, 0.22);
  g.add(torso, stripe, head, hat, brim, legL, legR, box);
  g.traverse((o) => { o.castShadow = true; });
  g.userData = { legL, legR, torso, box };
  return g;
}

export class Trailer {
  constructor(id, accent) {
    this.id = id;
    this.accent = new THREE.Color(accent);
    this.group = new THREE.Group();
    this.group.name = id;
    this.body = new THREE.Group();
    this.group.add(this.body);
    this.yaw = 0; this.targetYaw = 0; this.cargoShown = 0; this.status = "idle"; this.speed = 0;

    const base = new THREE.Mesh(new RoundedBoxGeometry(0.56, 0.17, 0.52, 3, 0.045), YELLOW);
    base.position.y = 0.135; base.castShadow = true; base.receiveShadow = true;
    const skirt = new THREE.Mesh(new RoundedBoxGeometry(0.6, 0.07, 0.56, 3, 0.03), RUBBER);
    skirt.position.y = 0.045; skirt.castShadow = true;
    const bandTex = hazard({ w: 512, h: 48, stripe: 20 }).clone();
    bandTex.needsUpdate = true; bandTex.repeat.set(3, 1);
    const band = new THREE.Mesh(new THREE.BoxGeometry(0.566, 0.045, 0.526),
      [0, 1, 2, 3, 4, 5].map((i) => (i === 2 || i === 3 ? YELLOW : new THREE.MeshStandardMaterial({ map: bandTex, roughness: 0.5 }))));
    band.position.y = 0.105;
    const deck = new THREE.Mesh(new THREE.BoxGeometry(0.5, 0.02, 0.46), DECK);
    deck.position.y = 0.232; deck.receiveShadow = true;
    const railGeoX = new THREE.BoxGeometry(0.5, 0.05, 0.018), railGeoZ = new THREE.BoxGeometry(0.018, 0.05, 0.46);
    for (const [g, x, z] of [[railGeoX, 0, 0.221], [railGeoX, 0, -0.221], [railGeoZ, -0.241, 0]]) {
      const rail = new THREE.Mesh(g, YELLOW); rail.position.set(x, 0.265, z); rail.castShadow = true; this.body.add(rail);
      const cap = new THREE.Mesh(new THREE.BoxGeometry(g.parameters.width + 0.004, 0.012, g.parameters.depth + 0.004), BLACK);
      cap.position.set(x, 0.294, z); this.body.add(cap);
    }
    // lidar mast at the front, above the tray
    const mast = new THREE.Mesh(new THREE.BoxGeometry(0.05, 0.12, 0.08), BLACK);
    mast.position.set(0.23, 0.29, 0);
    const lidar = new THREE.Mesh(new THREE.CylinderGeometry(0.055, 0.06, 0.06, 28), BLACK);
    lidar.position.set(0.23, 0.38, 0); lidar.castShadow = true;
    this.lidarRing = new THREE.Mesh(new THREE.TorusGeometry(0.058, 0.006, 8, 40),
      new THREE.MeshBasicMaterial({ color: 0x00e5ff, transparent: true, opacity: 0.9 }));
    this.lidarRing.rotation.x = Math.PI / 2; this.lidarRing.position.set(0.23, 0.395, 0);
    this.lidarDot = new THREE.Mesh(new THREE.SphereGeometry(0.012, 10, 10), new THREE.MeshBasicMaterial({ color: 0x9ff6ff }));
    this.lidarDot.position.set(0.23 + 0.058, 0.395, 0);
    this.lidarSpin = new THREE.Group(); this.lidarSpin.position.set(0.23, 0, 0); this.lidarDot.position.set(0.058, 0.395, 0);
    this.lidarSpin.add(this.lidarDot);
    // status light ring around the top edge
    this.statusMat = new THREE.MeshBasicMaterial({ color: STATUS_COLOR.idle });
    const ring = new THREE.Mesh(new THREE.BoxGeometry(0.575, 0.012, 0.535), this.statusMat);
    ring.position.y = 0.222;
    // head and brake lights
    this.headMat = new THREE.MeshBasicMaterial({ color: 0xf4fbff });
    this.brakeMat = new THREE.MeshBasicMaterial({ color: 0x5a0d14 });
    for (const z of [-0.17, 0.17]) {
      const h = new THREE.Mesh(new THREE.BoxGeometry(0.012, 0.03, 0.07), this.headMat); h.position.set(0.282, 0.15, z); this.body.add(h);
      const b = new THREE.Mesh(new THREE.BoxGeometry(0.012, 0.03, 0.07), this.brakeMat); b.position.set(-0.282, 0.15, z); this.body.add(b);
    }
    // accent tag on the rear so robots can be told apart from above
    const tag = new THREE.Mesh(new THREE.BoxGeometry(0.02, 0.05, 0.3), new THREE.MeshBasicMaterial({ color: this.accent }));
    tag.position.set(-0.252, 0.265, 0);
    this.body.add(base, skirt, band, deck, mast, lidar, this.lidarRing, this.lidarSpin, ring, tag);

    // hazard beacon: only lit while the trailer is faulted
    this.beaconMat = new THREE.MeshBasicMaterial({ color: 0xff8a1a, transparent: true, opacity: 0.95 });
    this.beacon = new THREE.Group();
    const dome = new THREE.Mesh(new THREE.CylinderGeometry(0.035, 0.045, 0.07, 16), this.beaconMat);
    const glow = new THREE.Sprite(new THREE.SpriteMaterial({ map: radialGlow("255,120,40"), transparent: true, depthWrite: false, opacity: 0.9 }));
    glow.scale.setScalar(0.5); this.beaconGlow = glow;
    this.beacon.add(dome, glow); this.beacon.position.set(-0.19, 0.335, 0.17); this.beacon.visible = false;
    this.body.add(this.beacon);

    // cargo on the tray
    this.cargo = [];
    const slots = [[-0.1, 0.33, -0.1], [-0.1, 0.33, 0.1], [0.1, 0.33, 0], [-0.1, 0.49, 0]];
    for (const [x, y, z] of slots) {
      const b = cargoBox(1); b.position.set(x, y, z); b.rotation.y = (Math.random() - 0.5) * 0.12; b.visible = false; b.scale.setScalar(0.001);
      this.cargo.push(b); this.body.add(b);
    }

    // sensor fan on the floor, and a soft headlight pool
    this.fanMat = new THREE.MeshBasicMaterial({ color: 0x00e5ff, transparent: true, opacity: 0.12, depthWrite: false, side: THREE.DoubleSide });
    const fan = new THREE.Mesh(new THREE.CircleGeometry(SENSOR_RANGE + 0.3, 36, -0.42, 0.84), this.fanMat);
    fan.rotation.x = -Math.PI / 2; fan.position.set(0, 0.012, 0);
    this.fan = fan; this.group.add(fan);
    this.stopMat = new THREE.MeshBasicMaterial({ color: 0xff3b5c, transparent: true, opacity: 0.0, depthWrite: false });
    this.stopBar = new THREE.Mesh(new THREE.PlaneGeometry(0.04, 0.5), this.stopMat);
    this.stopBar.rotation.x = -Math.PI / 2; this.stopBar.position.y = 0.014;
    this.group.add(this.stopBar);
    const pool = new THREE.Mesh(new THREE.PlaneGeometry(1.4, 1.4),
      new THREE.MeshBasicMaterial({ map: radialGlow("200,240,255"), transparent: true, opacity: 0.12, depthWrite: false }));
    pool.rotation.x = -Math.PI / 2; pool.position.set(0.75, 0.011, 0);
    this.body.add(pool);

    // selection ring and hover glow
    this.selRing = new THREE.Mesh(new THREE.RingGeometry(0.46, 0.5, 48),
      new THREE.MeshBasicMaterial({ color: 0xf2c230, transparent: true, opacity: 0, depthWrite: false }));
    this.selRing.rotation.x = -Math.PI / 2; this.selRing.position.y = 0.013;
    this.group.add(this.selRing);
    // generous invisible hit box for taps
    this.hit = new THREE.Mesh(new THREE.BoxGeometry(0.9, 0.9, 0.9), new THREE.MeshBasicMaterial({ visible: false }));
    this.hit.position.y = 0.3; this.hit.userData.robot = id;
    this.group.add(this.hit);
    this.group.traverse((o) => { o.userData.robot = id; });
  }

  // x, z in metres; dir E/W/S/N; v in mm/tick
  set(x, z, dir, v, status, carry, dt, now) {
    this.group.position.set(x, 0, z);
    const yaw = { E: 0, N: Math.PI / 2, W: Math.PI, S: -Math.PI / 2 }[dir] ?? this.targetYaw;
    this.targetYaw = yaw;
    let d = this.targetYaw - this.yaw;
    d = Math.atan2(Math.sin(d), Math.cos(d));
    this.yaw += d * Math.min(1, dt * 9);
    this.body.rotation.y = this.yaw;
    this.fan.rotation.z = this.yaw;
    this.speed = v;
    if (status !== this.status) { this.status = status; this.statusMat.color.setHex(STATUS_COLOR[status] ?? STATUS_COLOR.idle); }
    const mps = (v * 10) / 1000;
    this.lidarSpin.rotation.y += dt * (status === "estop" ? 0 : 14);
    const braking = status === "waiting" || status === "blocked" || status === "estop" || status === "held";
    this.brakeMat.color.setHex(braking ? 0xff2d45 : 0x5a0d14);
    // stopping distance (+10 cm clearance) against the 0.8 m sensor: the fan turns red when it can't stop in range
    let stop = 0, u = v; while (u > 0) { u = Math.max(u - 10, 0); stop += u; }
    stop = stop / 1000 + 0.1;
    const unsafe = mps > 0.05 && stop > SENSOR_RANGE;
    this.fanMat.color.setHex(unsafe ? 0xff3b5c : 0x00e5ff);
    this.fanMat.opacity = status === "idle" ? 0.05 : unsafe ? 0.22 + 0.08 * Math.sin(now / 90) : 0.1;
    this.stopBar.position.set(Math.cos(this.yaw) * (0.3 + stop), 0.014, -Math.sin(this.yaw) * (0.3 + stop));
    this.stopBar.rotation.z = this.yaw;
    this.stopMat.opacity = mps > 0.05 ? 0.75 : 0;
    this.stopMat.color.setHex(unsafe ? 0xff3b5c : 0xf2c230);
    // cargo: pop boxes in and out
    const n = Math.min(carry, this.cargo.length);
    this.cargo.forEach((b, i) => {
      const want = i < n ? 1 : 0.001;
      b.visible = true;
      b.scale.setScalar(b.scale.x + (want - b.scale.x) * Math.min(1, dt * 10));
      if (b.scale.x < 0.01) b.visible = false;
    });
  }

  setFault(on, now) {
    this.beacon.visible = !!on;
    if (on) {
      const k = (Math.sin(now / 110) + 1) / 2;
      this.beaconMat.opacity = 0.55 + 0.45 * k;
      this.beaconGlow.material.opacity = 0.25 + 0.75 * k;
      this.beaconGlow.scale.setScalar(0.35 + 0.35 * k);
    }
  }

  select(on, now) {
    this.selected = on;
    this.selRing.material.opacity = on ? 0.85 : 0;
  }

  pulse(now) {
    if (!this.selected) return;
    const k = (Math.sin(now / 260) + 1) / 2;
    this.selRing.scale.setScalar(1 + k * 0.12);
    this.selRing.material.opacity = 0.5 + 0.4 * (1 - k);
  }
}
