// 3D WebGL knowledge-graph view for the agent Knowledge tab.
//
// One THREE.Points draw call for all nodes and one LineSegments draw call for all edges. Level of detail runs in
// the vertex shaders: nodes with few incoming refs are hidden until the camera is close to them, and hubs
// (many incoming refs) are drawn larger. The force layout runs in a Web Worker (kb-graph-layout-worker.js).
//
//   KBGraph3D.mount(el, { tooltipEl, onOpen(node), getDescription(node) -> Promise<string> })
//   KBGraph3D.setData({ nodes: [{id,title,type,tags,incoming,outgoing,thumbnail,color}],
//                       links: [{s,t,edgeType}] })      // s / t are indexes into nodes
//   KBGraph3D.applyFilters({ query, types:Set })        // -> { count }
//   KBGraph3D.focus(index) / unfocus() / dispose()
import * as THREE from 'three';
import { OrbitControls } from './vendor/OrbitControls.js';

const FOV = 55;
const WORKER_URL = 'kb-graph-layout-worker.js?v=2';
const LOD_MIN_NODES = 1500;      // below this the whole graph is always drawn
const LOD_SCALE = 0.8;           // importance needed per octave of distance beyond lodNear
const LABEL_POOL = 60;
const EDGE_LABEL_POOL = 20;
const THUMB_POOL = 64;
const THUMB_CACHE_MAX = 400;
const EDGE_WIDTH = 1.6;       // css px
const POLAR = 1.35;           // initial camera elevation (rad from the Y axis)

const NODE_VERT = `
attribute vec3 aColor;
attribute float aSize;
attribute float aImp;
attribute float aFlag;
uniform float uPixelScale;
uniform float uSizeScale;
uniform float uLodNear;
uniform float uLodScale;
varying vec3 vColor;
varying float vFlag;
void main() {
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    float dist = length(mv.xyz);
    float need = uLodScale * max(0.0, log2(dist / uLodNear));
    if (aImp < need && aFlag < 1.5) {
        gl_Position = vec4(2.0, 2.0, 2.0, 1.0);
        gl_PointSize = 0.0;
        return;
    }
    vColor = aColor;
    vFlag = aFlag;
    float px = aSize * uSizeScale * uPixelScale / max(dist, 0.001);
    gl_PointSize = clamp(px, 2.5, 90.0);
    gl_Position = projectionMatrix * mv;
    // dimmed nodes sit at the back of the depth range so they never hide highlighted ones
    if (aFlag > 0.5 && aFlag < 1.5) gl_Position.z = gl_Position.w * 0.9999;
}`;

const NODE_FRAG = `
varying vec3 vColor;
varying float vFlag;
void main() {
    vec2 p = gl_PointCoord * 2.0 - 1.0;
    float r = length(p);
    if (r > 1.0) discard;
    bool hl = vFlag > 1.5 && vFlag < 2.5;
    float ringW = hl ? 0.3 : 0.14;
    float lum = dot(vColor, vec3(0.299, 0.587, 0.114));
    vec3 border = lum > 0.92 ? vec3(0.61) : vec3(1.0);
    vec3 col = r > 1.0 - ringW ? border : vColor;
    if (hl && r > 1.0 - ringW) col = vec3(0.976, 0.451, 0.086);
    float a = (vFlag > 0.5 && vFlag < 1.5) ? 0.15 : 1.0;
    float alpha = a * smoothstep(1.0, 0.85, r);
    if (alpha < 0.02) discard;   // keep transparent pixels out of the depth buffer
    gl_FragColor = vec4(col, alpha);
}`;

// Edges are instanced screen-space quads (WebGL lines are stuck at 1px): one quad per edge, offset sideways by a
// fixed pixel width, trimmed against the near plane.
const EDGE_VERT = `
attribute vec3 aStart;
attribute vec3 aEnd;
attribute float aImp;
attribute float aFlag;
uniform vec2 uResolution;
uniform float uWidth;
uniform float uNear;
uniform float uLodNear;
uniform float uLodScale;
varying float vDist;
varying float vHalf;
varying float vFlag;
void main() {
    vec4 ms = modelViewMatrix * vec4(aStart, 1.0);
    vec4 me = modelViewMatrix * vec4(aEnd, 1.0);
    float dist = length((ms.xyz + me.xyz) * 0.5);
    float need = uLodScale * max(0.0, log2(dist / uLodNear));
    float nearZ = -uNear;
    bool hidden = (aImp < need && aFlag < 1.5) || (ms.z > nearZ && me.z > nearZ);
    if (hidden) {
        gl_Position = vec4(2.0, 2.0, 2.0, 1.0);
        vDist = 0.0; vHalf = 0.0; vFlag = 0.0;
        return;
    }
    if (ms.z > nearZ) ms = mix(ms, me, (nearZ - ms.z) / (me.z - ms.z));
    else if (me.z > nearZ) me = mix(me, ms, (nearZ - me.z) / (ms.z - me.z));
    vec4 cs = projectionMatrix * ms;
    vec4 ce = projectionMatrix * me;
    vec2 d = (ce.xy / ce.w - cs.xy / cs.w) * uResolution * 0.5;
    float len = length(d);
    vec2 dir = len > 0.0001 ? d / len : vec2(1.0, 0.0);
    vec2 nrm = vec2(-dir.y, dir.x);
    float w = aFlag > 1.5 ? uWidth * 1.8 : (aFlag > 0.5 ? uWidth * 0.6 : uWidth);
    float halfW = w * 0.5 + 1.0;   // +1px so the edge can be anti-aliased
    vec4 clip = position.x < 0.5 ? cs : ce;
    clip.xy += nrm * position.y * halfW / (uResolution * 0.5) * clip.w;
    vDist = position.y * halfW;
    vHalf = w * 0.5;
    vFlag = aFlag;
    gl_Position = clip;
}`;

