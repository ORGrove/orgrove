// ORGrove Office 3D (#70): premium isometric "clay" office on the same /api/state as the 2D view.
// Honesty rule: people move only when their real state changes; typing only while a run is live.
// One labelled exception (#121): someone on a coffee break may move between break spots every
// 20 minutes. That walk is still a break, and their tag says so.
import * as THREE from "three";
import { OrbitControls } from "three/addons/OrbitControls.js";
import { CSS2DRenderer, CSS2DObject } from "three/addons/CSS2DRenderer.js";
import { RoomEnvironment } from "three/addons/RoomEnvironment.js";
import { RoundedBoxGeometry } from "three/addons/RoundedBoxGeometry.js";
import { placements, seatOf as seatIn } from "/office_place.js";
import { LAYOUT, LOUNGE_SEATS } from "/layout_classic.js";
import { EDITION } from "/edition.js";

// ---------- palette & roles --------------------------------------------------------------------
const C = { floor: 0xdccfbc, plinth: 0xb9ab95, wall: 0xc9d3c3, oak: 0xcfa676, oakDark: 0xb08a5c, chair: 0x8f98a6,
  screen: 0x262b33, glass: 0xd5e7f3, frame: 0xe4e8ec, sofa: 0xc9b8a6, rug: 0xe9def3, plant: 0x86b496, pot: 0xd9cbb9,
  skin: [0xf1d0b5, 0xe0b391, 0xc68f69, 0x9c6b4c], hair: [0x2f2a28, 0x5b4536, 0x1f1f24, 0x8a6a4f] };
const ROLE = {
  pm:       { name: "PM", team: "Planning",            shirt: 0x6fb98f },
  dev:      { name: "Developer", team: "Build",               shirt: 0x7c8cf8 },
  devops:   { name: "DevOps",    team: "Build",               shirt: 0xb49af0 },
  qa:       { name: "QA",        team: "Quality & Security",  shirt: 0xf2b661 },
  security: { name: "Security",  team: "Quality & Security",  shirt: 0xef8a80 },
  sanity:   { name: "Sanity",    team: "Quality & Security",  shirt: 0x6cc9d6 },
  cto:      { name: "CTO (you)", team: "Leadership",          shirt: 0x2f3542 },
  finance:  { name: "Finance",   team: "Finance",             shirt: 0x4fae9b },
};
const roleInfo = (r) => ROLE[r] || { name: r, team: "Other", shirt: 0x9aa5b1 };
let PEOPLE = {};   // role -> person name, from config/people.toml (#137)
const personLabel = (r) => PEOPLE[r] ? `${PEOPLE[r].split(" ")[0]} · ${roleInfo(r).name}` : roleInfo(r).name;
const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// ---------- renderer, camera, light ---------------------------------------------------------------
const host = document.getElementById("scene");
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight);
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 0.86;
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
host.appendChild(renderer.domElement);
const labels = new CSS2DRenderer();
labels.setSize(innerWidth, innerHeight);
Object.assign(labels.domElement.style, { position: "fixed", inset: "0", pointerEvents: "none" });
host.appendChild(labels.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0xdcd5ca);
scene.environment = new THREE.PMREMGenerator(renderer).fromScene(new RoomEnvironment(), 0.04).texture;
scene.environmentIntensity = 0.55;

const FRUSTUM = 30;
const aspect = () => innerWidth / innerHeight;
const camera = new THREE.OrthographicCamera(-FRUSTUM * aspect() / 2, FRUSTUM * aspect() / 2, FRUSTUM / 2, -FRUSTUM / 2, 0.1, 400);
camera.position.set(34, 32, 34);
const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(0, 0, 0);
controls.enableDamping = true;
controls.minZoom = 0.8; controls.maxZoom = 3.4;
controls.minPolarAngle = 0.55; controls.maxPolarAngle = 1.12;
controls.minAzimuthAngle = Math.PI / 4 - 0.75; controls.maxAzimuthAngle = Math.PI / 4 + 0.75;
controls.screenSpacePanning = true;

const key = new THREE.DirectionalLight(0xfff1dd, 2.6);
key.position.set(18, 30, 12);
key.castShadow = true;
key.shadow.mapSize.set(4096, 4096);
key.shadow.radius = 6;
key.shadow.bias = -0.0004;
Object.assign(key.shadow.camera, { left: -30, right: 30, top: 30, bottom: -30, near: 1, far: 90 });
scene.add(key);
scene.add(new THREE.HemisphereLight(0xfff8ef, 0xb9ad9c, 0.35));

