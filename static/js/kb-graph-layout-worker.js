// 3D force layout for the KB graph, kept off the main thread.
// in:  { n: number, links: Uint32Array [s0,t0,s1,t1,...] }
// out: { positions: Float32Array [x,y,z,...], done: boolean } (positions buffer is transferred)
importScripts(
    'vendor/d3-dispatch.min.js',
    'vendor/d3-timer.min.js',
    'vendor/d3-quadtree.min.js',
    'vendor/d3-binarytree.min.js',
    'vendor/d3-octree.min.js',
    'vendor/d3-force-3d.min.js'
);

const CHUNK_TICKS = 10;
const MAX_TICKS = 300;

let runId = 0;

self.onmessage = (e) => {
    const { n, links } = e.data;
    const id = ++runId;

    const nodes = new Array(n);
    for (let i = 0; i < n; i++) nodes[i] = { index: i };
    const edges = new Array(links.length / 2);
    for (let i = 0; i < edges.length; i++) edges[i] = { source: links[2 * i], target: links[2 * i + 1] };

    // Charge range and link length grow with the graph so big KBs spread out instead of collapsing into a ball.
    const spread = Math.max(1, Math.cbrt(n / 500));
    const sim = d3.forceSimulation(nodes)
        .numDimensions(3)
        .force('link', d3.forceLink(edges).distance(30 * spread).strength(0.3))
        .force('charge', d3.forceManyBody().strength(-40 * spread).theta(0.9).distanceMax(400 * spread))
        .force('center', d3.forceCenter(0, 0, 0))
        .stop();

    let ticks = 0;
    const step = () => {
        if (id !== runId) return; // superseded by a newer request
        const upto = Math.min(CHUNK_TICKS, MAX_TICKS - ticks);
        for (let i = 0; i < upto; i++) sim.tick();
        ticks += upto;

        const positions = new Float32Array(n * 3);
        for (let i = 0; i < n; i++) {
            const nd = nodes[i];
            positions[3 * i] = nd.x || 0;
            positions[3 * i + 1] = nd.y || 0;
            positions[3 * i + 2] = nd.z || 0;
        }
        const done = ticks >= MAX_TICKS || sim.alpha() < sim.alphaMin();
        self.postMessage({ positions, done }, [positions.buffer]);
        if (!done) setTimeout(step, 0);
    };
    step();
};
