// Procedural textures (canvas): concrete, hazard stripes, cardboard, painted floor text, chevrons.
import * as THREE from "three";

const cache = new Map();

function canvas(w, h) { const c = document.createElement("canvas"); c.width = w; c.height = h; return [c, c.getContext("2d")]; }

function rng(seed) { let s = seed >>> 0; return () => ((s = (s * 1664525 + 1013904223) >>> 0) / 4294967296); }

function tex(c, { repeat = [1, 1], srgb = true, aniso = 8 } = {}) {
  const t = new THREE.CanvasTexture(c);
  t.wrapS = t.wrapT = THREE.RepeatWrapping;
  t.repeat.set(repeat[0], repeat[1]);
  t.anisotropy = aniso;
  if (srgb) t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

export function concrete() {
  if (cache.has("concrete")) return cache.get("concrete");
  const [c, g] = canvas(1024, 1024), r = rng(7);
  g.fillStyle = "#1a1d21"; g.fillRect(0, 0, 1024, 1024);
  for (let i = 0; i < 26000; i++) {          // aggregate speckle
    const v = 22 + r() * 22, a = 0.05 + r() * 0.12;
    g.fillStyle = `rgba(${v + 6},${v + 8},${v + 12},${a})`;
    g.fillRect(r() * 1024, r() * 1024, 1 + r() * 2, 1 + r() * 2);
  }
  for (let i = 0; i < 40; i++) {              // soft tyre-worn patches
    const x = r() * 1024, y = r() * 1024, rad = 60 + r() * 180, gr = g.createRadialGradient(x, y, 0, x, y, rad);
    gr.addColorStop(0, "rgba(255,255,255,0.025)"); gr.addColorStop(1, "rgba(255,255,255,0)");
    g.fillStyle = gr; g.fillRect(x - rad, y - rad, rad * 2, rad * 2);
  }
  g.strokeStyle = "rgba(0,0,0,0.55)"; g.lineWidth = 3;          // slab joints every 4 m (texture = 4 m)
  g.beginPath(); g.moveTo(0, 1); g.lineTo(1024, 1); g.moveTo(1, 0); g.lineTo(1, 1024); g.stroke();
  const t = tex(c, { repeat: [6, 3.25] });
  cache.set("concrete", t);
  return t;
}

export function hazard({ w = 256, h = 64, stripe = 22 } = {}) {
  const key = `hazard${w}x${h}`;
  if (cache.has(key)) return cache.get(key);
  const [c, g] = canvas(w, h);
  g.fillStyle = "#f2c230"; g.fillRect(0, 0, w, h);
  g.fillStyle = "#0d0d0d";
  for (let x = -h; x < w + h; x += stripe * 2) {
    g.beginPath(); g.moveTo(x, h); g.lineTo(x + stripe, h); g.lineTo(x + stripe + h, 0); g.lineTo(x + h, 0); g.closePath(); g.fill();
  }
  const t = tex(c);
  cache.set(key, t);
  return t;
}

export function cardboard() {
  if (cache.has("cardboard")) return cache.get("cardboard");
  const [c, g] = canvas(256, 256), r = rng(3);
  g.fillStyle = "#b07a45"; g.fillRect(0, 0, 256, 256);
  for (let i = 0; i < 2600; i++) { g.fillStyle = `rgba(${90 + r() * 60},${55 + r() * 40},${25 + r() * 25},${0.08 + r() * 0.1})`; g.fillRect(r() * 256, r() * 256, 1 + r() * 3, 1); }
  g.fillStyle = "rgba(210,170,120,0.55)"; g.fillRect(112, 0, 32, 256);           // packing tape
  g.fillStyle = "rgba(255,255,255,0.08)"; g.fillRect(112, 0, 3, 256); g.fillRect(141, 0, 3, 256);
  g.fillStyle = "rgba(40,25,10,0.35)"; g.fillRect(24, 26, 60, 34);                 // shipping label
  g.fillStyle = "rgba(245,240,230,0.85)"; g.fillRect(26, 28, 56, 30);
  g.fillStyle = "rgba(20,20,20,0.7)"; for (let i = 0; i < 9; i++) g.fillRect(30 + i * 5, 34, 2 + (i % 3), 14);
  const t = tex(c);
  cache.set("cardboard", t);
  return t;
}

// White floor paint: letters with slight wear.
export function paintText(text, { size = 120, color = "rgba(235,240,245,0.92)", pad = 24, font = "800" } = {}) {
  const key = `paint:${text}:${size}:${color}`;
  if (cache.has(key)) return cache.get(key);
  const [m] = canvas(8, 8), mg = m.getContext("2d");
  mg.font = `${font} ${size}px Inter, "Helvetica Neue", Arial, sans-serif`;
  const w = Math.ceil(mg.measureText(text).width) + pad * 2, h = Math.ceil(size * 1.25) + pad;
  const [c, g] = canvas(w, h), r = rng(text.length * 31 + size);
  g.font = mg.font; g.textAlign = "center"; g.textBaseline = "middle"; g.fillStyle = color;
  g.fillText(text, w / 2, h / 2 + size * 0.04);
  g.globalCompositeOperation = "destination-out";                                  // scuffs
  for (let i = 0; i < w * h / 180; i++) { g.fillStyle = `rgba(0,0,0,${0.15 + r() * 0.35})`; g.fillRect(r() * w, r() * h, 1 + r() * 3, 1 + r() * 2); }
  const t = tex(c, { aniso: 16 });
  t.wrapS = t.wrapT = THREE.ClampToEdgeWrapping;
  const out = { texture: t, aspect: w / h };
  cache.set(key, out);
  return out;
}

// Label plate (dark with white text), e.g. rack bay numbers.
export function plate(text, { bg = "#0c0e11", fg = "#f2f4f7", w = 128, h = 64, size = 38, accent = null } = {}) {
  const key = `plate:${text}:${bg}:${fg}:${accent}`;
  if (cache.has(key)) return cache.get(key);
  const [c, g] = canvas(w, h);
  g.fillStyle = bg; g.fillRect(0, 0, w, h);
  if (accent) { g.fillStyle = accent; g.fillRect(0, 0, w, 7); }
  g.fillStyle = fg; g.font = `800 ${size}px Inter, Arial, sans-serif`; g.textAlign = "center"; g.textBaseline = "middle";
  g.fillText(text, w / 2, h / 2 + (accent ? 4 : 1));
  const t = tex(c, { aniso: 8 });
  t.wrapS = t.wrapT = THREE.ClampToEdgeWrapping;
  cache.set(key, t);
  return t;
}

// Chevrons for route ribbons; scrolled along the ribbon to show direction of travel.
export function chevrons(color) {
  const key = `chev:${color}`;
  if (cache.has(key)) return cache.get(key);
  const [c, g] = canvas(64, 64);
  g.clearRect(0, 0, 64, 64);
  g.strokeStyle = color; g.lineWidth = 9; g.lineCap = "round"; g.lineJoin = "round";
  g.beginPath(); g.moveTo(18, 12); g.lineTo(40, 32); g.lineTo(18, 52); g.stroke();
  const t = tex(c, { srgb: true });
  t.wrapT = THREE.ClampToEdgeWrapping;
  cache.set(key, t);
  return t;
}

export function radialGlow(color = "255,255,255") {
  const key = `glow:${color}`;
  if (cache.has(key)) return cache.get(key);
  const [c, g] = canvas(128, 128), gr = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  gr.addColorStop(0, `rgba(${color},0.9)`); gr.addColorStop(0.35, `rgba(${color},0.35)`); gr.addColorStop(1, `rgba(${color},0)`);
  g.fillStyle = gr; g.fillRect(0, 0, 128, 128);
  const t = tex(c);
  t.wrapS = t.wrapT = THREE.ClampToEdgeWrapping;
  cache.set(key, t);
  return t;
}

// ---------------------------------------------------------------- the mine

// Fine grain for the terrain (multiplied over vertex colours): gravel, cracks, a little blotching.
export function grain() {
  if (cache.has("grain")) return cache.get("grain");
  const [c, g] = canvas(512, 512), r = rng(11);
  g.fillStyle = "#bdbdbd"; g.fillRect(0, 0, 512, 512);
  for (let i = 0; i < 90; i++) {                // blotches
    const x = r() * 512, y = r() * 512, rad = 30 + r() * 90, gr = g.createRadialGradient(x, y, 0, x, y, rad);
    const v = r() < .5 ? "255,255,255" : "0,0,0";
    gr.addColorStop(0, `rgba(${v},0.07)`); gr.addColorStop(1, `rgba(${v},0)`);
    g.fillStyle = gr; g.fillRect(x - rad, y - rad, rad * 2, rad * 2);
  }
  for (let i = 0; i < 16000; i++) {             // gravel
    const v = 120 + r() * 135;
    g.fillStyle = `rgba(${v},${v},${v},${0.18 + r() * 0.3})`;
    const s = 1 + r() * 2.2; g.fillRect(r() * 512, r() * 512, s, s);
  }
  g.strokeStyle = "rgba(0,0,0,.12)"; g.lineWidth = 1;   // hairline cracks
  for (let i = 0; i < 40; i++) {
    let x = r() * 512, y = r() * 512; g.beginPath(); g.moveTo(x, y);
    for (let k = 0; k < 6; k++) { x += (r() - .5) * 40; y += (r() - .5) * 40; g.lineTo(x, y); }
    g.stroke();
  }
  const t = tex(c, { repeat: [1, 1], srgb: false, aniso: 8 });
  cache.set("grain", t);
  return t;
}

// A haul road seen from above: compacted dirt, two pairs of tyre ruts running along +u, soft edges.
export function haulRoad() {
  if (cache.has("haulRoad")) return cache.get("haulRoad");
  const [c, g] = canvas(256, 256), r = rng(5);
  g.fillStyle = "#b99467"; g.fillRect(0, 0, 256, 256);
  for (let i = 0; i < 5000; i++) {
    const v = r();
    g.fillStyle = v < .5 ? `rgba(90,62,36,${.06 + r() * .1})` : `rgba(235,210,170,${.05 + r() * .09})`;
    g.fillRect(r() * 256, r() * 256, 1 + r() * 3, 1 + r() * 2);
  }
  for (const y0 of [58, 92, 164, 198]) {        // ruts: darker, slightly compacted, with tread chatter
    const gr = g.createLinearGradient(0, y0 - 12, 0, y0 + 12);
    gr.addColorStop(0, "rgba(70,48,28,0)"); gr.addColorStop(.5, "rgba(70,48,28,.28)"); gr.addColorStop(1, "rgba(70,48,28,0)");
    g.fillStyle = gr; g.fillRect(0, y0 - 12, 256, 24);
    g.fillStyle = "rgba(40,26,14,.18)";
    for (let x = 0; x < 256; x += 6) g.fillRect(x, y0 - 7 + (r() - .5) * 2, 3, 14);
  }
  const t = tex(c, { srgb: true, aniso: 16 });
  cache.set("haulRoad", t);
  return t;
}

// Soft round sprite for dust, smoke and lidar returns.
export function softDot() {
  if (cache.has("softDot")) return cache.get("softDot");
  const [c, g] = canvas(64, 64), gr = g.createRadialGradient(32, 32, 0, 32, 32, 32);
  gr.addColorStop(0, "rgba(255,255,255,1)"); gr.addColorStop(.4, "rgba(255,255,255,.55)"); gr.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = gr; g.fillRect(0, 0, 64, 64);
  const t = tex(c, { srgb: false });
  t.wrapS = t.wrapT = THREE.ClampToEdgeWrapping;
  cache.set("softDot", t);
  return t;
}

// A puffy dust cloud sprite: lumpy, not a perfect disc.
export function dustPuff() {
  if (cache.has("dustPuff")) return cache.get("dustPuff");
  const [c, g] = canvas(128, 128), r = rng(23);
  for (let i = 0; i < 14; i++) {
    const x = 64 + (r() - .5) * 50, y = 64 + (r() - .5) * 50, rad = 18 + r() * 26, gr = g.createRadialGradient(x, y, 0, x, y, rad);
    gr.addColorStop(0, "rgba(255,255,255,.34)"); gr.addColorStop(1, "rgba(255,255,255,0)");
    g.fillStyle = gr; g.fillRect(0, 0, 128, 128);
  }
  const t = tex(c, { srgb: false });
  t.wrapS = t.wrapT = THREE.ClampToEdgeWrapping;
  cache.set("dustPuff", t);
  return t;
}

// Dusk sky: black overhead, a hot amber band at the horizon.
export function duskSky() {
  if (cache.has("duskSky")) return cache.get("duskSky");
  const [c, g] = canvas(16, 512);
  const gr = g.createLinearGradient(0, 0, 0, 512);
  gr.addColorStop(0, "#050403"); gr.addColorStop(.45, "#140d07"); gr.addColorStop(.62, "#3a220f");
  gr.addColorStop(.72, "#7a4518"); gr.addColorStop(.78, "#b06a24"); gr.addColorStop(.83, "#5a3a22"); gr.addColorStop(1, "#1a120b");
  g.fillStyle = gr; g.fillRect(0, 0, 16, 512);
  const t = tex(c, { srgb: true });
  t.wrapS = t.wrapT = THREE.ClampToEdgeWrapping;
  cache.set("duskSky", t);
  return t;
}

// Afternoon sky: a clear, hazy desert sky, deeper overhead and washed out to warm dust at the horizon.
export function afternoonSky() {
  if (cache.has("afternoonSky")) return cache.get("afternoonSky");
  const [c, g] = canvas(16, 512);
  const gr = g.createLinearGradient(0, 0, 0, 512);
  gr.addColorStop(0, "#5f8fc4"); gr.addColorStop(.28, "#86acd3"); gr.addColorStop(.44, "#b7cbdc");
  gr.addColorStop(.5, "#e3dccb"); gr.addColorStop(.54, "#efdcbc"); gr.addColorStop(.6, "#d8bf96"); gr.addColorStop(1, "#b89c74");
  g.fillStyle = gr; g.fillRect(0, 0, 16, 512);
  const t = tex(c, { srgb: true });
  t.wrapS = t.wrapT = THREE.ClampToEdgeWrapping;
  cache.set("afternoonSky", t);
  return t;
}

// A light, compacted haul road: pale crushed rock, dual-tyre ruts, a graded crown.
export function lightRoad() {
  if (cache.has("lightRoad")) return cache.get("lightRoad");
  const [c, g] = canvas(256, 256), r = rng(9);
  g.fillStyle = "#e2cda6"; g.fillRect(0, 0, 256, 256);
  for (let i = 0; i < 7000; i++) {
    const v = r();
    g.fillStyle = v < .45 ? `rgba(150,118,80,${.05 + r() * .1})` : v < .9 ? `rgba(250,236,210,${.06 + r() * .1})` : `rgba(110,90,70,${.12 + r() * .12})`;
    g.fillRect(r() * 256, r() * 256, 1 + r() * 2.5, 1 + r() * 2);
  }
  for (const y0 of [54, 88, 168, 202]) {
    const gr = g.createLinearGradient(0, y0 - 13, 0, y0 + 13);
    gr.addColorStop(0, "rgba(150,112,72,0)"); gr.addColorStop(.5, "rgba(150,112,72,.3)"); gr.addColorStop(1, "rgba(150,112,72,0)");
    g.fillStyle = gr; g.fillRect(0, y0 - 13, 256, 26);
    g.fillStyle = "rgba(120,88,56,.16)";
    for (let x = 0; x < 256; x += 5) g.fillRect(x, y0 - 8 + (r() - .5) * 2, 2.5, 16);
  }
  const t = tex(c, { srgb: true, aniso: 16 });
  cache.set("lightRoad", t);
  return t;
}

// Wind ripples on drifted sand.
export function sandRipples() {
  if (cache.has("sandRipples")) return cache.get("sandRipples");
  const [c, g] = canvas(256, 256), r = rng(31);
  g.fillStyle = "#ecd6ad"; g.fillRect(0, 0, 256, 256);
  for (let y = 0; y < 256; y += 7) {
    g.strokeStyle = `rgba(170,130,84,${.18 + r() * .14})`; g.lineWidth = 1.6; g.beginPath();
    for (let x = 0; x <= 256; x += 8) g.lineTo(x, y + Math.sin(x / 19 + y / 11) * 2.6 + (r() - .5));
    g.stroke();
    g.strokeStyle = "rgba(255,246,226,.35)"; g.lineWidth = 1; g.beginPath();
    for (let x = 0; x <= 256; x += 8) g.lineTo(x, y + 2 + Math.sin(x / 19 + y / 11) * 2.6);
    g.stroke();
  }
  const t = tex(c, { srgb: true, aniso: 8 });
  cache.set("sandRipples", t);
  return t;
}