// ---------- builders ------------------------------------------------------------------------------
const mats = new Map();
const mat = (color, o = {}) => {
  const k = color + JSON.stringify(o);
  if (!mats.has(k)) mats.set(k, new THREE.MeshStandardMaterial({ color, roughness: 0.82, metalness: 0, ...o }));
  return mats.get(k);
};
const pickables = [];
function rbox(w, h, d, color, x, y, z, r = 0.08, o = {}, parent = scene) {
  const m = new THREE.Mesh(new RoundedBoxGeometry(w, h, d, 3, Math.min(r, w / 2, h / 2, d / 2)), mat(color, o));
  m.position.set(x, y, z); m.castShadow = m.receiveShadow = true; parent.add(m); return m;
}
function cyl(rt, rb, h, color, x, y, z, parent = scene, seg = 28) {
  const m = new THREE.Mesh(new THREE.CylinderGeometry(rt, rb, h, seg), mat(color));
  m.position.set(x, y, z); m.castShadow = m.receiveShadow = true; parent.add(m); return m;
}
function label(html, cls, x, y, z, parent = scene) {
  const d = document.createElement("div"); d.className = cls; d.innerHTML = html;
  const o = new CSS2DObject(d); o.position.set(x, y, z); parent.add(o); return o;
}
function plant(x, z, s = 1) {
  cyl(0.32 * s, 0.26 * s, 0.55 * s, C.pot, x, 0.28 * s, z);
  for (const [dx, dy, dz, r] of [[0, 0.95, 0, 0.42], [0.18, 1.25, 0.1, 0.3], [-0.16, 1.18, -0.12, 0.3]]) {
    const m = new THREE.Mesh(new THREE.IcosahedronGeometry(r * s, 1), mat(C.plant, { flatShading: true }));
    m.position.set(x + dx * s, dy * s, z + dz * s); m.castShadow = true; scene.add(m);
  }
}
function glassRoom(x, z, w, d, name, id, rug = C.rug) {
  const g = new THREE.Group(); g.position.set(x, 0, z); scene.add(g);
  const glass = mat(C.glass, { transparent: true, opacity: 0.28, roughness: 0.05 });
  for (const [px, pz, sw, sd] of [[0, -d / 2, w, 0.06], [-w / 2, 0, 0.06, d], [w / 2, 0, 0.06, d]]) {
    const p = new THREE.Mesh(new THREE.BoxGeometry(sw, 2.3, sd), glass); p.position.set(px, 1.15, pz); g.add(p);
    rbox(sw + 0.08, 0.08, sd + 0.08, C.frame, px, 2.32, pz, 0.03, {}, g);
  }
  const floor = rbox(w - 0.2, 0.04, d - 0.2, rug, 0, 0.085, 0, 0.02, {}, g);   // sits on the floor surface
  floor.userData = { kind: "room", id }; pickables.push(floor);
  label(name, "zone", x, 0.1, z + d / 2 + 0.6);
  return g;
}
function screenTexture() {
  const cv = document.createElement("canvas"); cv.width = 512; cv.height = 300;
  const tex = new THREE.CanvasTexture(cv); tex.colorSpace = THREE.SRGBColorSpace; tex.anisotropy = 8;
  return { cv, tex };
}
function drawScreen(sc, roleId, row) {
  const g = sc.cv.getContext("2d"), r = roleInfo(roleId), work = row?.status === "working";
  g.fillStyle = "#1c2028"; g.fillRect(0, 0, 512, 300);
  g.fillStyle = "#" + r.shirt.toString(16).padStart(6, "0"); g.fillRect(0, 0, 512, 10);
  g.fillStyle = "#e8ecf1"; g.font = "600 34px Inter, -apple-system, sans-serif"; g.fillText(r.name, 26, 62);
  g.font = "700 22px Inter, -apple-system, sans-serif";
  g.fillStyle = work ? "#5fd39b" : "#f1bd5e"; g.fillText(work ? "● WORKING" : "● ON BREAK", 26, 102);
  g.fillStyle = "#aab3c0"; g.font = "500 24px Inter, -apple-system, sans-serif";
  const t = row?.on?.[0];
  if (work && t) {
    g.fillStyle = "#e8ecf1"; g.font = "700 40px Inter, -apple-system, sans-serif"; g.fillText(`#${t.number}`, 26, 162);
    g.font = "500 22px Inter, -apple-system, sans-serif"; g.fillStyle = "#c9d1dc";
    const words = String(t.title || "").split(" "); let line = "", y = 200;
    for (const w of words) { if (g.measureText(line + w).width > 460) { g.fillText(line, 26, y); line = ""; y += 30; if (y > 270) break; } line += w + " "; }
    if (y <= 270) g.fillText(line, 26, y);
  } else {
    g.fillText(row?.break_since ? `last run ${new Date(row.break_since).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : "no runs yet", 26, 150);
  }
  sc.tex.needsUpdate = true;
}

// ---------- the floor plate & fixed rooms --------------------------------------------------------
rbox(44, 0.6, 30, C.plinth, 0, -0.32, 0, 0.25);
const floorMesh = rbox(43.2, 0.12, 29.2, C.floor, 0, 0.0, 0, 0.06);
rbox(43.2, 2.8, 0.3, C.wall, 0, 1.4, -14.45, 0.08);            // back wall
rbox(43.2, 0.22, 0.34, 0xb9ab97, 0, 0.11, -14.42, 0.04);         // skirting
rbox(0.3, 2.8, 29.2, C.wall, -21.45, 1.4, 0, 0.08);             // left wall
rbox(0.34, 0.22, 29.2, 0xb9ab97, -21.42, 0.11, 0, 0.04);

// back row: meeting room (left; ORGrove Pro only), wallboard (centre), CTO cabin (right)
const MT = new THREE.Vector3(-14, 0, -10.4);                 // meeting table centre
// Seat 0 faces the agenda screen (the CTO's); the rest ring the table, everyone facing its centre.
const MEETING_SEATS = [...Array(8)].map((_, i) => {
  const a = Math.PI / 2 + i * (Math.PI * 2 / 8);
  return [MT.x + Math.cos(a) * 2.35, MT.z + Math.sin(a) * 2.35];
});
const faceTable = (x, z) => Math.atan2(MT.x - x, MT.z - z);
const ms = screenTexture(); ms.cv.width = 1200; ms.cv.height = 640; ms.tex = new THREE.CanvasTexture(ms.cv); ms.tex.colorSpace = THREE.SRGBColorSpace;
const mscreen = new THREE.Mesh(new RoundedBoxGeometry(5.2, 2.75, 0.12, 2, 0.06), [mat(0x20242c), mat(0x20242c), mat(0x20242c), mat(0x20242c),
  new THREE.MeshBasicMaterial({ map: ms.tex, toneMapped: false }), mat(0x20242c)]);
mscreen.position.set(MT.x, 2.05, -13.45);
const meetLight = new THREE.PointLight(0xb9a2ff, 0, 10); meetLight.position.set(MT.x, 2.7, MT.z);
if (EDITION.meetings) {   // ORGrove Community has no meetings, so no meeting room at all (owner, 2026-10-30)
  glassRoom(-14, -10.2, 9, 7, "Meeting room", "meeting", 0xc7b2e6);
  cyl(1.6, 1.6, 0.1, C.oak, MT.x, 0.78, MT.z); cyl(0.18, 0.3, 0.75, C.oakDark, MT.x, 0.38, MT.z);
  for (const [sx, sz] of MEETING_SEATS) rbox(0.7, 0.5, 0.7, C.chair, sx, 0.25, sz, 0.12);
  scene.add(mscreen); scene.add(meetLight);
}

const wb = screenTexture(); wb.cv.width = 1600; wb.cv.height = 600; wb.tex = new THREE.CanvasTexture(wb.cv); wb.tex.colorSpace = THREE.SRGBColorSpace;
const wall = new THREE.Mesh(new RoundedBoxGeometry(10.5, 3.9, 0.18, 3, 0.08), [mat(0x20242c), mat(0x20242c), mat(0x20242c), mat(0x20242c),
  new THREE.MeshBasicMaterial({ map: wb.tex, toneMapped: false }), mat(0x20242c)]);
wall.position.set(0, 2.7, -14.18); wall.userData = { kind: "wall" }; pickables.push(wall); scene.add(wall);

glassRoom(14, -10.2, 9, 7, "CTO cabin · you", "cto", 0xe2c095);
rbox(3.2, 0.12, 1.4, C.oakDark, 14, 0.86, -11.4, 0.05); for (const dx of [-1.4, 1.4]) rbox(0.12, 0.8, 1.2, C.oakDark, 14 + dx, 0.42, -11.4, 0.04);
rbox(0.8, 1.1, 0.8, 0x4b5563, 14, 0.55, -10.2, 0.2);
const ctoLamp = new THREE.PointLight(0xffb84d, 0, 9); ctoLamp.position.set(14, 2.6, -10.2); scene.add(ctoLamp);
const CTO_SEAT = { pos: new THREE.Vector3(14, 0, -10.0), face: Math.PI };
// Finance controller (#78; ORGrove Pro only): a small desk between the wallboard and the CTO cabin.
if (EDITION.finance) {
  rbox(2.4, 0.1, 1.2, C.oak, 7.4, 0.8, -11.3, 0.04); for (const sx of [-1.05, 1.05]) rbox(0.1, 0.76, 1.0, C.oakDark, 7.4 + sx, 0.4, -11.3, 0.03);
  rbox(1.2, 0.75, 0.06, C.screen, 7.4, 1.3, -11.75, 0.03); rbox(0.85, 0.12, 0.85, C.chair, 7.4, 0.52, -10.2, 0.15);
  label("Finance", "zone", 7.4, 0.1, -8.9);
}
const FIN_SEAT = { pos: new THREE.Vector3(7.4, 0, -10.2), face: Math.PI };
// Always in the office, not in the roster: the CTO, and in ORGrove Pro the finance controller.
const ALWAYS = EDITION.finance ? ["cto", "finance"] : ["cto"];
const ctoTag = label("…", "tag", 14, 3.1, -11.4);

// front row: the lounge (right); no review room (owner, 2026-10-30): QA and Security review at their desks
rbox(4.2, 0.12, 1.6, 0xf3f1ec, -14, 0.8, 9.8, 0.05); for (const dx of [-1.8, 1.8]) rbox(0.12, 0.76, 1.4, 0xbfc5cc, -14 + dx, 0.4, 9.8, 0.04);

const loungeRug = rbox(10, 0.04, 7.2, 0xe2c29c, 13.5, 0.085, 9.6, 0.02); loungeRug.userData = { kind: "room", id: "lounge" }; pickables.push(loungeRug);
label("Lounge · on a break", "zone", 13.5, 0.1, 13.8);
const loungeTag = label("", "tag", 11.2, 2.6, 11.6);
rbox(5.2, 0.7, 1.2, C.sofa, 13.5, 0.35, 12.2, 0.3); rbox(5.2, 1.1, 0.4, C.sofa, 13.5, 0.75, 12.75, 0.18);
cyl(0.9, 0.9, 0.08, C.oak, 11.4, 0.62, 8.6); cyl(0.12, 0.2, 0.6, C.oakDark, 11.4, 0.31, 8.6);
cyl(0.9, 0.9, 0.08, C.oak, 15.6, 0.62, 8.6); cyl(0.12, 0.2, 0.6, C.oakDark, 15.6, 0.31, 8.6);
rbox(1.0, 1.6, 0.8, 0x2d3139, 18.6, 0.8, 7.0, 0.12); rbox(0.5, 0.25, 0.4, 0xd9d9d9, 18.6, 1.4, 6.55, 0.05);
for (const [px, pz] of [[-20, -13], [20.5, -2], [-20, 6], [5.5, -13.2], [-5.5, -13.2], [20.5, 13.5], [-8.6, 13.5]]) plant(px, pz, 1.1);

// Break spots (#121): where a coffee break is spent. Each place is [x, z, facing, pose].
rbox(3.0, 0.08, 1.6, 0x3f7f6a, 0, 0.82, 10.2, 0.03); rbox(0.04, 0.16, 1.7, 0xf2f2f2, 0, 0.94, 10.2, 0.02);   // table tennis
for (const [lx, lz] of [[-1.3, 9.6], [1.3, 9.6], [-1.3, 10.8], [1.3, 10.8]]) rbox(0.08, 0.78, 0.08, C.oakDark, lx, 0.39, lz, 0.03);
const ball = new THREE.Mesh(new THREE.SphereGeometry(0.07, 12, 8), mat(0xfff3d6)); ball.visible = false; scene.add(ball);
rbox(2.6, 2.2, 0.5, C.oakDark, -4.6, 1.1, 13.0, 0.06);                                                         // bookshelf
[0.55, 1.2, 1.85].forEach((y, row) => [...Array(6)].forEach((_, i) =>
  rbox(0.26, 0.48, 0.3, [0x7c8cf8, 0xef8a80, 0xf2b661, 0x6fb98f, 0xb49af0, 0x6cc9d6][(i + row * 2) % 6], -5.55 + i * 0.38, y, 13.2, 0.03)));
rbox(0.08, 1.5, 2.8, C.frame, -21.25, 1.65, 2.4, 0.03); rbox(0.06, 1.3, 2.6, 0xbfe0f5, -21.2, 1.65, 2.4, 0.02, { emissive: 0x7fb6d6, emissiveIntensity: 0.25 });  // window
label("Break corner", "zone", -2.3, 0.1, 13.6);
// News TV (#121): one real item every 8 s, from the same snapshot. Nothing here is filler.
const tv = screenTexture(); tv.cv.width = 1200; tv.cv.height = 640; tv.tex = new THREE.CanvasTexture(tv.cv); tv.tex.colorSpace = THREE.SRGBColorSpace;
const tvMesh = new THREE.Mesh(new RoundedBoxGeometry(5.6, 3.0, 0.12, 2, 0.06), [mat(0x20242c), mat(0x20242c), mat(0x20242c), mat(0x20242c),
  new THREE.MeshBasicMaterial({ map: tv.tex, toneMapped: false }), mat(0x20242c)]);
// Square to the room, like the wallboard; big enough to read at the isometric angle.
tvMesh.position.set(13.5, 2.15, 6.2); tvMesh.castShadow = true; tvMesh.userData = { kind: "tv" }; pickables.push(tvMesh); scene.add(tvMesh);
rbox(0.16, 1.1, 0.16, C.screen, 13.5, 0.55, 6.2, 0.04); rbox(1.4, 0.06, 0.6, C.screen, 13.5, 0.05, 6.2, 0.03);

// ---------- team pods from the roster (scales with headcount) ---------------------------------
const desks = new Map();        // role -> {pos, screen}
const podGroup = new THREE.Group(); scene.add(podGroup);
const GAPS = [];                // x of the walkways between and beside the pods
function buildPods(roster) {
  podGroup.clear(); desks.clear(); GAPS.length = 0;
  const teams = new Map();
  for (const row of roster) { const t = roleInfo(row.role).team; if (!teams.has(t)) teams.set(t, []); teams.get(t).push(row.role); }
  const list = [...teams.entries()];
  const podW = 10, gap = 1.4, total = list.length * podW + (list.length - 1) * gap;
  list.forEach(([team, roles], i) => {
    const cx = -total / 2 + podW / 2 + i * (podW + gap), cz = -0.6;
    rbox(podW, 0.05, 6.2, 0xc9bca6, cx, 0.085, cz, 0.04, {}, podGroup);
    GAPS.push(cx + podW / 2 + gap / 2); if (i === 0) GAPS.push(cx - podW / 2 - gap / 2);
    label(team, "zone", cx, 0.1, cz + 3.4, podGroup);
    roles.forEach((role, j) => {
      const dx = cx - podW / 2 + (podW / roles.length) * (j + 0.5);
      const top = rbox(2.6, 0.1, 1.3, C.oak, dx, 0.8, cz - 1.2, 0.04, {}, podGroup);
      for (const sx of [-1.15, 1.15]) rbox(0.1, 0.76, 1.1, C.oakDark, dx + sx, 0.4, cz - 1.2, 0.03, {}, podGroup);
      const sc = screenTexture();
      const mon = new THREE.Mesh(new RoundedBoxGeometry(1.7, 1.02, 0.07, 2, 0.03),
        [mat(C.screen), mat(C.screen), mat(C.screen), mat(C.screen), new THREE.MeshBasicMaterial({ map: sc.tex, toneMapped: false }), mat(C.screen)]);
      mon.position.set(dx, 1.5, cz - 1.62); mon.castShadow = true; podGroup.add(mon);
      rbox(0.12, 0.42, 0.12, C.screen, dx, 1.04, cz - 1.62, 0.03, {}, podGroup);
      mon.userData = { kind: "agent", role }; top.userData = { kind: "agent", role }; pickables.push(mon, top);
      rbox(0.9, 0.12, 0.9, C.chair, dx, 0.52, cz - 0.2, 0.15, {}, podGroup); rbox(0.9, 0.8, 0.14, C.chair, dx, 0.95, cz + 0.22, 0.07, {}, podGroup);
      // Desk lamp (#121): amber while this role's latest finished run failed (QA: also while a PR's CI fails).
      const lamp = new THREE.MeshStandardMaterial({ color: 0xd9d4cc, roughness: 0.5, emissive: 0x000000 });
      cyl(0.03, 0.03, 0.42, C.screen, dx + 1.05, 1.06, cz - 1.5, podGroup, 8);
      const shade = new THREE.Mesh(new THREE.SphereGeometry(0.16, 16, 10), lamp); shade.position.set(dx + 1.05, 1.32, cz - 1.5); podGroup.add(shade);
      desks.set(role, { seat: new THREE.Vector3(dx, 0, cz - 0.2), face: Math.PI, screen: sc, lamp });
    });
  });
}

// ---------- people ----------------------------------------------------------------------------------
const people = new Map();       // role -> person
function makePerson(role) {
  const r = roleInfo(role), seed = [...role].reduce((a, c) => a + c.charCodeAt(0), 0);
  const g = new THREE.Group(), body = new THREE.Group(); g.add(body);
  const shirt = mat(r.shirt), pants = mat(0x3c4250), skin = mat(C.skin[seed % 4]), hair = mat(C.hair[seed % 4]);
  const legL = new THREE.Group(), legR = new THREE.Group();
  for (const [leg, x] of [[legL, -0.17], [legR, 0.17]]) {
    const m = new THREE.Mesh(new THREE.CapsuleGeometry(0.13, 0.62, 6, 12), pants); m.position.y = -0.4; m.castShadow = true;
    leg.add(m); leg.position.set(x, 0.92, 0); body.add(leg);
  }
  const torso = new THREE.Mesh(new THREE.CapsuleGeometry(0.33, 0.55, 8, 16), shirt); torso.position.y = 1.32; torso.castShadow = true; body.add(torso);
  const armL = new THREE.Group(), armR = new THREE.Group();
  for (const [arm, x] of [[armL, -0.45], [armR, 0.45]]) {
    const m = new THREE.Mesh(new THREE.CapsuleGeometry(0.1, 0.5, 6, 10), shirt); m.position.y = -0.3; m.castShadow = true;
    const hand = new THREE.Mesh(new THREE.SphereGeometry(0.1, 12, 10), skin); hand.position.y = -0.62;
    arm.add(m, hand); arm.position.set(x, 1.62, 0); body.add(arm);
  }
  const head = new THREE.Mesh(new THREE.SphereGeometry(0.3, 24, 18), skin); head.position.y = 2.02; head.castShadow = true; body.add(head);
  const cap = new THREE.Mesh(new THREE.SphereGeometry(0.315, 24, 12, 0, Math.PI * 2, 0, Math.PI / 2.1), hair); cap.position.y = 2.05; cap.rotation.x = -0.25; body.add(cap);
  g.traverse((o) => { if (o.isMesh) { o.userData = { kind: "agent", role }; pickables.push(o); } });
  const tag = label("", "tag", 0, 2.75, 0, g);
  const bubble = label("", "bubble", 0, 3.35, 0, g); bubble.visible = false;
  scene.add(g);
  return { role, g, body, legL, legR, armL, armR, tag, bubble, path: [], pose: "stand", working: false, facing: 0 };
}

const AISLE_Z = 4.2, BACK_Z = -5.4;   // front aisle (desks <-> lounge/review) and back aisle (rooms along the wall)
const V = (x, z) => new THREE.Vector3(x, 0, z);
function routeTo(p, dest, pose, facing) {
  const cur = p.g.position.clone(), back = (v) => v.z < -4.5;
  if (cur.distanceTo(dest) < 0.05) { p.pose = pose; p.facing = facing; return; }
  // Never through the desks: switch between front and back via the walkway nearest the destination.
  const gapNear = (x) => GAPS.reduce((b, g) => Math.abs(g - x) < Math.abs(b - x) ? g : b, GAPS[0] ?? 0);
  if (back(cur) && back(dest)) p.path = [V(cur.x, BACK_Z), V(dest.x, BACK_Z), dest.clone()];
  else if (!back(cur) && !back(dest)) p.path = [V(cur.x, AISLE_Z), V(dest.x, AISLE_Z), dest.clone()];
  else if (back(dest)) { const gx = gapNear(dest.x); p.path = [V(cur.x, AISLE_Z), V(gx, AISLE_Z), V(gx, BACK_Z), V(dest.x, BACK_Z), dest.clone()]; }
  else { const gx = gapNear(cur.x); p.path = [V(cur.x, BACK_Z), V(gx, BACK_Z), V(gx, AISLE_Z), V(dest.x, AISLE_Z), dest.clone()]; }
  p.after = { pose, facing };
  p.pose = "walk";
}

// ---------- state -> scene -----------------------------------------------------------------------
let STATE = null, MTG = null, placed = false;
// Where everyone is: from the real state (runs, review) and the live meeting (#102). Re-run whenever either changes.
function moveTo(p, dest, pose, facing) {
  if (!placed) { p.g.position.copy(dest); p.pose = pose; p.facing = facing; p.path = []; }
  else if (!p.dest || p.dest.distanceTo(dest) > 0.05) routeTo(p, dest, pose, facing);
  else { p.after = { pose, facing }; if (!p.path.length) { p.pose = pose; p.facing = facing; } }
  p.dest = dest;
}
const seatOf = (character) => seatIn(character, MTG);   // office_place.js decides; this look only draws
function placeAll() {
  const s = STATE; if (!s) return;
  const roster = s.roster || [];
  if (!placed || roster.length !== desks.size) buildPods(roster);
  const plan = placements(s, MTG, { ...LAYOUT, always: ALWAYS });
  const ciFailing = (s.inbox || []).some((i) => i.kind === "merge" && i.ci === "failing");
  roster.forEach((row) => {
    if (!people.has(row.role)) people.set(row.role, makePerson(row.role));
    const p = people.get(row.role), d = desks.get(row.role), task = row.on?.[0], at = plan.at.get(row.role), seat = at.seat;
    p.working = row.status === "working";
    let dest, pose, facing;
    if (at.where === "meeting") {                      // invited and free: walks to the meeting room
      const [x, z] = MEETING_SEATS[at.index]; dest = V(x, z); pose = "sit"; facing = faceTable(x, z);
    } else if (at.where === "desk") { dest = d.seat.clone(); pose = "type"; facing = d.face; }
    else if (at.where === "break") { dest = V(at.place[0], at.place[1]); facing = at.place[2]; pose = at.place[3]; }
    else { const [x, z] = LOUNGE_SEATS[at.index]; dest = V(x, z); pose = "sit"; facing = z > 11 ? Math.PI : 0; }
    p.spot = at.breakSpot;
    moveTo(p, dest, pose, facing);
    if (d) {
      const last = (s.runs || []).find((x) => x.role === row.role && x.status === "completed");
      const amber = last?.conclusion === "failure" || (row.role === "qa" && ciFailing);
      d.lamp.color.setHex(amber ? 0xffa733 : 0xd9d4cc); d.lamp.emissive.setHex(amber ? 0xc25f00 : 0x000000);
    }
    const mins = row.break_since ? Math.floor((Date.now() - new Date(row.break_since)) / 60000) : null;
    const joinsLater = seat >= 0 && p.working;          // honest: a live run is finished first
    p.tag.visible = p.working || seat >= 0;              // lounge shows one summary tag
    p.tag.element.innerHTML = joinsLater
      ? `<i style="background:#7c8cf8"></i>${esc(personLabel(row.role))} <small>joins after #${esc(task?.number ?? "?")}</small>`
      : p.working
      ? `<i style="background:#2f9e6e"></i>${esc(personLabel(row.role))} <small>#${esc(task?.number ?? "?")}</small>`
      : seat >= 0 ? `<i style="background:#b9a2ff"></i>${esc(personLabel(row.role))} <small>in the meeting</small>`
      : `<i style="background:#c27a12"></i>${esc(personLabel(row.role))} <small>on break · ${mins == null ? "no runs yet" : mins < 120 ? mins + "m" : Math.floor(mins / 60) + "h " + (mins % 60) + "m"}${p.spot ? " · " + esc(p.spot) : ""}</small>`;
    if (d) drawScreen(d.screen, row.role, row);
  });
  for (const who of ALWAYS) {                                                 // not in the roster: always present
    const home = who === "cto" ? CTO_SEAT : FIN_SEAT;
    if (!people.has(who)) people.set(who, makePerson(who));
    const p = people.get(who), seat = plan.at.get(who).seat;
    if (seat >= 0) { const [x, z] = MEETING_SEATS[seat]; moveTo(p, V(x, z), "sit", faceTable(x, z)); }
    else moveTo(p, home.pos.clone(), "sit", home.face);
    p.tag.visible = who === "finance" || seat >= 0;
    p.tag.element.innerHTML = `<i style="background:${who === "cto" ? "#1f2430" : "#4fae9b"}"></i>${esc(who === "cto" ? roleInfo(who).name : personLabel(who))}`
      + (seat >= 0 ? " <small>in the meeting</small>" : "");
  }
  placed = true;
  const resting = plan.resting;
  loungeTag.visible = resting > 0;
  loungeTag.element.innerHTML = `<i style="background:#c27a12"></i>${resting} on a break <small>hover for names</small>`;
  pingpong = plan.pingpong.map((role) => people.get(role));
}
let pingpong = [];

