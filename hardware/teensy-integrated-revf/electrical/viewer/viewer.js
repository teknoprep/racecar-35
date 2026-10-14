// Racecar-35 Rev F — single-file 3D viewer.
//
// Built into Racecar-RevF-3D.html by electrical/make_3d_share.py. Everything is inlined:
// three.js + GLTFLoader + OrbitControls + the Meshopt decoder + the model itself (base64
// GLB) + the hotspot list. No CDN, no server: the file opens offline from any folder.
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { MeshoptDecoder } from 'three/examples/jsm/libs/meshopt_decoder.module.js';
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js';

const DATA = window.RC3D_DATA;        // { glb: base64, parts: [...], meta: {...} }

const canvas = document.getElementById('view');
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.32;
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(35, 1, 1, 20000);
const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.minDistance = 60;
controls.maxDistance = 2600;
controls.maxPolarAngle = Math.PI * 0.495;

// ---------------------------------------------------------------- environment + lights
const pmrem = new THREE.PMREMGenerator(renderer);
scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.05).texture;
scene.environmentIntensity = 0.55;

const key = new THREE.DirectionalLight(0xffffff, 2.45);
key.position.set(-260, 420, 300);
key.castShadow = true;
key.shadow.mapSize.set(2048, 2048);
key.shadow.bias = -0.0006;
key.shadow.radius = 3;
scene.add(key);
scene.add(new THREE.HemisphereLight(0xdfe9ff, 0x2a2a30, 0.95));
const rim = new THREE.DirectionalLight(0x9fd4ff, 0.75);
rim.position.set(320, 240, -360);
scene.add(rim);

// ---------------------------------------------------------------- model
const model = new THREE.Group();
scene.add(model);
const anchors = new THREE.Group();
model.add(anchors);

// Green PCB / black plastic / gold metal, from the base colour only (KiCad writes no
// metallic/roughness factors, so three's defaults would render everything as rough metal).
function tuneMaterial(m) {
  m.metalness = 0.25;
  m.roughness = 0.55;
  m.envMapIntensity = 0.85;
  const c = m.color;
  const lum = 0.2126 * c.r + 0.7152 * c.g + 0.0722 * c.b;
  if (lum < 0.045) { m.metalness = 0.15; m.roughness = 0.62; }            // black plastic
  else if (c.r > 0.35 && c.g > 0.15 && c.g < 0.62 && c.b < 0.32) {         // gold / brass
    m.metalness = 0.95; m.roughness = 0.28; m.envMapIntensity = 1.3;
  } else if (lum > 0.55 && Math.abs(c.r - c.b) < 0.12) {                   // silver / tin
    m.metalness = 0.85; m.roughness = 0.32; m.envMapIntensity = 1.25;
  }
  m.side = THREE.FrontSide;
}

function bytesFromBase64(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out.buffer;
}

const loader = new GLTFLoader();
loader.setMeshoptDecoder(MeshoptDecoder);
loader.parse(bytesFromBase64(DATA.glb), '', (gltf) => {
  const root = gltf.scene;
  root.traverse((o) => {
    if (!o.isMesh) return;
    o.castShadow = true;
    o.receiveShadow = true;
    (Array.isArray(o.material) ? o.material : [o.material]).forEach(tuneMaterial);
  });
  // glTF is metres (Y up, board 150 x 155 mm); work in mm and centre on the board.
  root.scale.setScalar(1000);
  const box = new THREE.Box3().setFromObject(root);
  const size = box.getSize(new THREE.Vector3());
  const centre = box.getCenter(new THREE.Vector3());
  root.position.sub(centre);
  model.add(root);

  const span = Math.max(size.x, size.z);
  const corners = [];
  for (const x of [box.min.x, box.max.x]) for (const y of [box.min.y, box.max.y]) for (const z of [box.min.z, box.max.z]) {
    corners.push(new THREE.Vector3(x, y, z).sub(centre));
  }
  bb = { size, radius: size.length() / 2, box, corners };
  addAnchors(centre);
  addGround(span);
  computeHome();
  resetView();
  window.__rc3d = { box, size, centre, root, camera, controls, home, bb, probe: (d) => projectedFill(d, new THREE.Vector3()) };
  document.getElementById('loading').style.display = 'none';
  animateIntro();
});

// ---------------------------------------------------------------- camera framing
let intro = null;
const HOME_AZ = -0.72, HOME_EL = 0.80;
let home = { dist: 400, target: new THREE.Vector3(), pos: new THREE.Vector3(), az: HOME_AZ, el: HOME_EL };
let bb = { size: new THREE.Vector3(150, 20, 155), radius: 110 };

