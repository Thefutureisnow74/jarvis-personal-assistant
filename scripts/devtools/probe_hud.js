// Prove the BEHAVIOUR, not just that it runs: do page names grow then turn to
// fire, do embers get thrown, does charge move, do cells light up?
const fs = require('fs');
let js = fs.readFileSync(process.argv[2], 'utf8');

const stats = {
  fontSizes: [], fillColors: [], strokes: 0, arcs: 0, texts: 0,
  arcRadii: [], lineSegs: 0,
};
let cur = { fillStyle: '', font: '' };

function parseRGB(s) {
  const m = /rgba?\((\d+),(\d+),(\d+)/.exec(String(s).replace(/\s/g, ''));
  return m ? [ +m[1], +m[2], +m[3] ] : null;
}
const ctx = new Proxy({}, {
  get(t, k) {
    if (k === 'createRadialGradient') return () => ({ addColorStop() {} });
    if (k === 'measureText') return () => ({ width: 40 });
    if (k === 'fillText') return (txt, X, Y) => {
      stats.texts++;
      const m = /([\d.]+)px/.exec(cur.font); if (m) stats.fontSizes.push(+m[1]);
      const c = parseRGB(cur.fillStyle); if (c) stats.fillColors.push(c);
    };
    if (k === 'arc') return (X, Y, r) => { stats.arcs++; stats.arcRadii.push(r); };
    if (k === 'stroke') return () => { stats.strokes++; };
    if (k === 'lineTo') return () => { stats.lineSegs++; };
    return () => {};
  },
  set(t, k, v) { cur[k] = v; return true; }
});

global.requestAnimationFrame = (fn) => { q.push(fn); };
global.addEventListener = () => {}; global.setInterval = () => 0;
global.location = { protocol: 'http:', hostname: '127.0.0.1' };
global.fetch = () => ({ then() { return this; }, catch() { return this; } });
global.navigator = {}; global.devicePixelRatio = 1;
const el = new Proxy({ classList: { add() {}, remove() {} }, style: {}, addEventListener() {} }, {
  get(t, k) { if (k in t) return t[k]; if (k === 'getContext') return () => ctx;
    if (k === 'clientWidth') return 1920; if (k === 'clientHeight') return 1080; return undefined; },
  set() { return true; }
});
global.document = { getElementById: () => el, querySelector: () => el, querySelectorAll: () => [el], addEventListener() {} };
global.window = global;

let q = [];
eval(js);

let t = 0, n = 0;
while (q.length && n < 600) { const fn = q.shift(); t += 16.7; fn(t); n++; }

const warm = stats.fillColors.filter(c => c[0] > 200 && c[1] < 200 && c[2] < 120).length;
const white = stats.fillColors.filter(c => c[0] > 230 && c[1] > 230 && c[2] > 200).length;
const cool = stats.fillColors.filter(c => c[2] > c[0]).length;
const fs_ = stats.fontSizes;
console.log('frames                 :', n);
console.log('page names drawn       :', stats.texts);
console.log('font size min/max      :', Math.min(...fs_).toFixed(1), '/', Math.max(...fs_).toFixed(1),
            '  (growth ratio ' + (Math.max(...fs_) / Math.min(...fs_)).toFixed(1) + 'x)');
console.log('text colors  white-hot :', white);
console.log('             fire/amber:', warm, warm > 0 ? '<-- BURSTS INTO FLAME' : '<-- NO FIRE!');
console.log('             cool/idle :', cool);
console.log('particles (arcs) drawn :', stats.arcs);
console.log('charge streaks (lines) :', stats.lineSegs);
console.log('halo strokes           :', stats.strokes);
