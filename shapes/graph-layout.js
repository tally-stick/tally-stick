// Layout for the "who talks to whom" graph on board.html. One file so the page and the tuning renders (node) run the
// same code: node graph-layout.js graph.json 2 300 > positions.json
//
// Fruchterman–Reingold with edge weights and a weak pull to the centre: every pair repels (k²/d), every edge attracts
// (d²/k, scaled by the square root of how many comments the pair exchanged), and a cooling temperature caps how far a
// node moves per step. Handles are seeded on a spiral by degree, most connected in the middle, so the settled picture
// is stable from one load to the next.
function graphLayout(G, opts) {
  const o = Object.assign({minW: 2, cap: 300, W: 1400, H: 900, steps: 300}, opts || {});
  const edges0 = G.edges.filter(e => e.w >= o.minW);
  const dg = new Map();
  for (const e of edges0) { dg.set(e.from, (dg.get(e.from) || 0) + e.w); dg.set(e.to, (dg.get(e.to) || 0) + e.w); }
  const nodes = G.nodes.filter(n => dg.has(n.id)).sort((a, b) => dg.get(b.id) - dg.get(a.id)).slice(0, o.cap === "all" ? Infinity : o.cap);
  const idx = new Map(nodes.map((n, i) => [n.id, i]));
  const E = edges0.filter(e => idx.has(e.from) && idx.has(e.to)).map(e => ({a: idx.get(e.from), b: idx.get(e.to), w: e.w}));
  const maxC = Math.max(1, ...nodes.map(n => n.comments));
  const N = nodes.map((n, i) => {
    const a = i * 2.399963, r = 10 * Math.sqrt(i);
    return {...n, x: o.W / 2 + Math.cos(a) * r, y: o.H / 2 + Math.sin(a) * r, dx: 0, dy: 0, deg: dg.get(n.id),
            r: 2.5 + 11 * Math.sqrt(n.comments / maxC),
            kind: (n.in === 0 && n.out > 0) ? "send" : (n.out === 0 && n.in > 0 && n.comments === 0) ? "recv" : "both"};
  });
  const k = Math.sqrt(o.W * o.H / Math.max(1, N.length)) * 0.9, k2 = k * k, cut = (4 * k) ** 2;
  const state = {N, E, k, shown: {nodes: N.length, edges: E.length, of: G.nodes.length, ofEdges: G.edges.length}, T: k};
  state.step = function (T) {
    for (const n of N) { n.dx = 0; n.dy = 0; }
    for (let i = 0; i < N.length; i++) {
      const a = N[i];
      for (let j = i + 1; j < N.length; j++) {
        const b = N[j]; let dx = a.x - b.x, dy = a.y - b.y; const d2 = dx * dx + dy * dy + 0.01;
        if (d2 > cut) continue;
        let f = k2 / d2;  // k²/d, then divided by d again to normalise the direction
        const gap = a.r + b.r + 4, d = Math.sqrt(d2);
        if (d < gap) f += (gap - d) / d;  // no two dots on top of each other: push overlapping pairs apart by the overlap
        dx *= f; dy *= f; a.dx += dx; a.dy += dy; b.dx -= dx; b.dy -= dy;
      }
    }
    for (const e of E) {
      const a = N[e.a], b = N[e.b]; const dx = a.x - b.x, dy = a.y - b.y; const d = Math.sqrt(dx * dx + dy * dy) + 0.01;
      // d²/k along the unit vector, more for pairs that talk more, less for hubs (a handle with 80 ties would otherwise
      // be pulled into a knot with the other hubs; dividing by the smaller degree opens the core)
      const f = d / k * Math.sqrt(Math.min(e.w, 6)) * 0.35 / Math.sqrt(1 + Math.min(a.deg, b.deg) / 12);
      a.dx -= dx * f; a.dy -= dy * f; b.dx += dx * f; b.dy += dy * f;
    }
    for (const n of N) {
      n.dx += (o.W / 2 - n.x) * 0.02; n.dy += (o.H / 2 - n.y) * 0.03;  // a weak pull to the middle keeps islands in frame (weaker across, so the picture is wide like the canvas)
      if (n.pinned) continue;
      const d = Math.sqrt(n.dx * n.dx + n.dy * n.dy) + 0.01, m = Math.min(d, T);
      n.x += n.dx / d * m; n.y += n.dy / d * m;
    }
  };
  for (let i = 0; i < o.steps; i++) state.step(k * (1 - i / o.steps) + 0.5);
  return state;
}
if (typeof module !== "undefined") {
  module.exports = graphLayout;
  if (require.main === module) {
    const fs = require("fs"), [file, minW, cap] = process.argv.slice(2);
    const s = graphLayout(JSON.parse(fs.readFileSync(file, "utf8")), {minW: +minW || 2, cap: cap === "all" ? "all" : (+cap || 300)});
    process.stdout.write(JSON.stringify({nodes: s.N.map(n => ({id: n.id, x: n.x, y: n.y, r: n.r, kind: n.kind, deg: n.deg})), edges: s.E, shown: s.shown}));
  }
}