const EDGE_FRAG = `
uniform vec3 uColor;
uniform float uBase;
varying float vDist;
varying float vHalf;
varying float vFlag;
void main() {
    float cov = clamp(vHalf + 0.5 - abs(vDist), 0.0, 1.0);
    float a = vFlag > 1.5 ? 0.9 : (vFlag > 0.5 ? 0.05 : uBase);
    vec3 c = vFlag > 1.5 ? vec3(0.976, 0.451, 0.086) : uColor;
    gl_FragColor = vec4(c, a * cov);
}`;

let G = null;   // the single active instance

function createInstance(container, opts) {
    const pr = Math.min(window.devicePixelRatio || 1, 2);
    const renderer = new THREE.WebGLRenderer({ antialias: false, alpha: true, powerPreference: 'high-performance' });
    renderer.setPixelRatio(pr);
    renderer.setClearColor(0x000000, 0);

    container.innerHTML = '';
    if (getComputedStyle(container).position === 'static') container.style.position = 'relative';
    const canvas = renderer.domElement;
    canvas.style.cssText = 'display:block;width:100%;height:100%;outline:none;';
    container.appendChild(canvas);

    const overlay = document.createElement('div');
    overlay.className = 'kb3d-overlay';
    container.appendChild(overlay);

    const makePool = (count, cls) => {
        const arr = [];
        for (let k = 0; k < count; k++) {
            const el = document.createElement('div');
            el.className = cls;
            el.style.display = 'none';
            overlay.appendChild(el);
            arr.push({ el, i: -1, e: -1 });
        }
        return arr;
    };
    const labelPool = makePool(LABEL_POOL, 'kb3d-label');
    const edgeLabelPool = makePool(EDGE_LABEL_POOL, 'kb3d-label kb3d-edge-label');

    const balloon = document.createElement('div');
    balloon.className = 'kb3d-balloon';
    balloon.innerHTML = '<div class="kb3d-balloon-title"></div><div class="kb3d-balloon-type"></div>'
        + '<div class="kb3d-balloon-desc"></div><div class="kb3d-balloon-hint">Click to open</div>';
    const balloonTitle = balloon.querySelector('.kb3d-balloon-title');
    const balloonType = balloon.querySelector('.kb3d-balloon-type');
    const balloonDesc = balloon.querySelector('.kb3d-balloon-desc');
    overlay.appendChild(balloon);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(FOV, 1, 0.5, 1e5);
    camera.position.set(0, 300 * Math.cos(POLAR), 300 * Math.sin(POLAR));
    const controls = new OrbitControls(camera, canvas);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.zoomToCursor = true;
    controls.autoRotate = true;
    controls.autoRotateSpeed = 0.6;

    // ---- graph state ----
    let nodes = [], links = [];
    let n = 0, m = 0;
    let imp = null, sizes = null, flags = null, base = null;
    let linkS = null, linkT = null;
    let adjOff = null, adjEdge = null;
    let byImp = null, thumbNodes = null;
    let hay = null;
    let idToIndex = new Map();
    let nodeGeo = null, edgeGeo = null, points = null, lines = null;
    let nodeMat = null, edgeMat = null;
    let worker = null;
    let R = 100;
    let layoutReady = false;
    let userInteracted = false;
    let focusIdx = -1;
    let hadQuery = false;
    let tween = null;
    let disposed = false;

    const pixelScaleUniform = { value: 1 };
    const sizeScaleUniform = { value: 1 };
    const lodNearUniform = { value: 100 };
    const lodScaleUniform = { value: 0 };
    const resolutionUniform = { value: new THREE.Vector2(1, 1) };   // drawing buffer, device px
    const widthUniform = { value: EDGE_WIDTH * pr };                // device px
    const nearUniform = { value: camera.near };
    let W = 0, H = 0, pixelScaleCss = 1;

    controls.addEventListener('start', () => { userInteracted = true; tween = null; });

    // ---- helpers ----
    const _v = new THREE.Vector3();
    const _m4 = new THREE.Matrix4();

    const nodeDist = (i, pos) => {
        const cp = camera.position;
        const dx = pos[3 * i] - cp.x, dy = pos[3 * i + 1] - cp.y, dz = pos[3 * i + 2] - cp.z;
        return Math.sqrt(dx * dx + dy * dy + dz * dz);
    };
    const lodVisible = (i, dist) => {
        if (flags[i] > 1.5) return true;
        return imp[i] >= lodScaleUniform.value * Math.max(0, Math.log2(dist / lodNearUniform.value));
    };
    // Projects a world point to CSS pixels; returns null when behind the camera.
    const project = (x, y, z) => {
        _v.set(x, y, z).applyMatrix4(camera.matrixWorldInverse);
        if (_v.z > -camera.near) return null;
        _v.applyMatrix4(camera.projectionMatrix);
        return { x: (_v.x * 0.5 + 0.5) * W, y: (-_v.y * 0.5 + 0.5) * H };
    };
    const onScreen = (p, pad) => p && p.x > -pad && p.x < W + pad && p.y > -pad && p.y < H + pad;
    const pos = () => nodeGeo.attributes.position.array;

    // ---- build / teardown of graph objects ----
    function clearGraph() {
        if (worker) { worker.terminate(); worker = null; }
        if (points) { scene.remove(points); nodeGeo.dispose(); nodeMat.dispose(); points = null; }
        if (lines) { scene.remove(lines); edgeGeo.dispose(); edgeMat.dispose(); lines = null; }
        for (const s of sprites) s.visible = false;
        for (const p of labelPool.concat(edgeLabelPool)) { p.el.style.display = 'none'; p.i = -1; p.e = -1; }
        focusIdx = -1; tween = null; hadQuery = false;
        balloonOn(false);
        layoutReady = false;
    }

    function setData(d) {
        clearGraph();
        nodes = d.nodes || [];
        links = d.links || [];
        n = nodes.length; m = links.length;
        if (!n) return;

        idToIndex = new Map();
        imp = new Float32Array(n); sizes = new Float32Array(n);
        flags = new Float32Array(n); base = new Float32Array(n);
        const colors = new Float32Array(n * 3);
        const col = new THREE.Color();
        const thumbs = [];
        for (let i = 0; i < n; i++) {
            const nd = nodes[i];
            idToIndex.set(nd.id, i);
            imp[i] = Math.log2(1 + (nd.incoming || 0));
            let s = Math.min(12, 1.5 + 1.1 * imp[i]);
            if (nd.thumbnail) { s = Math.max(s, 4.5); thumbs.push(i); }
            sizes[i] = s;
            col.set(nd.color || '#9ca3af');
            colors[3 * i] = col.r; colors[3 * i + 1] = col.g; colors[3 * i + 2] = col.b;
        }
        thumbNodes = thumbs;
        hay = null;

        // Hubs first, for label selection.
        byImp = Array.from({ length: n }, (_, i) => i).filter(i => imp[i] > 0)
            .sort((a, b) => imp[b] - imp[a]).slice(0, 4000);
        if (byImp.length < 200) {
            byImp = Array.from({ length: n }, (_, i) => i).sort((a, b) => imp[b] - imp[a]).slice(0, 4000);
        }

        // Adjacency (edge ids per node) for focus highlighting.
        linkS = new Uint32Array(m); linkT = new Uint32Array(m);
        const deg = new Uint32Array(n + 1);
        for (let e = 0; e < m; e++) {
            linkS[e] = links[e].s; linkT[e] = links[e].t;
            deg[linkS[e]]++; deg[linkT[e]]++;
        }
        adjOff = new Uint32Array(n + 1);
        for (let i = 0; i < n; i++) adjOff[i + 1] = adjOff[i] + deg[i];
        adjEdge = new Uint32Array(adjOff[n]);
        const fill = adjOff.slice(0, n);
        for (let e = 0; e < m; e++) { adjEdge[fill[linkS[e]]++] = e; adjEdge[fill[linkT[e]]++] = e; }

        nodeGeo = new THREE.BufferGeometry();
        nodeGeo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(n * 3), 3));
        nodeGeo.setAttribute('aColor', new THREE.BufferAttribute(colors, 3));
        nodeGeo.setAttribute('aSize', new THREE.BufferAttribute(sizes, 1));
        nodeGeo.setAttribute('aImp', new THREE.BufferAttribute(imp, 1));
        nodeGeo.setAttribute('aFlag', new THREE.BufferAttribute(flags, 1));
        nodeMat = new THREE.ShaderMaterial({
            vertexShader: NODE_VERT, fragmentShader: NODE_FRAG, transparent: true, depthTest: true, depthWrite: true,
            uniforms: {
                uPixelScale: pixelScaleUniform, uSizeScale: sizeScaleUniform,
                uLodNear: lodNearUniform, uLodScale: lodScaleUniform,
            },
        });
        points = new THREE.Points(nodeGeo, nodeMat);
        points.frustumCulled = false;
        points.renderOrder = 2;
        points.visible = false;

        const edgeImp = new Float32Array(m);
        for (let e = 0; e < m; e++) edgeImp[e] = Math.min(imp[linkS[e]], imp[linkT[e]]);
        edgeGeo = new THREE.InstancedBufferGeometry();
        // quad corners: x = which end (0 start / 1 end), y = side (-1 / +1)
        edgeGeo.setAttribute('position', new THREE.BufferAttribute(new Float32Array([0, -1, 0, 0, 1, 0, 1, -1, 0, 1, 1, 0]), 3));
        edgeGeo.setIndex([0, 2, 1, 1, 2, 3]);
        edgeGeo.instanceCount = m;
        edgeGeo.setAttribute('aStart', new THREE.InstancedBufferAttribute(new Float32Array(m * 3), 3));
        edgeGeo.setAttribute('aEnd', new THREE.InstancedBufferAttribute(new Float32Array(m * 3), 3));
        edgeGeo.setAttribute('aImp', new THREE.InstancedBufferAttribute(edgeImp, 1));
        edgeGeo.setAttribute('aFlag', new THREE.InstancedBufferAttribute(new Float32Array(m), 1));
        edgeMat = new THREE.ShaderMaterial({
            vertexShader: EDGE_VERT, fragmentShader: EDGE_FRAG, transparent: true, depthTest: false, depthWrite: false,
            side: THREE.DoubleSide,
            uniforms: {
                uResolution: resolutionUniform, uWidth: widthUniform, uNear: nearUniform,
                uLodNear: lodNearUniform, uLodScale: lodScaleUniform,
                uColor: { value: new THREE.Color() }, uBase: { value: 0.5 },
            },
        });
        lines = new THREE.Mesh(edgeGeo, edgeMat);
        lines.frustumCulled = false;
        lines.renderOrder = 1;
        lines.visible = false;
        scene.add(lines);
        scene.add(points);

        lodScaleUniform.value = n < LOD_MIN_NODES ? 0 : LOD_SCALE;
        applyTheme(true);

        worker = new Worker(new URL(WORKER_URL, import.meta.url));
        worker.onmessage = onLayout;
        const linkBuf = new Uint32Array(m * 2);
        for (let e = 0; e < m; e++) { linkBuf[2 * e] = linkS[e]; linkBuf[2 * e + 1] = linkT[e]; }
        worker.postMessage({ n, links: linkBuf }, [linkBuf.buffer]);
    }

    function onLayout(ev) {
        if (disposed || !points) return;
        const { positions, done } = ev.data;
        const pa = pos();
        pa.set(positions);
        nodeGeo.attributes.position.needsUpdate = true;

        const es = edgeGeo.attributes.aStart.array, ee = edgeGeo.attributes.aEnd.array;
        for (let e = 0; e < m; e++) {
            const s = linkS[e] * 3, t = linkT[e] * 3, o = e * 3;
            es[o] = pa[s]; es[o + 1] = pa[s + 1]; es[o + 2] = pa[s + 2];
            ee[o] = pa[t]; ee[o + 1] = pa[t + 1]; ee[o + 2] = pa[t + 2];
        }
        edgeGeo.attributes.aStart.needsUpdate = true;
        edgeGeo.attributes.aEnd.needsUpdate = true;

        if (!layoutReady) { points.visible = true; lines.visible = true; layoutReady = true; }
        if (!userInteracted) fitCamera();
        else updateScale();
    }

    // Centroid + RMS radius of the layout; sets the scale-dependent uniforms.
    let center = new THREE.Vector3();
    function updateScale() {
        const pa = pos();
        let cx = 0, cy = 0, cz = 0;
        for (let i = 0; i < n; i++) { cx += pa[3 * i]; cy += pa[3 * i + 1]; cz += pa[3 * i + 2]; }
        cx /= n; cy /= n; cz /= n;
        let sq = 0;
        for (let i = 0; i < n; i++) {
            const dx = pa[3 * i] - cx, dy = pa[3 * i + 1] - cy, dz = pa[3 * i + 2] - cz;
            sq += dx * dx + dy * dy + dz * dz;
        }
        R = Math.max(50, Math.sqrt(sq / n) * 1.7);
        center.set(cx, cy, cz);
        sizeScaleUniform.value = R / 120;
        lodNearUniform.value = R * 0.35;
        camera.near = R * 0.005;
        nearUniform.value = camera.near;
        camera.far = R * 60;
        camera.updateProjectionMatrix();
        controls.minDistance = R * 0.01;
        controls.maxDistance = R * 10;
    }

    // Unit offset (target -> camera): keeps whatever angle the user is currently viewing from.
    function viewDir(camPos, target) {
        const d = camPos.clone().sub(target);
        return d.lengthSq() < 1e-6 ? new THREE.Vector3(0.3, 0.4, 1).normalize() : d.normalize();
    }

    function fitCamera() {
        updateScale();
        const dir = viewDir(camera.position, controls.target);
        controls.target.copy(center);
        camera.position.copy(center).addScaledVector(dir, R * 2.2);
        controls.update();
    }

    // ---- theme ----
    let isDark = null;
    function applyTheme(force) {
        const dark = document.documentElement.classList.contains('dark');
        if (!force && dark === isDark) return;
        isDark = dark;
        if (!edgeMat) return;
        edgeMat.uniforms.uColor.value.set(dark ? '#b6c4d8' : '#6b7280');
        edgeMat.uniforms.uBase.value = dark ? 0.55 : 0.5;
    }

    // ---- flags (filters + focus) ----
    function recomputeFlags() {
        if (!n) return;
        if (focusIdx < 0) {
            flags.set(base);
        } else {
            for (let i = 0; i < n; i++) flags[i] = base[i] === 2 ? 2 : 1;
            flags[focusIdx] = 2;
            for (let k = adjOff[focusIdx]; k < adjOff[focusIdx + 1]; k++) {
                const e = adjEdge[k];
                const o = linkS[e] === focusIdx ? linkT[e] : linkS[e];
                if (flags[o] !== 2) flags[o] = 3;
            }
        }
        const ef = edgeGeo.attributes.aFlag.array;
        for (let e = 0; e < m; e++) {
            let f;
            if (focusIdx >= 0) f = (linkS[e] === focusIdx || linkT[e] === focusIdx) ? 2 : 1;
            else f = (flags[linkS[e]] === 1 && flags[linkT[e]] === 1) ? 1 : 0;
            ef[e] = f;
        }
        nodeGeo.attributes.aFlag.needsUpdate = true;
        edgeGeo.attributes.aFlag.needsUpdate = true;
        labelsDirty = true;
    }

    // ---- filters ----
    function applyFilters(f) {
        if (!n) return { count: 0 };
        const q = ((f && f.query) || '').toLowerCase().trim();
        const types = f && f.types;
        const hasT = !!(types && types.size);
        let count = 0, best = -1, bestScore = -1;
        if (!q && !hasT) {
            base.fill(0);
        } else {
            if (q && !hay) {
                hay = new Array(n);
                for (let i = 0; i < n; i++) {
                    const nd = nodes[i];
                    hay[i] = ((nd.title || '') + '\u0001' + nd.id + '\u0001' + (nd.tags || []).join(' ') + '\u0001' + (nd.type || '')).toLowerCase();
                }
            }
            for (let i = 0; i < n; i++) {
                const passT = !hasT || types.has(nodes[i].type);
                const passS = !q || hay[i].includes(q);
                if (passT && passS) {
                    base[i] = q ? 2 : 3;
                    count++;
                    if (q) {
                        const nd = nodes[i];
                        const t = (nd.title || '').toLowerCase();
                        // exact title/slug beats prefix beats the most-referenced match
                        const score = (t === q || nd.id.toLowerCase() === q ? 1e6 : (t.startsWith(q) ? 1e3 : 0)) + imp[i];
                        if (score > bestScore) { bestScore = score; best = i; }
                    }
                } else {
                    base[i] = 1;
                }
            }
        }
        if (q) {
            hadQuery = true;
            if (best >= 0) {
                if (best === focusIdx) recomputeFlags(); else focus(best);
            } else {
                clearFocus();
            }
        } else if (hadQuery) {
            hadQuery = false;
            unfocus();
        } else {
            recomputeFlags();
        }
        return { count };
    }

    // ---- balloon visibility: shown while hovering the focused node (or the balloon), fades out slowly after ----
    const HIDE_DELAY = 450;   // ms the pointer may stray (e.g. travel from the node up to the balloon) before it fades
    let hideTimer = 0;
    function balloonOn(on) {
        clearTimeout(hideTimer);
        hideTimer = 0;
        balloon.classList.toggle('is-on', on);
    }
    function scheduleBalloonHide() {
        if (hideTimer || !balloon.classList.contains('is-on')) return;
        hideTimer = setTimeout(() => { hideTimer = 0; balloon.classList.remove('is-on'); }, HIDE_DELAY);
    }
    balloon.addEventListener('pointerenter', () => { if (focusIdx >= 0) balloonOn(true); });
    balloon.addEventListener('pointerleave', scheduleBalloonHide);

    // ---- focus / balloon ----
    const descCache = new Map();
    function prepareBalloon(i) {
        const nd = nodes[i];
        balloonTitle.textContent = nd.title || nd.id;
        balloonType.textContent = nd.type || '';
        balloonType.style.display = nd.type ? '' : 'none';
        const cached = descCache.get(nd.id);
        balloonDesc.textContent = cached !== undefined ? cached : 'Loading…';
        if (cached === undefined && opts.getDescription) {
            Promise.resolve(opts.getDescription(nd)).catch(() => '').then(text => {
                text = text || '';
                descCache.set(nd.id, text);
                if (!disposed && focusIdx === i) balloonDesc.textContent = text || 'No description.';
            });
        }
    }

    function focus(i) {
        if (!layoutReady || i < 0 || i >= n) return;
        focusIdx = i;
        userInteracted = true;
        controls.autoRotate = false;
        controls.enablePan = false;          // keep the focused node as the fixed pivot
        controls.zoomToCursor = false;
        recomputeFlags();
        prepareBalloon(i);

        const pa = pos();
        const p = new THREE.Vector3(pa[3 * i], pa[3 * i + 1], pa[3 * i + 2]);
        const closeDist = R * 0.18;
        const c1 = camera.position.clone(), t1 = controls.target.clone();
        let c2;
        if (c1.distanceTo(p) > closeDist * 1.6) {
            c2 = viewDir(c1, t1).multiplyScalar(closeDist).add(p);
        } else {
            c2 = c1.clone().add(p).sub(t1);   // already close: just recenter
        }
        tween = { t0: performance.now(), dur: 700, t1, t2: p, c1, c2 };
    }

    function clearFocus() {
        focusIdx = -1;
        balloonOn(false);
        controls.autoRotate = true;
        controls.enablePan = true;
        controls.zoomToCursor = true;
        recomputeFlags();
    }

    function unfocus() {
        if (focusIdx < 0) { recomputeFlags(); return; }
        clearFocus();
    }

    // Reset the view so the whole graph is visible (also drops the focus).
    function fit() {
        if (!layoutReady) return;
        clearFocus();
        updateScale();
        userInteracted = true;
        const c1 = camera.position.clone(), t1 = controls.target.clone();
        const c2 = viewDir(c1, t1).multiplyScalar(R * 2.2).add(center);
        tween = { t0: performance.now(), dur: 800, t1, t2: center.clone(), c1, c2 };
    }

    balloon.addEventListener('click', () => {
        if (focusIdx >= 0 && opts.onOpen) opts.onOpen(nodes[focusIdx]);
    });

    // ---- picking ----
    function pick(px, py) {
        if (!layoutReady) return -1;
        const pa = pos();
        const me = _m4.multiplyMatrices(camera.projectionMatrix, camera.matrixWorldInverse).elements;
        let best = -1, bestW = Infinity;
        for (let i = 0; i < n; i++) {
            if (flags[i] === 1) continue;
            const x = pa[3 * i], y = pa[3 * i + 1], z = pa[3 * i + 2];
            const w = me[3] * x + me[7] * y + me[11] * z + me[15];
            if (w <= camera.near || w >= bestW) continue;
            const dist = nodeDist(i, pa);
            if (!lodVisible(i, dist)) continue;
            const sx = ((me[0] * x + me[4] * y + me[8] * z + me[12]) / w * 0.5 + 0.5) * W;
            const sy = (-(me[1] * x + me[5] * y + me[9] * z + me[13]) / w * 0.5 + 0.5) * H;
            const rad = Math.min(90, Math.max(2.5, sizes[i] * sizeScaleUniform.value * pixelScaleCss / dist)) / 2 + 3;
            const dx = sx - px, dy = sy - py;
            if (dx * dx + dy * dy <= rad * rad) { best = i; bestW = w; }
        }
        return best;
    }

    // ---- pointer events ----
    let down = null, hoverXY = null, hoverDirty = false, hoverIdx = -1;
    const tooltip = opts.tooltipEl || null;
    const hideTooltip = () => { if (tooltip) tooltip.classList.add('hidden'); hoverIdx = -1; };
    canvas.addEventListener('pointerdown', e => {
        down = { x: e.clientX, y: e.clientY, t: performance.now(), button: e.button };
        hideTooltip();
    });
    canvas.addEventListener('pointerup', e => {
        const d = down; down = null;
        if (!d || d.button !== 0) return;
        if (Math.hypot(e.clientX - d.x, e.clientY - d.y) >= 4 || performance.now() - d.t > 600) return;
        const r = canvas.getBoundingClientRect();
        const hit = pick(e.clientX - r.left, e.clientY - r.top);
        if (hit >= 0) {
            focus(hit);
            balloonOn(true);
            hoverXY = { x: e.clientX, y: e.clientY };
            hoverDirty = true;
        } else {
            unfocus();
        }
    });
    canvas.addEventListener('pointermove', e => {
        if (down) return;
        hoverXY = { x: e.clientX, y: e.clientY };
        hoverDirty = true;
    });
    canvas.addEventListener('pointerleave', () => { hoverXY = null; hoverDirty = false; hideTooltip(); canvas.style.cursor = ''; scheduleBalloonHide(); });

    function doHover() {
        if (!hoverXY) return;
        const r = canvas.getBoundingClientRect();
        const hit = pick(hoverXY.x - r.left, hoverXY.y - r.top);
        canvas.style.cursor = hit >= 0 ? 'pointer' : '';
        if (hit >= 0 && hit === focusIdx) { hideTooltip(); balloonOn(true); return; }   // the balloon replaces the tooltip
        scheduleBalloonHide();
        if (!tooltip) return;
        if (hit < 0) { hideTooltip(); return; }
        if (hit !== hoverIdx) {
            const nd = nodes[hit];
            const rows = [nd.title || nd.id, 'Type: ' + (nd.type || '—')];
            if (nd.tags && nd.tags.length) rows.push('Tags: ' + nd.tags.join(', '));
            rows.push('In: ' + (nd.incoming || 0) + ' | Out: ' + (nd.outgoing || 0));
            tooltip.style.whiteSpace = 'pre';
            tooltip.textContent = rows.join('\n');
            hoverIdx = hit;
        }
        const cr = container.getBoundingClientRect();
        tooltip.style.position = 'absolute';
        tooltip.style.left = (hoverXY.x - cr.left + 10) + 'px';
        tooltip.style.top = (hoverXY.y - cr.top - 10) + 'px';
        tooltip.classList.remove('hidden');
    }

    const onKey = (e) => {
        if (e.key !== 'Escape' || focusIdx < 0) return;
        const t = e.target;
        if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
        if (!container.clientWidth) return;
        unfocus();
    };
    document.addEventListener('keydown', onKey);

    // ---- thumbnails (sprite pool) ----
    const sprites = [];
    for (let k = 0; k < THUMB_POOL; k++) {
        const s = new THREE.Sprite(new THREE.SpriteMaterial({ transparent: true, alphaTest: 0.5 }));
        s.visible = false;
        s.renderOrder = 3;
        scene.add(s);
        sprites.push(s);
    }
    const thumbCache = new Map();   // url -> Promise<Texture|null>
    function loadThumb(url) {
        let p = thumbCache.get(url);
        if (p) return p;
        p = new Promise(resolve => {
            const img = new Image();
            img.crossOrigin = 'anonymous';
            img.onload = () => {
                const S = 96, c = document.createElement('canvas');
                c.width = c.height = S;
                const x = c.getContext('2d');
                x.save();
                x.beginPath(); x.arc(S / 2, S / 2, S / 2 - 3, 0, Math.PI * 2); x.clip();
                const k = Math.max(S / img.width, S / img.height);
                x.drawImage(img, (S - img.width * k) / 2, (S - img.height * k) / 2, img.width * k, img.height * k);
                x.restore();
                x.lineWidth = 5; x.strokeStyle = '#fff';
                x.beginPath(); x.arc(S / 2, S / 2, S / 2 - 3, 0, Math.PI * 2); x.stroke();
                const t = new THREE.CanvasTexture(c);
                t.colorSpace = THREE.SRGBColorSpace;
                resolve(t);
            };
            img.onerror = () => resolve(null);
            img.src = url;
        });
        thumbCache.set(url, p);
        if (thumbCache.size > THUMB_CACHE_MAX) {
            const oldest = thumbCache.keys().next().value;
            thumbCache.get(oldest).then(t => t && t.dispose());
            thumbCache.delete(oldest);
        }
        return p;
    }

    function selectThumbs() {
        if (!layoutReady || !thumbNodes.length) return;
        const pa = pos();
        const cand = [];
        for (const i of thumbNodes) {
            if (flags[i] === 1) continue;
            const dist = nodeDist(i, pa);
            if (!lodVisible(i, dist)) continue;
            const p = project(pa[3 * i], pa[3 * i + 1], pa[3 * i + 2]);
            if (!onScreen(p, 40)) continue;
            cand.push([dist, i]);
        }
        cand.sort((a, b) => a[0] - b[0]);
        for (let k = 0; k < sprites.length; k++) {
            const s = sprites[k], c = cand[k];
            if (!c) { s.visible = false; s.userData.i = -1; continue; }
            const i = c[1], nd = nodes[i];
            // nudge toward the camera so the sprite wins the depth test against its own node's point
            const nudge = 0.004;
            const cp = camera.position;
            s.position.set(pa[3 * i] + (cp.x - pa[3 * i]) * nudge, pa[3 * i + 1] + (cp.y - pa[3 * i + 1]) * nudge,
                           pa[3 * i + 2] + (cp.z - pa[3 * i + 2]) * nudge);
            const d = sizes[i] * sizeScaleUniform.value;
            s.scale.set(d, d, 1);
            if (s.userData.url !== nd.thumbnail) {
                s.userData.url = nd.thumbnail;
                s.visible = false;
                loadThumb(nd.thumbnail).then(tex => {
                    if (s.userData.url !== nd.thumbnail || !tex) return;
                    s.material.map = tex;
                    s.material.needsUpdate = true;
                    s.visible = true;
                });
            } else if (s.material.map) {
                s.visible = true;
            }
        }
    }

    // ---- labels ----
    let labelsDirty = true;
    const CELL_W = 90, CELL_H = 18;

    function selectLabels() {
        for (const p of labelPool) { p.i = -1; p.el.style.display = 'none'; }
        for (const p of edgeLabelPool) { p.e = -1; p.el.style.display = 'none'; }
        if (!layoutReady) return;
        const pa = pos();

        const order = [];
        if (focusIdx >= 0) {
            order.push(focusIdx);
            const nb = [];
            for (let k = adjOff[focusIdx]; k < adjOff[focusIdx + 1] && nb.length < 30; k++) {
                const e = adjEdge[k];
                nb.push(linkS[e] === focusIdx ? linkT[e] : linkS[e]);
            }
            for (const o of nb) order.push(o);
        }
        for (const i of byImp) order.push(i);

        const used = new Set(), grid = new Set();
        let slot = 0;
        for (const i of order) {
            if (slot >= labelPool.length) break;
            if (used.has(i) || flags[i] === 1) continue;
            used.add(i);
            if (!lodVisible(i, nodeDist(i, pa))) continue;
            const p = project(pa[3 * i], pa[3 * i + 1], pa[3 * i + 2]);
            if (!onScreen(p, 0)) continue;
            const key = Math.floor(p.x / CELL_W) * 100003 + Math.floor(p.y / CELL_H);
            if (grid.has(key) && i !== focusIdx) continue;
            grid.add(key);
            const lp = labelPool[slot++];
            lp.i = i;
            const nd = nodes[i];
            const text = (nd.title || nd.id).slice(0, 22);
            if (lp.el.textContent !== text) lp.el.textContent = text;
            lp.el.classList.toggle('kb3d-label-focus', i === focusIdx);
        }

        if (focusIdx >= 0) {
            let es = 0;
            for (let k = adjOff[focusIdx]; k < adjOff[focusIdx + 1] && es < edgeLabelPool.length; k++) {
                const e = adjEdge[k];
                const t = links[e].edgeType;
                if (!t || t === 'mentions') continue;
                const ep = edgeLabelPool[es++];
                ep.e = e;
                if (ep.el.textContent !== t) ep.el.textContent = t;
            }
        }
    }

    function placeLabels() {
        const pa = pos();
        for (const lp of labelPool) {
            if (lp.i < 0) continue;
            const i = lp.i;
            const p = project(pa[3 * i], pa[3 * i + 1], pa[3 * i + 2]);
            if (!onScreen(p, 0)) { lp.el.style.display = 'none'; continue; }
            lp.el.style.display = '';
            lp.el.style.transform = 'translate(' + Math.round(p.x + 9) + 'px,' + Math.round(p.y - 7) + 'px)';
        }
        for (const ep of edgeLabelPool) {
            if (ep.e < 0) continue;
            const s = linkS[ep.e] * 3, t = linkT[ep.e] * 3;
            const p = project((pa[s] + pa[t]) / 2, (pa[s + 1] + pa[t + 1]) / 2, (pa[s + 2] + pa[t + 2]) / 2);
            if (!onScreen(p, 0)) { ep.el.style.display = 'none'; continue; }
            ep.el.style.display = '';
            ep.el.style.transform = 'translate(' + Math.round(p.x) + 'px,' + Math.round(p.y) + 'px) translate(-50%,-50%)';
        }
        if (focusIdx >= 0) {
            const i = focusIdx;
            const p = project(pa[3 * i], pa[3 * i + 1], pa[3 * i + 2]);
            if (!p) { balloon.style.display = 'none'; return; }
            const half = (balloon.offsetWidth || 260) / 2 + 8;
            const x = Math.min(Math.max(p.x, half), Math.max(half, W - half));
            const y = Math.max(p.y - 18, balloon.offsetHeight + 8);
            balloon.style.display = '';
            balloon.style.transform = 'translate(' + Math.round(x) + 'px,' + Math.round(y) + 'px) translate(-50%,-100%)';
        }
    }

    // ---- resize + render loop ----
    function resize() {
        const w = container.clientWidth, h = container.clientHeight;
        if (!w || !h || (w === W && h === H)) return;
        W = w; H = h;
        renderer.setSize(W, H, false);
        camera.aspect = W / H;
        camera.updateProjectionMatrix();
        pixelScaleCss = H / (2 * Math.tan(THREE.MathUtils.degToRad(FOV) / 2));
        pixelScaleUniform.value = pixelScaleCss * pr;
        renderer.getDrawingBufferSize(resolutionUniform.value);
    }
    const ro = new ResizeObserver(resize);
    ro.observe(container);
    resize();

    let raf = 0, last = performance.now(), lastLabel = 0, lastThumb = 0;
    function frame(now) {
        raf = requestAnimationFrame(frame);
        if (document.hidden || !container.clientWidth || !container.clientHeight) { last = now; return; }
        if (!W) resize();
        const dt = Math.min(0.1, (now - last) / 1000);
        last = now;

        if (tween) {
            const k = Math.min(1, (now - tween.t0) / tween.dur);
            const e = k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2;
            controls.target.lerpVectors(tween.t1, tween.t2, e);
            camera.position.lerpVectors(tween.c1, tween.c2, e);
            if (k >= 1) tween = null;
        }
        controls.update(dt);
        applyTheme(false);
        renderer.render(scene, camera);

        if (hoverDirty) { hoverDirty = false; doHover(); }
        if (labelsDirty || now - lastLabel > 150) { lastLabel = now; labelsDirty = false; selectLabels(); }
        if (now - lastThumb > 250) { lastThumb = now; selectThumbs(); }
        placeLabels();
    }
    raf = requestAnimationFrame(frame);

    function dispose() {
        disposed = true;
        cancelAnimationFrame(raf);
        ro.disconnect();
        document.removeEventListener('keydown', onKey);
        clearGraph();
        for (const s of sprites) { scene.remove(s); s.material.dispose(); }
        for (const p of thumbCache.values()) p.then(t => t && t.dispose());
        thumbCache.clear();
        controls.dispose();
        renderer.dispose();
        renderer.forceContextLoss();
        if (tooltip) tooltip.classList.add('hidden');
        container.innerHTML = '';
    }

    return {
        container, canvas, setData, applyFilters, focus, unfocus, fit, dispose,
        indexOf: id => (idToIndex.has(id) ? idToIndex.get(id) : -1),
    };
}

window.KBGraph3D = {
    // Returns false when WebGL is unavailable.
    mount(el, opts) {
        if (G && G.container === el && G.canvas.parentNode === el) return true;
        if (G) { G.dispose(); G = null; }
        try {
            G = createInstance(el, opts || {});
        } catch (err) {
            console.error('KBGraph3D: WebGL init failed', err);
            G = null;
            return false;
        }
        return true;
    },
    isMounted: () => !!G,
    setData: d => G && G.setData(d),
    applyFilters: f => (G ? G.applyFilters(f) : { count: 0 }),
    focus: i => G && G.focus(i),
    unfocus: () => G && G.unfocus(),
    fit: () => G && G.fit(),
    indexOf: id => (G ? G.indexOf(id) : -1),
    dispose() { if (G) { G.dispose(); G = null; } },
};
window.dispatchEvent(new Event('kbgraph3d-ready'));