function apply(s) {
  STATE = s;
  placeAll();
  hud(s);
  drawWallboard(s);
  const waiting = (s.inbox || []).length;
  ctoLamp.intensity = waiting ? 6 : 0;
  ctoTag.element.innerHTML = waiting ? `<i style="background:#c27a12"></i>${waiting} waiting on you` : `<i style="background:#2f9e6e"></i>nothing waiting`;
  NEWS = newsItems(s); drawTV();
  celebrate(s);
  if (openPanel) openPanel();
  if (MTG?.active) renderBubbles();
}

// ---------- news TV & celebrations (#121) ------------------------------------------------------------
let NEWS = [], newsAt = 0;
const DAY_MS = 864e5;
function newsItems(s) {
  const recent = (ts) => ts && Date.now() - new Date(ts) < DAY_MS, items = [];
  const shipped = (s.cards || []).filter((c) => c.stage === "done" && recent(c.updated));
  const finished = (s.runs || []).filter((r) => r.status === "completed" && r.role && recent(r.updated)).slice(0, 5);
  if (!shipped.length) items.push({ tag: "Quiet day", color: "#9aa4b2", text: "Nothing shipped in 24 h",
    sub: finished.length ? `${finished.length} run(s) finished` : "no runs finished either" });
  for (const c of shipped) items.push({ tag: "Shipped", color: "#5fd39b", text: `#${c.number} ${c.title}`, sub: c.project, url: c.url });
  for (const c of (s.cards || []).filter((k) => k.stage === "deployed-dev" && recent(k.updated)))
    items.push({ tag: "On Dev", color: "#8ea0ff", text: `#${c.number} ${c.title}`, sub: c.project, url: c.url });
  for (const r of finished) items.push({ tag: "Run finished", text: `${personLabel(r.role)} · #${r.issue ?? "?"}`, url: r.url,
    color: r.conclusion === "success" ? "#5fd39b" : r.conclusion === "failure" ? "#f07a6e" : "#9aa4b2",
    sub: r.conclusion === "success" ? "passed" : r.conclusion === "failure" ? "failed" : r.conclusion || "ended" });
  for (const i of (s.inbox || []).filter((x) => x.kind === "merge")) items.push({ tag: "Waiting on you", text: `PR #${i.number} ${i.title}`, url: i.url,
    color: i.ci === "failing" ? "#f07a6e" : i.ci === "pending" ? "#f1bd5e" : "#5fd39b", sub: `CI ${i.ci} · ${i.project}` });
  const sp = s.spend || {};
  items.push({ tag: "Spend", color: "#f1bd5e", text: `Today $${(sp.today || 0).toFixed(2)} of $${sp.daily_cap}`,
    sub: `This month $${(sp.month || 0).toFixed(2)} of $${sp.monthly_cap}` });
  if (s.why?.headline) items.push({ tag: "Team status", color: "#8ea0ff", text: s.why.headline, sub: `dispatcher looked ${s.why.age_min ?? "?"} min ago` });
  return items;
}
function drawTV() {
  const g = tv.cv.getContext("2d"), W = tv.cv.width, H = tv.cv.height, it = NEWS[newsAt % Math.max(NEWS.length, 1)];
  g.fillStyle = "#1b1f27"; g.fillRect(0, 0, W, H);
  if (!it) { tv.tex.needsUpdate = true; return; }
  g.fillStyle = it.color; g.fillRect(0, 0, W, 14);
  g.font = "800 46px Inter, sans-serif"; g.fillText(it.tag.toUpperCase(), 60, 100);
  g.fillStyle = "#e9edf2"; g.font = "700 66px Inter, sans-serif";
  const words = String(it.text).split(" "); let line = "", y = 210;
  for (const w of words) {
    if (g.measureText(line + w).width > W - 120 && line) { g.fillText(line, 60, y); line = ""; y += 78; if (y > 430) { line = "…"; break; } }
    line += w + " ";
  }
  g.fillText(line, 60, y);
  g.fillStyle = "#9aa4b2"; g.font = "500 38px Inter, sans-serif"; g.fillText(String(it.sub || ""), 60, H - 110);
  g.font = "500 26px Inter, sans-serif";
  g.fillText(`${(newsAt % NEWS.length) + 1}/${NEWS.length} · live data · ${new Date(STATE?.generated || Date.now()).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`, 60, H - 50);
  tv.tex.needsUpdate = true;
}
setInterval(() => { if (NEWS.length > 1) { newsAt = (newsAt + 1) % NEWS.length; drawTV(); } }, 8000);
function tvPanel() {
  return `<h3>News TV</h3><div class="sub">Everything on the Lounge TV, from the same live data as the rest of the office.</div>
    <div class="sec">Now (${NEWS.length})</div>${NEWS.map((n) => `<div class="row"><span><b style="color:${n.color}">${esc(n.tag)}</b> ${link(n.url, n.text)}</span><span>${esc(n.sub || "")}</span></div>`).join("")}`;
}

