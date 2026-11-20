// A foosball table for the break corner (owner, 2026-11-03), shared by both 3D looks. Built from plain shapes, in
// metres, centred on the origin: the goals at either end along x, the rods across it along z. Team A (red) is worked
// from the -z side, team B (blue) from +z, so each player stands at a long side with their handles in front of them.
// Data and three.js only: the caller places the group, blocks the floor and drives update() while two people play.

const ROD_X = [-0.525, -0.375, -0.225, -0.075, 0.075, 0.225, 0.375, 0.525];   // goalie A ... goalie B
const MEN = [1, 2, 3, 5, 5, 3, 2, 1];
const TEAM = ["A", "A", "B", "A", "B", "A", "B", "B"];                          // the classic 1-2-5-3 against 3-5-2-1
const HELD = { A: [3, 5], B: [4, 2] };                                           // each player's right and left hand: midfield, attack
export const FIELD_Y = 0.75, ROD_Y = 0.84, GRIP_Z = 0.53;                        // grips: the handle middles, either side

export function buildFoosball(THREE, { mat }) {
  const g = new THREE.Group(), rods = [];
  const add = (geo, m, x, y, z, parent = g) => { const o = new THREE.Mesh(geo, m); o.position.set(x, y, z); o.castShadow = o.receiveShadow = true; parent.add(o); return o; };
  const wood = mat(0x5a3d2b, { roughness: 0.55 }), dark = mat(0x22252b, { roughness: 0.5 }), chrome = mat(0xd8dbe0, { roughness: 0.18, metalness: 1 });
  const red = mat(0xc0392b, { roughness: 0.45 }), blue = mat(0x2f5fa8, { roughness: 0.45 }), skin = mat(0xf1d9b8, { roughness: 0.6 });

  // the field: green baize with white lines, drawn once
  const cv = document.createElement("canvas"); cv.width = 512; cv.height = 290;
  const c = cv.getContext("2d");
  c.fillStyle = "#2f7d4a"; c.fillRect(0, 0, 512, 290);
  for (let i = 0; i < 8; i++) { c.fillStyle = i % 2 ? "rgba(255,255,255,.04)" : "rgba(0,0,0,.04)"; c.fillRect(i * 64, 0, 64, 290); }
  c.strokeStyle = "rgba(255,255,255,.85)"; c.lineWidth = 4; c.strokeRect(8, 8, 496, 274);
  c.beginPath(); c.moveTo(256, 8); c.lineTo(256, 282); c.stroke(); c.beginPath(); c.arc(256, 145, 40, 0, Math.PI * 2); c.stroke();
  c.strokeRect(8, 95, 40, 100); c.strokeRect(464, 95, 40, 100);
  const tex = new THREE.CanvasTexture(cv); tex.colorSpace = THREE.SRGBColorSpace; tex.anisotropy = 8;
  add(new THREE.BoxGeometry(1.2, 0.02, 0.68), [dark, dark, new THREE.MeshStandardMaterial({ map: tex, roughness: 0.9 }), dark, dark, dark], 0, FIELD_Y - 0.01, 0);

  // the cabinet: two long sides, two ends with the goal mouths, legs and a stretcher
  for (const s of [-1, 1]) add(new THREE.BoxGeometry(1.4, 0.2, 0.04), wood, 0, 0.82, s * 0.36);
  for (const s of [-1, 1]) {
    add(new THREE.BoxGeometry(0.1, 0.2, 0.76), wood, s * 0.65, 0.82, 0);
    add(new THREE.BoxGeometry(0.012, 0.07, 0.2), dark, s * 0.6, FIELD_Y + 0.035, 0);                   // the goal mouth
  }
  add(new THREE.BoxGeometry(1.4, 0.06, 0.76), wood, 0, 0.7, 0);                                         // under the field
  for (const sx of [-1, 1]) for (const sz of [-1, 1]) add(new THREE.BoxGeometry(0.08, 0.68, 0.08), wood, sx * 0.62, 0.34, sz * 0.32);
  add(new THREE.BoxGeometry(1.2, 0.05, 0.05), wood, 0, 0.16, 0);
  for (const s of [-1, 1]) add(new THREE.CylinderGeometry(0.035, 0.035, 0.02, 20), dark, s * 0.69, 0.86, 0.26).rotation.z = Math.PI / 2;  // score counters

  // the rods, each with its little men; a rod turns (kick) and slides (follow the ball) about its own axis
  const rodGeo = new THREE.CylinderGeometry(0.008, 0.008, 1.2, 10), gripGeo = new THREE.CylinderGeometry(0.019, 0.017, 0.13, 14);
  const bodyGeo = new THREE.BoxGeometry(0.028, 0.075, 0.02), headGeo = new THREE.SphereGeometry(0.014, 10, 8), footGeo = new THREE.BoxGeometry(0.02, 0.06, 0.03);
  ROD_X.forEach((x, i) => {
    const team = TEAM[i], side = team === "A" ? -1 : 1, rod = new THREE.Group();
    rod.position.set(x, ROD_Y, 0); g.add(rod);
    add(rodGeo, chrome, 0, 0, 0, rod).rotation.x = Math.PI / 2;
    add(gripGeo, dark, 0, 0, side * GRIP_Z, rod).rotation.x = Math.PI / 2;
    for (let k = 0; k < MEN[i]; k++) {
      const z = MEN[i] === 1 ? 0 : ((k + 0.5) / MEN[i] - 0.5) * 0.58;
      add(bodyGeo, team === "A" ? red : blue, 0, -0.005, z, rod);
      add(headGeo, skin, 0, 0.045, z, rod);
      add(footGeo, team === "A" ? red : blue, 0, -0.065, z, rod);
    }
    rods.push({ rod, team, x, spin: 0, slide: 0 });
  });
  const ball = add(new THREE.SphereGeometry(0.017, 14, 10), mat(0xf7f3e8, { roughness: 0.4 }), 0, FIELD_Y + 0.017, 0);
  ball.visible = false;

  // Play: the ball runs a smooth looping path; the rods slide toward it and kick as it passes.
  function update(t, playing) {
    ball.visible = playing;
    for (const r of rods) {
      let spin = 0, slide = 0;
      if (playing) {
        const bx = Math.sin(t * 1.3) * 0.52 + Math.sin(t * 3.1) * 0.04, bz = Math.sin(t * 2.2 + 1) * 0.24;
        ball.position.set(bx, FIELD_Y + 0.017, bz);
        const near = Math.max(0, 1 - Math.abs(bx - r.x) / 0.09);
        spin = Math.sin(t * 14 + r.x * 9) * 0.9 * near + Math.sin(t * 1.7 + r.x * 5) * 0.12;
        slide = Math.max(-0.05, Math.min(0.05, (bz - 0) * 0.25));
      }
      r.spin += (spin - r.spin) * 0.25; r.slide += (slide - r.slide) * 0.1;
      r.rod.rotation.z = r.spin; r.rod.position.z = r.slide;
    }
  }
  // Where a player's hands go, in the table's own space: the two handles they work, and how far each has turned.
  const grips = (team) => HELD[team].map((i) => ({ x: ROD_X[i], y: ROD_Y, z: (team === "A" ? -1 : 1) * GRIP_Z + rods[i].slide, spin: rods[i].spin }));
  return { group: g, update, grips };
}
