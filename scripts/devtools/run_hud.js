// Run the real HUD script against a stubbed canvas/DOM for N frames.
// Catches runtime errors that `node --check` cannot: bad property access,
// undefined array indexes, NaN coordinates, runaway particle counts.
const fs = require('fs');
const path = process.argv[2];
let js = fs.readFileSync(path, 'utf8');

let drawCalls = 0, nanSeen = 0;
function num(v, where) {
  if (typeof v === 'number' && !Number.isFinite(v)) { nanSeen++; if (nanSeen < 4) console.log('  NaN/Inf at', where, v); }
}
const ctx = new Proxy({}, {
  get(t, k) {
    if (k === 'createRadialGradient') return () => ({ addColorStop(o, c) { num(o, 'stop'); } });
    if (k === 'measureText') return () => ({ width: 40 });
    return (...a) => { drawCalls++; a.forEach((v, i) => num(v, String(k) + '#' + i)); };
  },
  set(t, k, v) { if (typeof v === 'number') num(v, 'set:' + String(k)); t[k] = v; return true; }
});

let rafQueue = [];
global.requestAnimationFrame = (fn) => { rafQueue.push(fn); return rafQueue.length; };
global.addEventListener = () => {};
global.setInterval = () => 0;
global.location = { protocol: 'http:', hostname: '127.0.0.1' };
global.fetch = () => ({ then() { return this; }, catch() { return this; } });
global.navigator = {};
const elStub = new Proxy({ classList: { add() {}, remove() {} }, style: {}, addEventListener() {} }, {
  get(t, k) { if (k in t) return t[k]; if (k === 'getContext') return () => ctx;
    if (k === 'clientWidth') return 1920; if (k === 'clientHeight') return 1080;
    return typeof t[k] === 'function' ? t[k] : (k === 'textContent' ? '' : undefined); },
  set() { return true; }
});
global.document = {
  getElementById: () => elStub,
  querySelector: () => elStub,
  querySelectorAll: () => [elStub],
  addEventListener: () => {},
};
global.window = global;
global.devicePixelRatio = 1;

try { eval(js); } catch (e) { console.log('SETUP ERROR:', e.message); process.exit(1); }

// Drive frames. SPEAKING is toggled halfway to exercise the speaking palette.
let t = 0, frames = 0, errors = 0;
while (rafQueue.length && frames < 240) {
  const fn = rafQueue.shift();
  t += 16.7;
  if (frames === 120) { try { global.SPEAKING = 1; } catch (e) {} }
  try { fn(t); } catch (e) { errors++; if (errors < 4) console.log('  FRAME ERROR:', e.message); }
  frames++;
}
console.log('frames run      :', frames);
console.log('draw calls      :', drawCalls);
console.log('runtime errors  :', errors);
console.log('NaN/Inf coords  :', nanSeen);
try {
  console.log('signals alive   :', typeof SIG !== 'undefined' ? SIG.length : 'n/a');
  console.log('flares alive    :', typeof FLARES !== 'undefined' ? FLARES.length : 'n/a');
  let lit = 0, emb = 0;
  if (typeof FIRE !== 'undefined') for (const f of FIRE) if (f > 0) lit++;
  if (typeof FLARES !== 'undefined') for (const f of FLARES) emb += f.em.length;
  console.log('cells firing    :', lit);
  console.log('embers in air   :', emb);
} catch (e) { console.log('state read failed:', e.message); }
process.exit(errors > 0 || nanSeen > 0 ? 1 : 0);