function camPos(az, el, dist) {
  return new THREE.Vector3(
    Math.cos(el) * Math.sin(az) * dist,
    Math.sin(el) * dist,
    Math.cos(el) * Math.cos(az) * dist,
  );
}

// Frame the board in the area actually free of the floating panels, so nothing important
// sits under the parts list on a laptop or gets cropped on a phone.
function panelShift(dist) {
  const panelPx = panelClosed || small ? 0 : Math.min(300, canvas.clientWidth * 0.35);
  const visW = 2 * dist * Math.tan((camera.fov * Math.PI) / 360) * Math.max(camera.aspect, 0.2);
  return (visW * (panelPx / 2)) / Math.max(canvas.clientWidth, 1);
}
function projectedFill(dist, target) {
  const probe = new THREE.PerspectiveCamera(camera.fov, camera.aspect, 0.1, 1e6);
  probe.position.copy(target).add(camPos(HOME_AZ, HOME_EL, dist));
  probe.lookAt(target);
  probe.updateMatrixWorld(true);
  probe.updateProjectionMatrix();
  let m = 0;
  if (bb.box) {
    for (const c of bb.corners) m = Math.max(m, Math.abs(c.clone().project(probe).x), Math.abs(c.clone().project(probe).y));
  } else {
    m = bb.radius / (dist * Math.tan((camera.fov * Math.PI) / 360));
  }
  return Math.max(m, 1e-4);
}
function computeHome() {
  const vFov = (camera.fov * Math.PI) / 180;
  const fitV = bb.radius / Math.sin(vFov / 2);
  const fitH = bb.radius / Math.sin(Math.atan(Math.tan(vFov / 2) * camera.aspect));
  let dist = Math.max(fitV, fitH) * 1.04;
  let right = new THREE.Vector3();
  for (let i = 0; i < 5; i++) {         // converge on a fill that clears the floating panels
    right.crossVectors(camera.up, camPos(HOME_AZ, HOME_EL, dist)).normalize();
    const target = right.clone().multiplyScalar(-panelShift(dist));
    const fill = projectedFill(dist, target);
    // Phones have the header stacked on top and the caveat at the bottom, so use more of
    // the width there; on a laptop the side panel eats ~300 px and 0.80 reads better.
    const want = small ? 0.95 : 0.80;
    dist = Math.min(Math.max(dist * fill / want, bb.radius * 1.05), bb.radius * 8);
  }
  home.dist = dist;
  right.crossVectors(camera.up, camPos(home.az, home.el, home.dist)).normalize();
  const shift = panelShift(home.dist);
  home.target.copy(right).multiplyScalar(-shift);
  home.pos.copy(camPos(home.az, home.el, home.dist)).addScaledVector(right, -shift);
}
function frame(t) {           // t = 0..1 orbit sweep used by the intro
  const az = HOME_AZ + t * 0.9;
  const dist = home.dist * (1 + t * 0.18);
  const right = new THREE.Vector3().crossVectors(camera.up, camPos(az, HOME_EL, dist)).normalize();
  const target = new THREE.Vector3().addScaledVector(right, -panelShift(dist));
  controls.target.copy(target);
  camera.position.copy(target).add(camPos(az, HOME_EL, dist));
}
function animateIntro() {
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) { controls.autoRotate = spin; return; }
  const start = performance.now();
  intro = () => {
    // sweeps from the wide, rotated establishing shot (t=1) IN to the framed home view (t=0)
    const k = Math.min(1, (performance.now() - start) / 2200);
    if (k < 1) frame(Math.pow(1 - k, 3));
    else { intro = null; controls.autoRotate = spin; }
  };
}

let spin = true;
controls.autoRotateSpeed = 0.6;
controls.addEventListener('start', () => { controls.autoRotate = false; syncButtons(); });

function resetView() {
  intro = null;
  computeHome();
  controls.target.copy(home.target);
  camera.position.copy(home.pos);
  controls.update();
}

// ---------------------------------------------------------------- ground shadow
function addGround(span) {
  const s = 512;
  const c = document.createElement('canvas');
  c.width = c.height = s;
  const g = c.getContext('2d');
  const grad = g.createRadialGradient(s / 2, s / 2, 0, s / 2, s / 2, s / 2);
  grad.addColorStop(0, 'rgba(0,0,0,0.55)');
  grad.addColorStop(0.55, 'rgba(0,0,0,0.22)');
  grad.addColorStop(1, 'rgba(0,0,0,0)');
  g.fillStyle = grad;
  g.fillRect(0, 0, s, s);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  const plane = new THREE.Mesh(
    new THREE.PlaneGeometry(span * 2.1, span * 2.1),
    new THREE.MeshBasicMaterial({ map: tex, transparent: true, depthWrite: false }),
  );
  plane.rotation.x = -Math.PI / 2;
  plane.position.y = -1.6;
  model.add(plane);
}