// Celebrations play only real changes the server saw (office.changes), each once per browser.
// A first visit plays nothing old; they arrive within about a minute of the dispatcher's tick.
const SEEN_KEY = "orgrove.office.seenChange";
let seenAt = (() => { try { return localStorage.getItem(SEEN_KEY); } catch { return null; } })();
const fx = [];
function celebrate(s) {
  const log = s.changes || [], newest = log.length ? log[log.length - 1].at : "";
  const fresh = seenAt === null ? [] : log.filter((c) => c.at > seenAt);
  if (seenAt === null || newest > (seenAt || "")) { seenAt = newest; try { localStorage.setItem(SEEN_KEY, seenAt); } catch { /* private window */ } }
  fresh.forEach((c, i) => setTimeout(() => react(c), i * 7000));
}
function spotOf(role) {
  const d = desks.get(role); return d ? d.seat.clone() : people.get(role)?.g.position.clone() || V(0, -12);
}
function flash(html, at, ms = 6500) {
  const l = label(html, "tag fx", at.x, 3.6, at.z); setTimeout(() => scene.remove(l), ms);
}
function react(c) {
  const roster = (STATE?.roster || []).map((r) => r.role), who = c.roles.filter((r) => people.has(r));
  const t = clock.elapsedTime;
  if (c.kind === "shipped") {
    for (const r of who.length ? who : roster) { const p = people.get(r); if (p && !MTG?.active) p.cheerUntil = t + 6; }
    const at = who.length ? spotOf(who[0]) : V(0, -12);
    flash(`<i style="background:#2f9e6e"></i>Shipped #${esc(c.issue)} <small>${esc(String(c.title).slice(0, 48))}</small>`, at);
    if (!still) confetti(at);
  } else if (c.kind === "deployed") {
    const at = spotOf("devops");
    flash(`<i style="background:#4f5fd6"></i>🔔 #${esc(c.issue)} is on Dev`, at);
    if (!still) bell(at);
  } else {
    const role = c.kind === "ci_failing" ? "qa" : who[0], at = role ? spotOf(role) : V(0, -12);
    flash(`<i style="background:#c2413a"></i>${c.kind === "ci_failing" ? `CI failing · PR #${esc(c.issue)}` : `Run failed · ${esc(c.title)}`}`, at);
  }
}
function confetti(at) {
  const g = new THREE.Group(); scene.add(g);
  const cols = [0x5fd39b, 0xf1bd5e, 0x8ea0ff, 0xef8a80, 0xb49af0];
  const bits = [...Array(70)].map((_, i) => {
    const m = new THREE.Mesh(new THREE.PlaneGeometry(0.12, 0.08), new THREE.MeshBasicMaterial({ color: cols[i % cols.length], side: THREE.DoubleSide }));
    m.position.set(at.x, 3.2, at.z); g.add(m);
    return { m, v: new THREE.Vector3((Math.random() - 0.5) * 4, 3 + Math.random() * 3, (Math.random() - 0.5) * 4), s: Math.random() * 8 };
  });
  let life = 0;
  fx.push((dt) => {
    life += dt;
    for (const b of bits) { b.v.y -= 6 * dt; b.m.position.addScaledVector(b.v, dt); b.m.rotation.x += b.s * dt; b.m.rotation.y += b.s * dt; if (b.m.position.y < 0.1) b.v.set(0, 0, 0); }
    if (life > 6) { scene.remove(g); g.traverse((o) => { o.geometry?.dispose(); o.material?.dispose(); }); return false; }
    return true;
  });
}
function bell(at) {
  const g = new THREE.Group(); g.position.set(at.x, 3.3, at.z); scene.add(g);
  const b = new THREE.Mesh(new THREE.ConeGeometry(0.35, 0.5, 20, 1, true), mat(0xf1bd5e, { side: THREE.DoubleSide, metalness: 0.4, roughness: 0.3 }));
  b.position.y = -0.25; g.add(b);
  let life = 0;
  fx.push((dt) => {
    life += dt; g.rotation.z = Math.sin(life * 12) * 0.5 * Math.max(0, 1 - life / 5);
    if (life > 6) { scene.remove(g); return false; }
    return true;
  });
}

function hud(s) {
  const roster = s.roster || [], working = roster.filter((r) => r.status === "working").length, waiting = (s.inbox || []).length;
  const set = (id, v, cls) => { const k = document.getElementById(id); k.querySelector(".v").textContent = v; k.className = "kpi " + (cls || ""); };
  document.getElementById("sub").textContent = `${(s.projects || []).map((p) => p.id).join(" + ")} · ${s.enabled === "true" ? "agents enabled" : "KILL SWITCH ON"}`;
  set("k-work", `${working}/${roster.length}`, working ? "good" : "");
  set("k-wait", String(waiting), waiting ? "warn" : "good");
  const hs = (s.projects || []).map((p) => p.health).filter(Boolean)[0];
  set("k-health", !hs || hs.state === "none" ? "—" : hs.state === "green" ? "Green" : hs.state === "red" ? "Red" : "Amber",
      hs?.state === "green" ? "good" : hs?.state === "red" ? "bad" : hs?.state === "amber" ? "warn" : "");
  const sp = s.spend || {};
  set("k-spend", `$${(sp.today || 0).toFixed(2)}`, sp.today >= sp.daily_cap ? "bad" : sp.today >= 0.75 * sp.daily_cap ? "warn" : "");
  document.getElementById("b-spend").style.width = Math.min(100, ((sp.today || 0) / (sp.daily_cap || 1)) * 100) + "%";
}

function drawWallboard(s) {
  const g = wb.cv.getContext("2d"), W = wb.cv.width, H = wb.cv.height;
  g.fillStyle = "#1b1f27"; g.fillRect(0, 0, W, H);
  g.fillStyle = "#e9edf2"; g.font = "700 54px Inter, -apple-system, sans-serif"; g.fillText("Pipeline", 60, 90);
  const cols = (s.columns || []).filter((c) => c.id !== "backlog");
  const counts = cols.map((c) => (s.cards || []).filter((k) => k.stage === c.id).length), max = Math.max(1, ...counts);
  cols.forEach((c, i) => {
    const x = 60 + i * 160, h = 260 * (counts[i] / max);
    g.fillStyle = "#2b313c"; g.fillRect(x, 140, 110, 260);
    g.fillStyle = c.id === "awaiting-review" ? "#f1bd5e" : c.id === "done" ? "#5fd39b" : "#8ea0ff"; g.fillRect(x, 400 - h, 110, h);
    g.fillStyle = "#e9edf2"; g.font = "700 40px Inter, sans-serif"; g.fillText(String(counts[i]), x + 8, 390 - h);
    g.fillStyle = "#9aa4b2"; g.font = "500 22px Inter, sans-serif"; g.fillText(c.label.replace("Awaiting Review", "Awaiting you").slice(0, 12), x, 440);
  });
  const sp = s.spend || {}, hs = (s.projects || []).map((p) => p.health).filter(Boolean)[0];
  g.fillStyle = "#e9edf2"; g.font = "700 30px Inter, sans-serif"; g.fillText(`Spend today  $${(sp.today || 0).toFixed(2)} / $${sp.daily_cap}`, 60, 520);
  g.fillStyle = "#2b313c"; g.fillRect(60, 540, 700, 16);
  g.fillStyle = sp.today >= sp.daily_cap ? "#f07a6e" : "#5fd39b"; g.fillRect(60, 540, 700 * Math.min(1, (sp.today || 0) / (sp.daily_cap || 1)), 16);
  g.fillStyle = !hs || hs.state === "none" ? "#9aa4b2" : hs.state === "green" ? "#5fd39b" : hs.state === "red" ? "#f07a6e" : "#f1bd5e";
  g.font = "700 30px Inter, sans-serif"; g.fillText(`Ops health  ${hs ? (hs.state === "none" ? "no report yet" : hs.summary.slice(0, 40)) : "—"}`, 830, 520);
  wb.tex.needsUpdate = true;
}

// ---------- detail panel --------------------------------------------------------------------------
const panel = document.getElementById("panel"), pbody = document.getElementById("pbody");
let openPanel = null;
document.getElementById("pclose").onclick = () => { panel.classList.remove("open"); openPanel = null; };
const link = (url, text) => url ? `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(text)}</a>` : esc(text);
function show(fn) { openPanel = fn; pbody.innerHTML = fn(); panel.classList.add("open"); }
function cardLink(n, project) {
  const k = (STATE.cards || []).find((c) => c.number === n && (!project || c.project === project));
  return k ? `${link(k.url, "#" + n)} ${esc(k.title)}` : esc("#" + n);
}
function whyBlock(role) {
  const st = (STATE.why?.roles || {})[role];
  if (!st) return "";
  const issues = (st.issues || []).map((n) => `<div class="row"><span>${cardLink(n, st.project)}</span></div>`).join("");
  const next = (st.next || []).map((n) => `<div class="row"><span>${cardLink(n, st.project)}</span></div>`).join("");
  return `<div class="sec">Why</div><div><b>${esc(st.text)}</b>${st.detail ? ` · ${esc(st.detail)}` : ""}</div>${issues}
    <div class="sec">Next in the queue</div>${next || `<div class="sub">nothing queued</div>`}`;
}
function agentPanel(role) {
  return () => {
    const row = (STATE.roster || []).find((r) => r.role === role) || {}, t = row.on?.[0], r = roleInfo(role);
    const runs = (STATE.runs || []).filter((x) => (x.title || "").startsWith(role + " ")).slice(0, 6);
    const spend = (STATE.spend?.by_role || {})[role] || 0;
    const mins = row.break_since ? Math.floor((Date.now() - new Date(row.break_since)) / 60000) : null;
    return `<h3>${esc(PEOPLE[role] ? PEOPLE[role] + " · " + r.name : r.name)}</h3><div class="sub">${esc(r.team)} · ${esc((row.model || "").replace("claude-", ""))} · limit $${esc(row.ceiling)}/run</div>
      <span class="chip ${row.status === "working" ? "work" : "brk"}">${row.status === "working" ? "Working" : mins == null ? "No runs yet" : "On break · " + mins + " min"}</span>
      ${whyBlock(role)}
      <div class="sec">Current ticket</div>${t ? `<div>${link(t.url, "#" + t.number)} ${esc(t.title)}<div class="sub">${esc(t.project || "")}</div></div>` : `<div class="sub">none</div>`}
      <div class="sec">Today</div><div class="row"><span>Spend</span><b>$${spend.toFixed(2)}</b></div>
      <div class="sec">Recent runs</div>${runs.map((x) => `<div class="row"><span>${link(x.url, x.title)}</span><span>${esc(x.conclusion || x.status)}</span></div>`).join("") || `<div class="sub">none</div>`}`;
  };
}
function roomPanel(id) {
  return () => {
    const s = STATE;
    if (id === "cto") {
      const items = s.inbox || [], W = s.why || {}, held = W.owner_waiting || [];
      return `<h3>CTO cabin</h3><div class="sub">What's waiting on you. Act on it in the <a href="/">2D view</a>.</div>
        ${W.headline ? `<div class="sec">Team status</div><div><b>${esc(W.headline)}</b></div>
          <div class="sub">The dispatcher's own reasons (#98), from its last look ${W.age_min ?? "?"} min ago. Click anyone for theirs.</div>` : ""}
        <div class="sec">Inbox (${items.length})</div>${items.map((i) => `<div class="row"><span>${link(i.url, "#" + i.number)} ${esc(i.title)}</span><span>${esc(i.kind)}</span></div>`).join("") || `<div class="sub">nothing waiting</div>`}
        <div class="sec">Held for you (${held.length})</div>${held.map((w) => `<div class="row"><span>${cardLink(w.issue, w.project)}</span><span>${esc(w.what)}</span></div>`).join("") || `<div class="sub">nothing held</div>`}`;
    }
    if (id === "meeting") {
      const m = s.meeting || {};
      return `<h3>Meeting room</h3><div class="sub">${m.active ? `In session · ${m.rounds?.length || 0} round(s)` : "No meeting running."}</div>
        <div class="sec">&nbsp;</div>${m.active ? "The meeting is running: use the dock below." : "Use <b>Call a meeting</b> in the top bar."}`;
    }
    const on = (s.roster || []).filter((r) => r.status !== "working");
    return `<h3>Lounge</h3><div class="sub">Agents with nothing waiting for them. A free agent takes the next ticket at once: there is no break while work waits.
      Moving between break spots (coffee machine, table tennis, sofa, bookshelf, window) is only a coffee break: working means a real run.
      Celebrations follow real events and can arrive up to about 6 min late.</div>
      <div class="sec">Not working (${on.length}) and why</div>${on.map((r) => { const st = (s.why?.roles || {})[r.role]; return `<div class="row"><span>${esc(personLabel(r.role))}</span><span>${esc(st ? st.text + (st.detail ? " · " + st.detail : "") : "no status yet")}</span></div>`; }).join("")}`;
  };
}
function wallPanel() {
  const s = STATE, hs = (s.projects || []).map((p) => p.health).filter(Boolean)[0];
  const rows = (s.columns || []).map((c) => `<div class="row"><span>${esc(c.label)}</span><b>${(s.cards || []).filter((k) => k.stage === c.id).length}</b></div>`).join("");
  return `<h3>Wallboard</h3><div class="sub">Pipeline across ${(s.projects || []).length} project(s).</div>${rows}
    <div class="sec">Ops health</div>${hs ? link(hs.url, hs.state === "none" ? "no report yet" : hs.summary) : "not configured"}`;
}

const ray = new THREE.Raycaster(), ptr = new THREE.Vector2();
let hovered = null;
function hit(ev) {
  ptr.set((ev.clientX / innerWidth) * 2 - 1, -(ev.clientY / innerHeight) * 2 + 1);
  ray.setFromCamera(ptr, camera);
  return ray.intersectObjects(pickables, false)[0]?.object.userData;
}
renderer.domElement.addEventListener("pointermove", (ev) => {
  const u = hit(ev); renderer.domElement.style.cursor = u?.kind ? "pointer" : "";
  const p = u?.kind === "agent" ? people.get(u.role) : null;
  if (hovered && hovered !== p) { hovered.g.scale.setScalar(1); if (!hovered.working) hovered.tag.visible = false; }
  if (p) { p.g.scale.setScalar(1.06); p.tag.visible = true; }
  hovered = p;
});
renderer.domElement.addEventListener("click", (ev) => {
  const u = hit(ev); if (!u?.kind || !STATE) return;
  if (u.kind === "agent") show(u.role === "cto" ? roomPanel("cto") : agentPanel(u.role));  // the CTO's cabin is the CTO's view
  else if (u.kind === "room") show(roomPanel(u.id));
  else if (u.kind === "wall") show(wallPanel);
  else if (u.kind === "tv") show(tvPanel);
});

// ---------- camera views & demo tour --------------------------------------------------------------
const VIEWS = {
  Overview: { t: [0.5, 0, 0.6], zoom: 1.0 }, Teams: { t: [0, 0, -1], zoom: 1.7 },
  Lounge: { t: [13.5, 0, 9.6], zoom: 2.4 }, "CTO cabin": { t: [14, 0, -10.2], zoom: 2.4 },
  ...(EDITION.meetings ? { Meeting: { t: [-13.2, 1.2, -10.6], zoom: 1.75 } } : {}),   // ORGrove Pro only
};
let tween = null, touring = null;
function go(name) {
  const v = VIEWS[name], off = camera.position.clone().sub(controls.target);
  tween = { from: controls.target.clone(), to: new THREE.Vector3(...v.t), z0: camera.zoom, z1: v.zoom, off, t: 0 };
  document.querySelectorAll("#views button").forEach((b) => b.classList.toggle("on", b.dataset.v === name));
}
const views = document.getElementById("views");
for (const name of Object.keys(VIEWS)) {
  const b = document.createElement("button"); b.textContent = name; b.dataset.v = name;
  b.onclick = () => { stopTour(); go(name); }; views.append(b);
}
const tourBtn = document.createElement("button"); tourBtn.textContent = "▶ Tour"; views.append(tourBtn);
const newLook = document.createElement("a"); newLook.href = "/office3d"; newLook.textContent = "New look"; newLook.className = "look";
if (EDITION.new_look) views.append(newLook);   // ORGrove Pro only
function stopTour() { if (touring) { clearInterval(touring); touring = null; tourBtn.textContent = "▶ Tour"; } }
tourBtn.onclick = () => {
  if (touring) return stopTour();
  const order = Object.keys(VIEWS); let i = 0; go(order[0]);
  touring = setInterval(() => { i = (i + 1) % order.length; go(order[i]); }, 5200); tourBtn.textContent = "■ Stop";
};
camera.zoom = VIEWS.Overview.zoom; controls.target.set(...VIEWS.Overview.t); camera.position.set(34, 32, 34.5); camera.updateProjectionMatrix();
document.querySelector('#views button[data-v="Overview"]').classList.add("on");

// ---------- meetings (#102, #137) ---------------------------------------------------------------------
// The office reacts to the real meeting: who is invited walks in, nobody speaks until the CTO types, and
// only the people the CTO tagged answer, one at a time, as each model call returns. Nothing here invents a line.
// Voice output is off (#138): replies are text above each person and in the meeting panel.
const $ = (id) => document.getElementById(id);
let TYPES = [], pickType = null, VOICE = false, pollTimer = null, sending = false;
const shown = new Set(), revealQ = [];
let revealing = null;

async function post(payload) {
  const r = await orgrovePost("/api/meeting", { headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload) });
  const out = await r.json();
  if (out.error) throw new Error(out.error);
  return out;
}
async function loadMeeting() {
  try {
    const m = await (await fetch("/api/meeting", { cache: "no-store" })).json();
    TYPES = m.types || TYPES; VOICE = !!m.voice; PEOPLE = m.people || PEOPLE;
    setMeeting(m);
  } catch (e) { /* the office keeps working without the meeting service */ }
}
function setMeeting(m) {
  const was = !!MTG?.active;
  MTG = m;
  placeAll();
  meetLight.intensity = m.active ? 5 : 0;
  if (m.active && !was) { panel.classList.remove("open"); go("Meeting"); }
  if (m.active && !pollTimer) pollTimer = setInterval(loadMeeting, 1500);   // local only: no GitHub calls
  if (!m.active && pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  if (was && m.status === "closed") showResult(m);
  if (!m.active) { for (const p of people.values()) p.bubble.visible = false; revealQ.length = 0; }
  queueReplies(); renderBubbles(); renderDock(); drawMeetingScreen();
}

// ---- who says what: one reply at a time, held long enough to read -------------------------------------
const msgs = () => MTG?.messages || [];
const firstName = (pid) => (MTG?.names?.[pid] || pid).split(" ")[0];
function lastCtoIndex() { const m = msgs(); for (let i = m.length - 1; i >= 0; i--) if (m[i].who === "cto") return i; return -1; }
let primed = false;
function queueReplies() {
  if (!primed && MTG?.id) {             // opened mid-meeting: what was already said shows at once, no replay
    primed = true;
    msgs().forEach((x, i) => { const k = MTG.id + "|" + i; shown.add(k); visible.add(k); });
  }
  msgs().forEach((x, i) => {
    const k = MTG.id + "|" + i;
    if (x.who === "cto" || shown.has(k)) return;
    shown.add(k); revealQ.push(k);
  });
  if (!revealing) nextReveal();
}
const walking = (ch) => !!people.get(ch)?.path.length;
const visible = new Set();
function nextReveal() {
  const k = revealQ[0];
  if (!k) { revealing = null; renderBubbles(); renderDock(); return; }
  const x = msgs()[Number(k.split("|")[1])];
  if (!x) { revealQ.shift(); return nextReveal(); }
  if (walking(MTG.characters[x.who])) return setTimeout(nextReveal, 400);   // sit down first
  revealQ.shift(); revealing = k; visible.add(k);
  renderBubbles(); renderDock();
  setTimeout(nextReveal, Math.min(9000, Math.max(2500, x.text.split(" ").length * 260)));
}
function renderBubbles() {
  for (const p of people.values()) { p.bubble.visible = false; p.bubble.element.className = "bubble"; p.bubble.element.style.opacity = "1"; }
  if (!MTG?.active) return;
  // Only the person speaking now gets a bubble, kept short; every reply is in full in the meeting panel.
  const m = msgs(), from = lastCtoIndex();
  const now = revealing ? Number(revealing.split("|")[1]) : -1;
  const x = m[now];
  if (x && now > from && x.who !== "cto") {
    const p = people.get(MTG.characters[x.who]);
    if (p) {
      const short = x.text.length > 200 ? x.text.slice(0, x.text.lastIndexOf(" ", 190)) + " …" : x.text;
      p.bubble.visible = true;
      p.bubble.element.innerHTML = `<b>${esc(MTG.names[x.who])} · ${esc(MTG.titles[x.who])}</b>${esc(short)}`;
    }
  }
  const th = MTG.thinking && MTG.characters?.[MTG.thinking];
  if (th && people.get(th)) { const p = people.get(th); p.bubble.visible = true; p.bubble.element.className = "bubble think"; p.bubble.element.textContent = "…"; }
  const me = people.get("cto"), mine = from >= 0 && MTG.status === "asking" ? m[from].text : "";
  if (me && mine) { me.bubble.visible = true; me.bubble.element.className = "bubble cto"; me.bubble.element.innerHTML = `<b>You</b>${esc(mine)}`; }
  for (const p of people.values()) if (p.bubble.visible) p.tag.visible = false;   // the bubble names the speaker
}

// ---- the dock ----------------------------------------------------------------------------------------
function renderDock() {
  const dock = $("dock"), on = !!MTG?.active;
  dock.classList.toggle("open", on); $("views").style.display = on ? "none" : ""; document.body.classList.toggle("meeting", on);
  if (!on) return;
  const items = MTG.agenda_items || [], at = MTG.agenda_at ?? 0;
  $("dagenda").innerHTML = `<b>${esc(MTG.title || "Meeting")}</b>` + items.map((a, i) =>
    `<div class="${i < at ? "done" : i === at ? "now" : ""}">${i < at ? "✓" : i === at ? "▸" : "·"} ${esc(a)}</div>`).join("");
  const log = $("dlog"), atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
  log.innerHTML = msgs().map((x, i) => x.who === "cto"
    ? `<div class="me"><b>You → ${esc(x.to.map(firstName).join(", "))}</b>${esc(x.text)}</div>`
    : visible.has(MTG.id + "|" + i) ? `<div><b>${esc(MTG.names[x.who])} · ${esc(MTG.titles[x.who])}</b>${esc(x.text)}</div>` : "").join("")
    || `<div class="hint">Nobody speaks until you do. Type a question and tag who should answer.</div>`;
  if (atEnd) log.scrollTop = log.scrollHeight;
  const thinking = MTG.thinking && MTG.names?.[MTG.thinking], busy = MTG.status === "asking" || revealQ.length || revealing;
  let line;
  if (sending) line = "Sending…";
  else if (MTG.status === "wrapping") line = `${esc(firstName("pm"))} is writing the minutes, decisions and draft tickets…`;
  else if (thinking) line = `${esc(thinking)} is thinking…`;
  else if (busy) line = "Replying…";
  else if ([...people.values()].some((p) => p.path.length && seatOf(p.role) >= 0)) line = "Everyone is walking to the meeting room…";
  else line = `<b>Your turn.</b> Tag who should answer, e.g. <i>@${esc(firstName(MTG.host || "pm"))}</i>; no tag goes to ${esc(firstName(MTG.host || "pm"))}.`;
  if (MTG.error) line += `<div style="color:var(--red);font-size:12px;margin-top:4px">${esc(MTG.error)}</div>`;
  $("dlive").innerHTML = line + `<small>Spent $${Number(MTG.cost_usd || 0).toFixed(3)} of $1.50 · each reply is a separate AI call</small>`;
  const canType = MTG.status === "waiting" && !sending;
  $("dtype").disabled = !canType;
  $("ptt").disabled = !(canType && VOICE);
  $("ptt").title = VOICE ? "Hold to dictate into the box (transcribed on this Mac only)" : "Dictation isn't set up on this Mac: type instead";
  $("dwrap").disabled = MTG.status !== "waiting" || sending;
  const here = new Set(MTG.participants || []);
  $("dwho").innerHTML = Object.entries(MTG.directory || {}).map(([pid, d]) =>
    `<button class="chip${here.has(pid) ? "" : " away"}" title="${here.has(pid) ? "" : "Not in the room: tagging brings them in"}" data-tag="${esc(d.name.split(" ")[0])}">@${esc(d.name.split(" ")[0])} <span>${esc(d.title)}</span></button>`).join("")
    + `<button class="chip" data-tag="all">@all</button>`;
  document.querySelectorAll("#dwho .chip").forEach((b) => b.onclick = () => insertTag(b.dataset.tag));
}
function insertTag(tag) {
  const box = $("dtype"); if (box.disabled) return;
  box.value = (box.value && !box.value.endsWith(" ") ? box.value + " " : box.value) + "@" + tag + " "; box.focus();
}
async function sendMessage(text) {
  sending = true; renderDock();
  try { setMeeting(await post({ op: "say", text })); }
  catch (e) { MTG.error = e.message; $("dtype").value = text; }
  sending = false; renderDock(); renderBubbles();
}
$("dtype").addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.value.trim()) { const t = e.target.value; e.target.value = ""; sendMessage(t); } });
$("dwrap").onclick = async () => { try { setMeeting(await post({ op: "wrap" })); } catch (e) { MTG.error = e.message; renderDock(); } };
$("dcancel").onclick = async () => {
  if (!confirm("Cancel this meeting? What it has spent so far stays recorded.")) return;
  try { setMeeting(await post({ op: "cancel" })); } catch (e) { MTG.error = e.message; renderDock(); }
};

// ---- hold to talk: recorded here, transcribed on this Mac (whisper.cpp) --------------------------------
let rec = null, recStream = null;
async function recStart(btn) {
  if (rec) return;
  try {
    recStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const chunks = []; rec = new MediaRecorder(recStream);
    rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    rec.chunks = chunks; rec.start(); btn.classList.add("rec");
  } catch (e) { alert("Microphone not available: " + e.message); rec = null; }
}
function recStop(btn, onText) {
  if (!rec) return;
  const r = rec; rec = null; btn.classList.remove("rec");
  r.onstop = async () => {
    recStream.getTracks().forEach((t) => t.stop());
    try {
      const out = await (await orgrovePost("/api/transcribe", { headers: { "Content-Type": r.mimeType || "audio/webm" },
        body: new Blob(r.chunks) })).json();
      if (out.error) throw new Error(out.error);
      onText(out.text || "");
    } catch (e) { if (MTG) { MTG.error = "Transcription failed: " + e.message; renderDock(); } else $("pnote").textContent = e.message; }
  };
  r.stop();
}
const dictate = (t) => { const box = $("dtype"); box.value = (box.value ? box.value.trimEnd() + " " : "") + t; box.focus(); };
const ptt = $("ptt");
ptt.addEventListener("pointerdown", (e) => { e.preventDefault(); if (!ptt.disabled) recStart(ptt); });
for (const ev of ["pointerup", "pointerleave", "pointercancel"]) ptt.addEventListener(ev, () => recStop(ptt, dictate));
addEventListener("keydown", (e) => {
  if (e.code !== "Space" || e.repeat || !MTG?.active || ["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName)) return;
  e.preventDefault(); if (!ptt.disabled) recStart(ptt);
});
addEventListener("keyup", (e) => { if (e.code === "Space" && rec) { e.preventDefault(); recStop(ptt, dictate); } });

// ---- calling a meeting --------------------------------------------------------------------------------
function openPicker() {
  if (MTG?.active) { go("Meeting"); return; }
  $("types").innerHTML = TYPES.map((t) => `<button class="type${t.id === pickType ? " on" : ""}" data-t="${esc(t.id)}"><b>${esc(t.title)}</b><span>${esc(t.purpose)}</span></button>`).join("")
    || `<div class="sub">Meeting types couldn't be loaded.</div>`;
  document.querySelectorAll("#types .type").forEach((b) => b.onclick = () => { pickType = b.dataset.t; openPicker(); });
  const t = TYPES.find((x) => x.id === pickType);
  $("tdetail").innerHTML = t ? `<div class="who">${t.participants.map((p) => `<i>${esc(p.name ? p.name.split(" ")[0] + " · " : "")}${esc(p.title)}</i>`).join("")}<i>You (CTO)</i></div>
    <b style="font-size:13px">Agenda</b><ol>${t.agenda.map((a) => `<li>${esc(a)}</li>`).join("")}</ol>` : "";
  $("opening").placeholder = t ? `Optional first message, e.g. "@${(t.participants[0]?.name || "Ananya").split(" ")[0]} ${t.opening}" Leave empty to start quietly.` : "What's on your mind?";
  $("startBtn").disabled = !t; $("pmic").disabled = !VOICE;
  $("pnote").textContent = "Reserves up to $1.50 of today's budget first. Only the people you tag answer; each reply is a separate AI call.";
  $("picker").classList.add("open");
}
$("callBtn").onclick = openPicker;
$("pcancel").onclick = () => $("picker").classList.remove("open");
const pmic = $("pmic");
pmic.addEventListener("pointerdown", (e) => { e.preventDefault(); if (!pmic.disabled) recStart(pmic); });
for (const ev of ["pointerup", "pointerleave", "pointercancel"]) pmic.addEventListener(ev, () => recStop(pmic, (t) => {
  const box = $("opening"); box.value = (box.value ? box.value.trimEnd() + " " : "") + t; }));
$("startBtn").onclick = async () => {
  $("startBtn").disabled = true; $("pnote").textContent = "Reserving budget and calling everyone in…";
  try { const m = await post({ op: "start", type: pickType, text: $("opening").value.trim() });
    $("picker").classList.remove("open"); $("opening").value = ""; setMeeting(m); }
  catch (e) { $("pnote").textContent = e.message; $("startBtn").disabled = false; }
};

// ---- the room's screen and the outcome --------------------------------------------------------------------
function drawMeetingScreen() {
  const g = ms.cv.getContext("2d"), W = ms.cv.width, H = ms.cv.height;
  g.fillStyle = "#1b1f27"; g.fillRect(0, 0, W, H);
  g.fillStyle = "#b9a2ff"; g.fillRect(0, 0, W, 12);
  if (!MTG?.active && MTG?.status !== "closed") {
    g.fillStyle = "#9aa4b2"; g.font = "600 46px Inter, sans-serif"; g.fillText("Meeting room · free", 60, 120);
    g.font = "500 30px Inter, sans-serif"; g.fillText("Call a meeting from the top bar.", 60, 180);
  } else if (MTG.status === "closed") {
    g.fillStyle = "#e9edf2"; g.font = "700 46px Inter, sans-serif"; g.fillText(`${MTG.title || "Meeting"} · done`, 60, 100);
    g.font = "500 28px Inter, sans-serif"; g.fillStyle = "#c9d1dc";
    (MTG.result?.decisions || []).slice(0, 6).forEach((d, i) => g.fillText(`• ${d.text}`.slice(0, 70), 60, 170 + i * 48));
    g.fillStyle = "#9aa4b2"; g.fillText(`${(MTG.result?.drafts || []).length} draft ticket(s) waiting for your approval`, 60, H - 50);
  } else {
    g.fillStyle = "#e9edf2"; g.font = "700 46px Inter, sans-serif"; g.fillText(MTG.title || "Meeting", 60, 100);
    const at = MTG.agenda_at ?? 0;
    (MTG.agenda_items || []).forEach((a, i) => {
      g.fillStyle = i < at ? "#5fd39b" : i === at ? "#ffffff" : "#7d8794";
      g.font = (i === at ? "700 " : "500 ") + "32px Inter, sans-serif";
      g.fillText(`${i < at ? "✓" : i === at ? "▸" : "·"}  ${a}`.slice(0, 64), 60, 180 + i * 56);
    });
  }
  ms.tex.needsUpdate = true;
}
function showResult(m) {
  const r = m.result || {}, box = $("result");
  box.innerHTML = `<h3>${esc(m.title || "Meeting")}: outcome</h3>
    ${r.minutes_url ? `<a href="${esc(r.minutes_url)}" target="_blank" rel="noopener">Full minutes on GitHub ↗</a>` : ""}
    <div class="sec">Decisions</div>${(r.decisions || []).map((d) => `<div class="row"><span>${esc(d.text)}</span><span>${esc(d.owner)}</span></div>`).join("") || `<div class="sub">none recorded</div>`}
    <div class="sec">Draft tickets · each needs your approval before any agent works on it</div>
    ${(r.drafts || []).map((d, i) => `<div class="row"><span>${r.draft_urls?.[i] ? `<a href="${esc(r.draft_urls[i])}" target="_blank" rel="noopener">${esc(d.title)}</a>` : esc(d.title)}</span><span>${esc(d.priority)}</span></div>`).join("") || `<div class="sub">none</div>`}
    <div class="sec">Minutes</div><pre>${esc((r.minutes || "").slice(0, 1800))}</pre>
    <div class="prow"><button class="btn" id="rclose">Back to work</button><span class="sub">cost $${Number(m.cost_usd || 0).toFixed(3)}</span></div>`;
  box.classList.add("open");
  $("rclose").onclick = () => { box.classList.remove("open"); go("Overview"); };
}
drawMeetingScreen();

// ---------- data ------------------------------------------------------------------------------------
async function refresh() {
  try {
    const s = await (await fetch("/api/state", { cache: "no-store" })).json();
    if (s.error) throw new Error(s.error);
    PEOPLE = s.people || PEOPLE;
    document.getElementById("err").textContent = s.stale ? "showing last good data" : "";
    apply(s);
  } catch (e) { document.getElementById("err").textContent = "Couldn't load data: " + e.message; }
}
refresh(); setInterval(refresh, 5000);   // local only: the server re-reads GitHub when the dispatcher has run
if (EDITION.meetings) { loadMeeting(); setInterval(() => { if (!MTG?.active) loadMeeting(); }, 60000); }   // Pro only

addEventListener("resize", () => {
  const a = aspect();
  Object.assign(camera, { left: -FRUSTUM * a / 2, right: FRUSTUM * a / 2, top: FRUSTUM / 2, bottom: -FRUSTUM / 2 });
  camera.updateProjectionMatrix(); renderer.setSize(innerWidth, innerHeight); labels.setSize(innerWidth, innerHeight);
});

// ---------- animation -------------------------------------------------------------------------------
const clock = new THREE.Clock(), still = matchMedia("(prefers-reduced-motion: reduce)").matches;
const ease = (x) => x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2;
renderer.setAnimationLoop(() => {
  const dt = Math.min(clock.getDelta(), 0.05), t = clock.elapsedTime;
  if (tween) {
    tween.t = Math.min(1, tween.t + dt / 1.4); const k = ease(tween.t);
    controls.target.lerpVectors(tween.from, tween.to, k);
    camera.position.copy(controls.target).add(tween.off);
    camera.zoom = tween.z0 + (tween.z1 - tween.z0) * k; camera.updateProjectionMatrix();
    if (tween.t >= 1) tween = null;
  }
  for (const p of people.values()) {
    if (p.path.length) {                               // walking: only because the real state changed
      const next = p.path[0], to = next.clone().sub(p.g.position); to.y = 0;
      const dist = to.length(), step = (still ? 99 : 2.6) * dt;
      if (dist <= step) { p.g.position.copy(next); p.path.shift(); if (!p.path.length) { p.pose = p.after.pose; p.facing = p.after.facing; if (MTG?.active) { renderBubbles(); renderDock(); } } }
      else { p.g.position.add(to.normalize().multiplyScalar(step)); p.g.rotation.y = Math.atan2(to.x, to.z); }
      const sw = still ? 0 : Math.sin(t * 9) * 0.55;
      p.legL.rotation.x = sw; p.legR.rotation.x = -sw; p.armL.rotation.x = -sw * 0.7; p.armR.rotation.x = sw * 0.7; p.body.position.y = Math.abs(Math.sin(t * 9)) * 0.05;
    } else {
      p.g.rotation.y += (p.facing - p.g.rotation.y) * Math.min(1, dt * 6);
      const cheering = p.cheerUntil > t;                 // a real change was celebrated (#121): stand and clap
      const seated = !cheering && (p.pose === "sit" || p.pose === "type");
      p.body.position.y = seated ? -0.42 : cheering && !still ? Math.abs(Math.sin(t * 8)) * 0.12 : 0;
      p.legL.rotation.x = p.legR.rotation.x = seated ? -1.45 : 0;
      const typing = p.pose === "type" && p.working && !still, m = still ? 0 : 1;
      let aL = seated ? -0.9 + (typing ? Math.sin(t * 14) * 0.08 : 0) : 0, aR = seated ? -0.9 + (typing ? Math.sin(t * 14 + 1.3) * 0.08 : 0) : 0, zL = 0, zR = 0;
      if (cheering) { aL = aR = -2.5; zL = -0.3 - Math.sin(t * 16) * 0.25 * m; zR = -zL; }
      else if (p.pose === "sip") aR = -1.3 - (Math.sin(t * 0.7 + p.role.length) > 0.6 ? 0.6 * m : 0);   // the occasional sip
      else if (p.pose === "play") aR = -0.7 + Math.sin(t * 4.4) * 0.6 * m;
      else if (p.pose === "read") aL = aR = -1.1;
      p.armL.rotation.x = aL; p.armR.rotation.x = aR; p.armL.rotation.z = zL; p.armR.rotation.z = zR;
    }
  }
  const players = pingpong.filter((p) => p && !p.path.length);   // a rally needs both players in place
  ball.visible = players.length === 2 && !still;
  if (ball.visible) ball.position.set(Math.sin(t * 2.2) * 1.5, 0.9 + Math.abs(Math.cos(t * 4.4)) * 0.4, 10.2);
  for (let i = fx.length - 1; i >= 0; i--) if (!fx[i](dt)) fx.splice(i, 1);
  controls.update();
  renderer.render(scene, camera);
  labels.render(scene, camera);
});