// ---------------------------------------------------------------- hotspots
// Part positions come from the board's own placement data (components.json) so the labels
// sit where KiCad put the parts; z is a chosen height above the PCB top.
const hotspots = [];
function addAnchors(centre) {
  const list = document.getElementById('parts');
  DATA.parts.forEach((p, i) => {
    const a = new THREE.Object3D();
    // board mm -> model mm (same axes; model already re-centred), then add offset
    a.position.set(p.x - centre.x, p.z + 1.6 - centre.y, p.y - centre.z);
    anchors.add(a);
    const el = document.createElement('button');
    el.className = 'part' + (p.key ? ' key' : '');
    el.innerHTML = `<span>${p.label}</span><em>${p.sub || ''}</em>`;
    el.onclick = () => focus(p, i);
    el.onmouseenter = () => { hl = i; };
    el.onmouseleave = () => { hl = null; };
    list.appendChild(el);
    const tag = document.createElement('div');
    tag.className = 'tag' + (p.key ? ' key' : '');
    tag.innerHTML = `<i></i><span>${p.label}</span>`;
    document.getElementById('tags').appendChild(tag);
    hotspots.push({ obj: a, tag, part: p });
  });
}

let hl = null;
const tmp = new THREE.Vector3();
function updateTags() {
  const show = labelsOn && !small;
  hotspots.forEach((h, i) => {
    h.obj.getWorldPosition(tmp);
    const v = tmp.clone().project(camera);
    const vis = show && v.z < 1;
    h.tag.style.display = vis ? 'block' : 'none';
    if (!vis) return;
    const r = canvas.getBoundingClientRect();
    const x = (v.x * 0.5 + 0.5) * r.width;
    const y = (-v.y * 0.5 + 0.5) * r.height;
    h.tag.style.transform = `translate(-50%,-50%) translate(${x}px,${y}px)`;
    h.tag.classList.toggle('hl', hl === i);
  });
}

function focus(p, i) {
  intro = null;
  controls.autoRotate = false;
  syncButtons();
  const a = hotspots[i].obj;
  const t = new THREE.Vector3();
  a.getWorldPosition(t);
  const az = HOME_AZ + 0.25, el = 0.62, dist = Math.max(90, Math.min(260, home.dist * 0.45));
  const from = camera.position.clone(), fromT = controls.target.clone();
  const to = camPosAt(t, az, el, dist);
  const right = new THREE.Vector3().crossVectors(camera.up, to.clone().sub(t)).normalize();
  to.addScaledVector(right, -panelShift(dist));
  t.addScaledVector(right, -panelShift(dist));
  const start = performance.now();
  intro = () => {
    const k = Math.min(1, (performance.now() - start) / 650);
    const e = 1 - Math.pow(1 - k, 3);
    camera.position.lerpVectors(from, to, e);
    controls.target.lerpVectors(fromT, t, e);
    if (k >= 1) intro = null;
  };
  document.querySelectorAll('#parts .part').forEach((el, n) => el.classList.toggle('on', n === i));
}
function camPosAt(target, az, el, dist) {
  return target.clone().add(camPos(az, el, dist));
}

// ---------------------------------------------------------------- UI wiring
let labelsOn = true;
let small = matchMedia('(max-width: 760px)').matches;
function syncButtons() {
  document.getElementById('b-spin').classList.toggle('on', controls.autoRotate);
  document.getElementById('b-labels').classList.toggle('on', labelsOn);
  document.getElementById('panel').classList.toggle('closed', panelClosed);
}
let panelClosed = small;
document.getElementById('b-spin').onclick = () => { intro = null; controls.autoRotate = !controls.autoRotate; syncButtons(); };
document.getElementById('b-labels').onclick = () => { labelsOn = !labelsOn; syncButtons(); };
document.getElementById('b-reset').onclick = () => { resetView(); document.querySelectorAll('#parts .part').forEach((el) => el.classList.remove('on')); syncButtons(); };
document.getElementById('b-panel').onclick = () => { panelClosed = !panelClosed; syncButtons(); computeHome(); };
addEventListener('resize', () => { small = matchMedia('(max-width: 760px)').matches; resize(); });
matchMedia('(max-width: 760px)').addEventListener('change', (e) => { small = e.matches; computeHome(); });

function resize() {
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w || !h) return;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  computeHome();
}
resize();
syncButtons();

// ---------------------------------------------------------------- loop
const clock = new THREE.Clock();
function tick() {
  requestAnimationFrame(tick);
  const dt = clock.getDelta();
  if (intro) intro();
  controls.autoRotate = controls.autoRotate && !intro;
  controls.update(dt);
  updateTags();
  renderer.render(scene, camera);
}
tick();
