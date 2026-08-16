#!/usr/bin/env python3
"""Pillar 5 — the heads-up display.

Renders ~/.jarvis/brief.json into a self-contained HUD: black ground, a violet particle
orb, and a left command rail. Local file only; nothing is uploaded.

  hud.py ~/.jarvis/brief.json --open
"""
from __future__ import annotations

import argparse
import datetime as _dt
import html
import json
import webbrowser
from pathlib import Path

from common import (BRIEF_FILE, HUD_FILE, STATE_DIR, VERSION, ensure_state_dir,
                    read_json)

VAULT_GRAPH = STATE_DIR / "vault_graph.json"

E = lambda s: html.escape(str(s if s is not None else ""))  # noqa: E731

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#05080d">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="JARVIS">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon-192.png">
<title>JARVIS</title>
<style>
  *{box-sizing:border-box}
  :root{
    /* Legibility pass 2026-08-15. The old --dim/#2f5158/#24413f greys measured
       under 3:1 against black — technically visible, actually unreadable. Every
       label colour here now clears 4.5:1 on #000. Do not darken them back. */
    --cyan:#3ad6c8; --cyan-hi:#8ff5ea; --dim:#8db3ba; --dimmer:#7ea4ab;
    --ink:#dbf1ee; --amber:#e0a63c; --green:#3fcf87; --blue:#4a9ff5;
    --label:#7ea4ab;                    /* section sub-labels, was #2f5158 */
    --faint:#6f959c;                    /* the quietest text on the page */
    --edge:rgba(58,214,200,.15); --fill:rgba(3,14,18,.5);
  }
  html,body{height:100%}
  body{margin:0;background:#000;color:var(--ink);overflow:hidden;
    font:14px/1.6 "SFMono-Regular",Consolas,"Roboto Mono","Courier New",monospace;
    -webkit-font-smoothing:antialiased}
  canvas#orb{position:fixed;inset:0;width:100%;height:100%;z-index:0}
  .vig{position:fixed;inset:0;z-index:1;pointer-events:none;
    background:radial-gradient(130% 95% at 55% 50%,transparent 42%,rgba(0,0,0,.7) 100%)}

  /* ---------------- top bar ---------------- */
  .top{position:fixed;top:0;left:0;right:0;z-index:3;
    display:flex;align-items:flex-start;justify-content:space-between;padding:11px 18px}
  .brand{display:flex;flex-direction:column}
  .dots{display:flex;gap:4px;margin-bottom:3px}
  .dots i{width:6px;height:6px;border-radius:50%;display:block}
  .wordmark{font-size:22px;font-weight:800;letter-spacing:6px;color:#f2fbfa;line-height:1}
  .brand small{font-size:8px;letter-spacing:3.6px;color:var(--faint);margin-top:3px}
  .nav{display:flex;align-items:center;gap:9px;padding-top:9px;flex:1;justify-content:center}
  .nav b{font-size:9.5px;font-weight:500;letter-spacing:2.4px;color:var(--dim)}
  .nav b.on{color:var(--cyan)}
  .nav s{text-decoration:none;color:var(--faint);font-size:9.5px}
  .right{display:flex;flex-direction:column;align-items:flex-end}
  .exit{font-size:9.5px;letter-spacing:2.4px;color:#ff6b6b;
    border:1px solid rgba(255,107,107,.32);border-radius:3px;padding:3px 9px;
    display:flex;align-items:center;gap:5px}
  .exit i{width:4px;height:4px;border-radius:50%;background:#ff6b6b;display:block}
  .clock{margin-top:9px;font-size:38px;font-weight:300;letter-spacing:1px;color:#eafffb;
    line-height:1;font-variant-numeric:tabular-nums;text-shadow:0 0 24px rgba(58,214,200,.3)}
  .clock em{font-style:normal;font-size:18px;color:var(--cyan)}
  .stamp{font-size:8px;letter-spacing:2.4px;color:var(--faint);margin-top:4px}

  /* ---------------- left rail ---------------- */
  /* 456px = the original 365px + 25% (King, 2026-08-15). The orb's centre in the
     canvas code below is derived from this width — change one, change both. */
  .rail{position:fixed;top:64px;bottom:12px;left:18px;width:456px;z-index:3;
    display:flex;flex-direction:column;gap:11px;overflow-y:auto;padding-right:6px}
  .rail::-webkit-scrollbar{width:4px}
  .rail::-webkit-scrollbar-thumb{background:rgba(58,214,200,.25)}
  .p{border:1px solid var(--edge);border-radius:4px;background:var(--fill);
    padding:13px 15px;backdrop-filter:blur(3px)}
  .ph{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:11px}
  .ph h2{margin:0;font-size:13px;font-weight:700;letter-spacing:2.6px;color:var(--cyan)}
  .ph span{font-size:9.5px;letter-spacing:2px;color:var(--label)}
  .led{width:6px;height:6px;border-radius:50%;flex:none}

  .hint{font-size:11.5px;color:var(--dim);text-align:center;margin:4px 0 2px}
  .mic{width:56px;height:56px;margin:14px auto 10px;border-radius:50%;
    border:1px solid rgba(58,214,200,.4);display:flex;align-items:center;
    justify-content:center;box-shadow:0 0 30px rgba(58,214,200,.2),inset 0 0 18px rgba(58,214,200,.08)}
  .mic b{display:block;width:15px;height:21px;border-radius:7px 7px 6px 6px;
    background:#48e0d0;box-shadow:0 0 16px #48e0d0}
  .mic.rec{border-color:#ff6b6b;box-shadow:0 0 34px rgba(255,107,107,.45)}
  .mic.rec b{background:#ff6b6b;box-shadow:0 0 18px #ff6b6b}
  /* Live input feedback. King must be able to SEE that his words are arriving —
     a silent mic and a working one looked identical, which is what made a dead
     microphone read as Jarvis ignoring him. Driven per-frame from the real
     AnalyserNode level, never a fake animation: if it does not move, no audio
     is reaching the browser, and that is the diagnosis. */
  .mic{transition:transform .06s linear}
  .lvl{width:132px;height:4px;margin:0 auto 10px;border-radius:3px;
    background:rgba(120,160,170,.16);overflow:hidden;opacity:0;
    transition:opacity .2s}
  .lvl.on{opacity:1}
  .lvl i{display:block;height:100%;width:0%;border-radius:3px;background:#ff6b6b;
    box-shadow:0 0 10px #ff6b6b;transition:width .05s linear}
  .cta{font-size:11.5px;font-weight:700;letter-spacing:2.1px;color:#9ff0e6;
    text-align:center;margin-bottom:9px;cursor:pointer;user-select:none;
    border:1px solid rgba(58,214,200,.35);border-radius:7px;padding:7px 10px;
    transition:all .18s}
  .cta:hover{border-color:rgba(58,214,200,.7)}
  .cta.on{color:#ffb3b3;border-color:rgba(255,107,107,.75);
    box-shadow:0 0 18px rgba(255,107,107,.28);
    animation:hfpulse 1.6s ease-in-out infinite}
  @keyframes hfpulse{50%{box-shadow:0 0 30px rgba(255,107,107,.5)}}
  .fine{font-size:10.5px;line-height:1.7;color:var(--label);text-align:center;margin-bottom:11px}
  .row{display:flex;gap:6px}
  .row input{flex:1;background:rgba(0,0,0,.45);border:1px solid var(--edge);border-radius:3px;
    color:var(--ink);font:inherit;font-size:12px;padding:9px 11px;outline:none}
  .row button,.qa button{background:transparent;border:1px solid var(--edge);border-radius:3px;
    color:var(--cyan);font:inherit;font-size:10.5px;font-weight:700;letter-spacing:1.6px;
    padding:9px 13px;cursor:pointer}

  .tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:11px}
  .tile{border:1px solid var(--edge);border-radius:3px;padding:9px 4px;text-align:center;
    background:rgba(0,0,0,.3)}
  .tile b{display:block;font-size:17px;font-weight:700;color:#eefcfa;letter-spacing:.4px}
  .tile span{display:block;font-size:9px;letter-spacing:1.5px;color:var(--label);margin-top:5px}

  .box{border:1px solid var(--edge);border-radius:3px;padding:10px 11px;
    background:rgba(0,0,0,.22);margin-bottom:10px}
  .box:last-child{margin-bottom:0}
  .box h3{margin:0 0 9px;font-size:10px;font-weight:500;letter-spacing:2.2px;color:var(--label)}
  ol.t3{margin:0;padding:0;list-style:none;counter-reset:n}
  ol.t3 li{counter-increment:n;font-size:12.5px;color:var(--ink);padding:4px 0 4px 19px;
    position:relative;line-height:1.5}
  ol.t3 li::before{content:counter(n);position:absolute;left:0;color:var(--dim);font-size:11px}
  ol.t3 li.done{color:var(--label);text-decoration:line-through}
  .ag{display:flex;justify-content:space-between;gap:14px;padding:5px 0;font-size:12.5px}
  .ag u{text-decoration:none;color:var(--dim);font-variant-numeric:tabular-nums;flex:none}
  .ag span{color:var(--ink);text-align:right;line-height:1.45}
  .ag.hard span{color:var(--amber)}
  .ag.new span{color:var(--green)}
  .qa{display:flex;gap:6px}
  .qa button{flex:1;padding:8px 2px}

  .tool{display:flex;align-items:center;gap:10px;padding:5px 0;font-size:11.5px}
  .tool i{width:6px;height:6px;border-radius:50%;flex:none}
  .tool b{font-weight:500;letter-spacing:1.5px;color:var(--dim);flex:1;text-transform:uppercase;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .tool u{text-decoration:none;color:#bcdadf;letter-spacing:1px;font-size:11px;text-transform:uppercase;flex:none}
  .badge{font-size:9px;letter-spacing:1.3px;padding:2px 7px;border-radius:2px;flex:none}
  .foot{font-size:9.5px;line-height:1.7;color:var(--label);margin-top:11px}
  .note{font-size:11.5px;color:var(--amber);border-left:2px solid var(--amber);
    padding:5px 0 5px 9px;margin-top:9px;line-height:1.55}
  .empty{font-size:11.5px;color:var(--label);font-style:italic}
  /* breakpoint follows the rail: 940 was 365+575, so 456+575 = 1031 -> 1040 */
  @media(max-width:1040px){.rail{position:static;width:auto;margin:64px 12px 12px}
    body{overflow:auto}canvas#orb{opacity:.4}}
  /* PHONE MODE (/phone route): the sphere, the name, the version — nothing
     else (King, 2026-08-16). Same page, same orb code, a class instead of a
     second template so the orb can never drift between devices. Overrides the
     1040px breakpoint that dims the orb and stacks the rail on small screens. */
  body.phone .rail,body.phone .nav,body.phone .right,body.phone .dots{display:none!important}
  body.phone{overflow:hidden!important}
  body.phone canvas#orb{opacity:1!important}
  body.phone .top{left:14px;top:12px;right:auto}
  body.phone .wordmark{font-size:17px}
</style></head><body class="%%PHONEMODE%%">
<canvas id="orb"></canvas><div class="vig"></div>

<div class="top">
  <div class="brand">
    <div class="dots"><i style="background:#ff5f57"></i><i style="background:#febc2e"></i><i style="background:#28c840"></i></div>
    <div class="wordmark">JARVIS</div><small>%%VERSION%%</small>
  </div>
  <div class="nav">
    <b class="on">&#9679; CORE</b><s>&middot;</s><b>%%NAVSTATE%%</b>
    <b style="margin-left:14px">LINK</b><s>&middot;</s><b class="on">ONLINE</b>
  </div>
  <div class="right">
    <div class="exit"><i></i>EXIT</div>
    <div class="clock">%%HH%%:%%MM%%<em>:%%SS%% %%AMPM%%</em></div>
    <div class="stamp">%%STAMP%%</div>
  </div>
</div>

<div class="rail">
  <section class="p">
    <div class="ph" id="vlink"><h2>VOICE LINK // %%VSTATE%%</h2>
      <i class="led" id="vled" style="background:%%VLED%%;box-shadow:0 0 7px %%VLED%%"></i></div>
    <div class="hint">Use the mic or type below to start.</div>
    <div class="mic"><b></b></div>
    <div class="lvl"><i></i></div>
    <div class="cta" id="hfbtn">HANDS-FREE: OFF</div>
    <div class="fine">%%VFINE%%</div>
    <div class="row"><input id="say" placeholder="Or type a message&hellip;"
      autocomplete="off"><button id="send">SEND</button></div>
    <div class="fine" id="reply" style="margin:8px 0 0"></div>
    <div id="offline" style="display:none;margin-top:9px;font-size:8.5px;line-height:1.6;
      color:#e0a63c;border-left:2px solid #e0a63c;padding-left:9px">
      <b>This is a published snapshot.</b> Browsers block an HTTPS page from
      reaching a local server, so this copy can show your day but cannot talk to
      Jarvis.<br><br>To speak or type to him, open
      <b>http://127.0.0.1:8787</b> on the laptop.
    </div>
  </section>

  <section class="p">
    <div class="ph"><h2>DAILY BRIEF // TODAY</h2><span>%%SYNCED%%</span></div>
    %%TILES%%
    <div class="box"><h3>TOP 3 TODAY</h3>%%TOP3%%</div>
    <div class="box"><h3>AGENDA</h3>%%AGENDA%%</div>
    %%NOTES%%
  </section>

  <section class="p">
    <div class="ph"><h2>QUICK ACTIONS</h2></div>
    <div class="qa"><button>CAPABILITIES</button><button>DAILY BRIEF</button><button>PLAN MY DAY</button></div>
  </section>

  <section class="p">
    <div class="ph"><h2>CONNECTED // KNOWLEDGE</h2><span>%%CAPSTATE%%</span></div>
    %%CAPS%%
    <div class="foot">Ask anything in the box above &mdash; Jarvis searches your
      vault, your files and your mail before he answers, and says so when he
      finds nothing.</div>
  </section>

  <section class="p">
    <div class="ph"><h2>TOOLS // CONTROL</h2><span>%%TOOLSTATE%%</span></div>
    %%TOOLS%%
    <div class="foot">%%FOOT%%</div>
  </section>
</div>

<script>
var VAULT=%%GRAPH%%;
// Where the API lives. Served by serve.py this is same-origin. Opened from the
// published here.now copy (or file://) it points at the local Jarvis, so the one
// link King keeps still drives the real thing when the server is up.
var API=(location.protocol.indexOf('http')===0&&location.hostname&&
         (location.hostname==='127.0.0.1'||location.hostname==='localhost'))
        ? '' : 'http://127.0.0.1:8787';
var SPEAKING=0;               // 0..1, driven by /api/status — the vault lights up
(function(){
  var c=document.getElementById('orb'),x=c.getContext('2d'),W,H,DPR;
  function size(){DPR=Math.min(window.devicePixelRatio||1,2);W=c.clientWidth;H=c.clientHeight;
    c.width=W*DPR;c.height=H*DPR;x.setTransform(DPR,0,0,DPR,0,0);}
  size();addEventListener('resize',size);
  // Fibonacci sphere -> even coverage, no polar clumping.
  // ---- the vault, not an abstract sphere. Nodes are ObsiVault pages, edges are
  // [[wikilinks]]. Laid out once with a 3D spring/repulsion pass, then rotated.
  var NODES=VAULT.nodes||[], LINKS=VAULT.links||[], idx={}, i, j;
  for(i=0;i<NODES.length;i++){var n=NODES[i];idx[n.id]=i;
    // seed on a Fibonacci shell so the solver starts spread out, never coincident
    var yy=1-(i/(Math.max(NODES.length-1,1)))*2, rr=Math.sqrt(Math.max(0,1-yy*yy)),
        th=Math.PI*(3-Math.sqrt(5))*i;
    n.x=Math.cos(th)*rr; n.y=yy; n.z=Math.sin(th)*rr;
    n.vx=n.vy=n.vz=0;}
  var E=[];
  for(i=0;i<LINKS.length;i++){var a1=idx[LINKS[i].s],b1=idx[LINKS[i].t];
    if(a1!==undefined&&b1!==undefined) E.push([a1,b1]);}

  // A guaranteed BALL, like the video. Physics gave a lopsided web twice — the fix
  // is constructive, not iterative: walk the graph breadth-first from the biggest hub
  // and lay nodes along a Fibonacci sphere in that order, so linked pages sit near
  // each other on the surface direction; then fill the volume with a deterministic
  // rank^(1/3) radius (uniform-ball distribution), hubs biased toward the core.
  (function place(){
    var n=NODES.length; if(!n) return;
    var adj=[], k;
    for(k=0;k<n;k++) adj.push([]);
    for(k=0;k<E.length;k++){adj[E[k][0]].push(E[k][1]);adj[E[k][1]].push(E[k][0]);}
    var order=[],seen=new Array(n),qi=0;
    var seed=0; for(k=1;k<n;k++) if((NODES[k].deg||0)>(NODES[seed].deg||0)) seed=k;
    var queue=[seed]; seen[seed]=1;
    while(order.length<n){
      if(qi>=queue.length){ for(k=0;k<n;k++) if(!seen[k]){queue.push(k);seen[k]=1;break;} }
      var cur=queue[qi++]; if(cur===undefined) break;
      order.push(cur);
      var nb=adj[cur];
      for(k=0;k<nb.length;k++) if(!seen[nb[k]]){seen[nb[k]]=1;queue.push(nb[k]);}
    }
    var ph=Math.PI*(3-Math.sqrt(5));
    for(k=0;k<order.length;k++){
      var nd=NODES[order[k]],
          yy=1-(k/Math.max(order.length-1,1))*2,
          rr=Math.sqrt(Math.max(0,1-yy*yy)),
          th=ph*k,
          // deterministic per-node depth: hash spreads it, deg pulls hubs inward
          h=0, id=nd.id;
      for(var c=0;c<id.length;c++) h=(h*31+id.charCodeAt(c))>>>0;
      var depth=Math.pow((h%1000)/1000,1/3),               // uniform ball fill
          hub=Math.min(nd.deg||0,40)/40,
          rad=.22+.78*depth*(1-.55*hub);                   // hubs live deeper
      nd.x=Math.cos(th)*rr*rad; nd.y=yy*rad; nd.z=Math.sin(th)*rr*rad;
    }
  })();
  // Per-group palette. UNUSED since the node dots were removed 2026-08-15 — kept
  // deliberately, not dead weight: this is the mapping to restore if page names
  // should ever be tinted by kind (entity / concept / source) instead of all
  // sharing LABEL_RGB. Deleting it would lose the only record of which colour
  // meant which kind.
  var COL={core:[235,245,255], entity:[150,125,250], concept:[ 90,215,235],
           source:[240,180,110], synthesis:[130,240,180], topic:[210,150,245],
           query:[120,200,255], ghost:[ 96, 96,150]};
  // Speaking palette. Cycled PER NODE and PER EDGE, not applied flat — the vault
  // shimmers through electric cyan / violet / magenta / plasma / gold instead of
  // the whole ball flashing one colour, which read as a tint rather than as life.
  var HOT=[[ 70,255,238],[165,120,255],[255, 85,215],[ 95,175,255],[255,215,130]];

  var SCALE=2.0;          // King 2026-08-15: "twice as large". ONE dial — the base
                          // .36 is untouched so this stays the honest multiplier.
                          // At 2.0 the sphere deliberately overflows the viewport:
                          // you are inside the vault, not looking at a marble.
  var LABEL_MIN_DEG=5;    // was 20 — MORE names on screen (kept)
  var LABEL_PX=11;        // 10 +10% (King, 2026-08-15). Frequency and size are
                          // separate dials on purpose — change one only.
  // "30% lighter" is TWO moves on black, not one. The base was already near-white
  // (214,240,248), so shifting the hue 30% toward white is nearly invisible on its
  // own; what actually reads as dim is the alpha, which falls off with depth. Both
  // are raised 30% so the change is visible where King is looking.
  var LABEL_RGB=[226,245,250];   // 30% of the remaining distance to white
  var LABEL_LIGHTEN=1.30;        // and 30% more opacity
  // What a name lights up TO when its cell fires. Was pure white [255,255,255],
  // which King called far too bright — a firing name flared harder than anything
  // else on the display. A neutral grey still reads as "this one just lit up"
  // without the glare, because the CHANGE is the signal, not the brightness.
  var LABEL_LIT=[176,186,192];

  // ---- the solar-flare system was REMOVED 2026-08-15 -----------------------
  // Page names used to ignite, swell and burst into rising embers. The burst was
  // drawn with a large shadowBlur halo, which rendered as a huge white disc with
  // the page name sitting on it — King asked for those gone and for names to stop
  // growing and bursting. Removed wholesale rather than shrunk: the glow WAS the
  // effect, so a smaller one would just be a smaller version of the thing he
  // rejected. Page names now render at one flat size, always.
  // The brain-cell firing below is a SEPARATE system and stays.

  function mix(c1,c2,k){return [c1[0]+(c2[0]-c1[0])*k|0,
                                c1[1]+(c2[1]-c1[1])*k|0,
                                c1[2]+(c2[2]-c1[2])*k|0];}

  // ---- synapses -----------------------------------------------------------
  // Charge runs the [[wikilinks]] continuously. A signal travels one edge; on
  // arrival it FIRES that page and hops onward down one or two of its OTHER
  // links. That cascade is the whole trick — dots sliding along a wireframe look
  // mechanical, but a signal that arrives, lights a cell, and branches reads as
  // brain tissue. Hop chance is deliberately under 1.0 average or the graph
  // saturates in about four seconds and never goes quiet again.
  var NADJ=[], FIRE=new Float32Array(NODES.length), SIG=[], TARGET=110;
  (function(){for(var k0=0;k0<NODES.length;k0++) NADJ.push([]);
    for(k0=0;k0<E.length;k0++){NADJ[E[k0][0]].push(k0); NADJ[E[k0][1]].push(k0);}})();
  function spawnSignal(node){
    if(!E.length||SIG.length>TARGET*1.8) return;
    var list=(node!==undefined&&NADJ[node]&&NADJ[node].length)?NADJ[node]:null,
        e=list?list[(Math.random()*list.length)|0]:(Math.random()*E.length)|0,
        // travel AWAY from whichever end just fired
        d=(node!==undefined)?(E[e][0]===node?0:1):((Math.random()<.5)?0:1);
    SIG.push({e:e,d:d,t:0,sp:.55+Math.random()*1.15,h:(Math.random()*HOT.length)|0});}

  var a=0, pulse=0, last=0;
  function frame(ts){
    // Real elapsed time, not per-frame constants: the flare lifecycle must run at
    // the same speed on a 60Hz and a 144Hz screen. Clamped so a backgrounded tab
    // does not resume with one enormous jump.
    var dt=last?Math.min((ts-last)/1000,.05):.016; last=ts||0;
    // 476 = rail left margin 18 + rail width 456 + 2. Keep in step with .rail.
    var R=Math.min(W,H)*.36*SCALE, cx=W<1040?W*.5:(476+(W-476)*.5), cy=H*.52;
    // Screen radius of the solid body. Derived, not guessed: a surface node sits
    // at |v|=1 and Z=0, where pe = 1/1.9, so it projects to R*1.7/1.9. Hard-coding
    // a number here instead would let the body drift out of register with the
    // page names the moment SCALE or the perspective divisor changed.
    var SR=R*1.7/1.9;
    x.clearRect(0,0,W,H);
    a+=(.0013+SPEAKING*.0022)*(dt/.016);
    pulse+=.055*(dt/.016);
    var lift=SPEAKING*(.55+.45*Math.sin(pulse));   // the "lighting up"
    var ca=Math.cos(a),sa=Math.sin(a),cb=Math.cos(a*.42),sb=Math.sin(a*.42),P=[];
    for(i=0;i<NODES.length;i++){var n2=NODES[i],
      X=n2.x*ca-n2.z*sa, Z0=n2.x*sa+n2.z*ca, Y=n2.y*cb-Z0*sb, Z=n2.y*sb+Z0*cb,
      pe=1/(1.9-Z*.55);
      P.push({X:cx+X*R*pe*1.7,Y:cy+Y*R*pe*1.7,Z:Z,pe:pe,n:n2});}

    // ---- advance the charge, and let arrivals fire their page
    for(var si=SIG.length-1;si>=0;si--){var s=SIG[si]; s.t+=dt*s.sp;
      if(s.t>=1){
        var dst=s.d===0?E[s.e][1]:E[s.e][0];
        if(FIRE[dst]!==undefined) FIRE[dst]=Math.min(1.6,FIRE[dst]+1);
        SIG.splice(si,1);
        var hops=(Math.random()<.62?1:0)+(Math.random()<.22?1:0);
        for(var hh=0;hh<hops;hh++) spawnSignal(dst);}}
    while(SIG.length<TARGET) spawnSignal();
    for(i=0;i<FIRE.length;i++) if(FIRE[i]>0) FIRE[i]=Math.max(0,FIRE[i]-dt*1.9);

    // ---- soft central bloom. A GRADIENT that fades to nothing, never a filled
    // disc with a rim: the painted body tried here on 2026-08-15 read as "a big
    // ugly circle around it" (King), because a hard edge anywhere on screen is a
    // circle, not a sphere. Volume has to come from the points themselves.
    var g=x.createRadialGradient(cx,cy,0,cx,cy,SR*1.25);
    if(lift>.02){var h1=HOT[((pulse*.35)|0)%HOT.length];
      g.addColorStop(0,'rgba('+h1[0]+','+h1[1]+','+h1[2]+','+(.10+lift*.22).toFixed(3)+')');
      g.addColorStop(.5,'rgba(110,80,220,'+(.05+lift*.12).toFixed(3)+')');}
    else{g.addColorStop(0,'rgba(150,120,255,.13)');
         g.addColorStop(.5,'rgba(110,80,220,.05)');}
    g.addColorStop(1,'rgba(0,0,0,0)');
    x.fillStyle=g;x.beginPath();x.arc(cx,cy,SR*1.25,0,6.2832);x.fill();

    // ---- links, whole sphere. Nothing is opaque now, so the far hemisphere is
    // drawn too and simply falls off with depth — seeing the back through the
    // front is exactly what makes a cloud of points read as a BALL rather than a
    // disc of points.
    x.lineWidth=.55;
    for(i=0;i<E.length;i++){var a1=E[i][0],b1=E[i][1],p4=P[a1],p5=P[b1];
      if(!p4||!p5) continue;
      var d=((p4.Z+p5.Z)/2+1)/2, al=(.05+.16*d)*(1+lift*1.9),
          fe=Math.max(FIRE[a1]||0,FIRE[b1]||0);
      if(fe>.05) al+=fe*.18;
      if(lift>.02){var hc=mix([120,105,235],HOT[i%HOT.length],lift);
        x.strokeStyle='rgba('+hc[0]+','+hc[1]+','+hc[2]+','+al.toFixed(3)+')';}
      else x.strokeStyle='rgba(120,105,235,'+al.toFixed(3)+')';
      x.beginPath();x.moveTo(p4.X,p4.Y);x.lineTo(p5.X,p5.Y);x.stroke();}

    // ---- the sphere itself: the points ARE the shape.
    // A node that carries a page name gets NO dot — that dot was "the circle to
    // the left of the file name" King removed. The unnamed pages keep theirs and
    // are what fill the volume, so the ball still reads as solid while no name
    // has a bead sitting beside it.
    x.globalCompositeOperation='lighter';
    for(i=0;i<P.length;i++){var p6=P[i],nd=p6.n;
      if(nd.deg>=LABEL_MIN_DEG&&nd.group!=='ghost') continue;   // this one is named
      var d2=(p6.Z+1)/2,fr=FIRE[i]||0,
      base=COL[nd.group]||COL.entity,
      col=lift>.02?mix(base,HOT[i%HOT.length],lift*.85):base,
      rad=(1.0+Math.min(nd.deg,40)*.11)*p6.pe*(1+lift*.40+fr*.35),
      al=(.26+.62*d2)*(nd.group==='ghost'?.5:1)*(1+lift*.5+fr*.8);
      if(fr>.02) col=mix(col,LABEL_LIT,Math.min(1,fr*.9));
      if(al>1) al=1;
      x.fillStyle='rgba('+col[0]+','+col[1]+','+col[2]+','+al.toFixed(3)+')';
      x.beginPath();x.arc(p6.X,p6.Y,rad,0,6.2832);x.fill();}
    x.globalCompositeOperation='source-over';

    // ---- page names, whole sphere, depth-dimmed
    for(i=0;i<P.length;i++){var p7=P[i],n7=p7.n,f7=FIRE[i]||0;
      if(n7.deg<LABEL_MIN_DEG||n7.group==='ghost') continue;
      var d3=(p7.Z+1)/2,
          la=((d3-.30)*.95*(.5+lift*.5)+f7*.55)*LABEL_LIGHTEN;
      if(la>1) la=1;
      if(la<=.02) continue;
      x.font=LABEL_PX+'px Consolas,monospace';
      x.fillStyle=f7>.05?'rgba('+mix(LABEL_RGB,LABEL_LIT,Math.min(1,f7)).join(',')+','+la.toFixed(3)+')'
                        :'rgba('+LABEL_RGB.join(',')+','+la.toFixed(3)+')';
      x.fillText(n7.title.toUpperCase(),p7.X+4,p7.Y+3);}

    LASTP=P;
    requestAnimationFrame(frame);}
  var LASTP=[];
  requestAnimationFrame(frame);
  // Click a node -> open its wiki page (served at /vault/<id>; inert on file://).
  c.addEventListener('click',function(ev){
      // Nothing occludes any more, so the whole sphere is clickable again — but
      // ties go to whichever page is nearest the FRONT, or a click near the limb
      // opens something on the far side that merely projects under the cursor.
      var best=null,bd=196,bz=-2;   // 14px radius
      for(var k=0;k<LASTP.length;k++){var q=LASTP[k],
        dx=q.X-ev.clientX,dy=q.Y-ev.clientY,d2=dx*dx+dy*dy;
        if(d2<bd&&q.n.group!=='ghost'&&(d2<bd*.6||q.Z>bz)){bd=d2;bz=q.Z;best=q.n;}}
      if(best) window.open(API+'/vault/'+best.id,'_blank');});
  // 12-hour with AM/PM. It used to render "13:57.35", which reads as a decimal
  // number rather than a clock — King asked for the time, plainly.
  var cl=document.querySelector('.clock');
  setInterval(function(){var d=new Date(),p=function(n){return(n<10?'0':'')+n;},
    h=d.getHours(), ap=h<12?'AM':'PM', h12=h%12||12;
    cl.innerHTML=h12+':'+p(d.getMinutes())+'<em>:'+p(d.getSeconds())+' '+ap+'</em>';},1000);

  // ---- live wiring. Served by serve.py these calls resolve; opened as a bare
  // file:// the fetches reject and the page stays exactly as rendered. Both are
  // valid states, so every handler fails silently rather than showing an error.
  var head=document.querySelector('#vlink h2'), led=document.querySelector('#vled'),
      say=document.getElementById('say'), send=document.getElementById('send'),
      reply=document.getElementById('reply'), mtime=null, busy=false;
  function post(){
    var t=(say.value||'').trim(); if(!t||busy) return;
    busy=true; reply.textContent='Working…'; say.value='';
    fetch(API+'/api/say',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text:t})})
      .then(function(r){return r.json();})
      .then(function(d){reply.textContent=d.reply||d.error||'';})
      .catch(function(){reply.textContent=
        'Cannot reach Jarvis at 127.0.0.1:8787 — is the server running?';})
      .then(function(){busy=false;});
  }
  if(send) send.addEventListener('click',post);
  if(say) say.addEventListener('keydown',function(e){if(e.key==='Enter') post();});

  // ---- push-to-talk. Click the mic (or hold Space) to record, click/release to
  // send. Uses the browser's own microphone and POSTs the clip to /api/listen, so
  // it works from the phone too — where the laptop's array cannot hear you.
  var mic=document.querySelector('.mic'), rec=null, chunks=[], recording=false;
  var lvl=document.querySelector('.lvl'), lvlBar=document.querySelector('.lvl i');
  var actx=null, meterRAF=0, sawSound=false, peakSeen=0;

  // Drive the mic button from the LIVE signal: it swells, glows and jitters in
  // time with his voice. This is feedback, not decoration — if he speaks and
  // nothing moves, the browser is getting no audio, and he can see that instantly
  // instead of waiting on a nonsense answer to tell him something was wrong.
  function startMeter(stream){
    try{
      var AC=window.AudioContext||window.webkitAudioContext; if(!AC) return;
      actx=new AC();
      var src=actx.createMediaStreamSource(stream);
      var an=actx.createAnalyser(); an.fftSize=512; an.smoothingTimeConstant=0.6;
      src.connect(an);
      var buf=new Uint8Array(an.fftSize);
      if(lvl) lvl.classList.add('on');
      sawSound=false; peakSeen=0;
      (function tick(){
        if(!recording){ if(lvl) lvl.classList.remove('on'); return; }
        meterRAF=requestAnimationFrame(tick);
        an.getByteTimeDomainData(buf);
        var sum=0;
        for(var i=0;i<buf.length;i++){var d=(buf[i]-128)/128; sum+=d*d;}
        var rms=Math.sqrt(sum/buf.length);          // 0..1
        var v=Math.min(1, rms*4.2);                 // speech sits low; scale up
        if(v>peakSeen) peakSeen=v;
        if(v>0.06) sawSound=true;
        if(lvlBar) lvlBar.style.width=Math.round(v*100)+'%';
        if(mic){
          // swell + a small random jitter = the "vibrate" while words land
          var j=v>0.06?(Math.random()-0.5)*v*7:0;
          mic.style.transform='scale('+(1+v*0.34).toFixed(3)+') translate('
            +j.toFixed(1)+'px,'+((Math.random()-0.5)*v*7).toFixed(1)+'px)';
          mic.style.boxShadow='0 0 '+(30+v*70).toFixed(0)+'px rgba(255,107,107,'
            +(0.35+v*0.5).toFixed(2)+')';
        }
        // Real haptics where the device has them (phone/tablet); no-op on desktop.
        if(v>0.5&&navigator.vibrate){try{navigator.vibrate(12);}catch(e){}}
      })();
    }catch(e){}
  }
  function stopMeter(){
    if(meterRAF) cancelAnimationFrame(meterRAF); meterRAF=0;
    if(lvl) lvl.classList.remove('on');
    if(lvlBar) lvlBar.style.width='0%';
    if(mic){mic.style.transform='';mic.style.boxShadow='';}
    if(actx){try{actx.close();}catch(e){} actx=null;}
  }

  function startRec(){
    if(recording||!navigator.mediaDevices) return;
    navigator.mediaDevices.getUserMedia({audio:true}).then(function(st){
      rec=new MediaRecorder(st); chunks=[]; recording=true;
      if(mic) mic.classList.add('rec');
      startMeter(st);
      if(head) head.textContent='VOICE LINK // RECORDING';
      rec.ondataavailable=function(e){chunks.push(e.data);};
      rec.onstop=function(){
        st.getTracks().forEach(function(tr){tr.stop();});
        var blob=new Blob(chunks,{type:rec.mimeType||'audio/webm'});
        // The browser heard nothing at all — say so here rather than shipping
        // silence to Whisper, which would invent a sentence to fill it.
        if(!sawSound){
          reply.textContent='No audio from your microphone — check that the '
            +'right input is selected and unmuted.';
          if(head) head.textContent='VOICE LINK // NO INPUT';
          return;
        }
        reply.textContent='Transcribing…';
        fetch(API+'/api/listen',{method:'POST',
          headers:{'Content-Type':blob.type},body:blob})
          .then(function(r){return r.json();})
          .then(function(d){reply.textContent=d.reply||d.error||d.heard||'';})
          .catch(function(){reply.textContent='Could not reach Jarvis.';});
      };
      rec.start();
    }).catch(function(){reply.textContent='Microphone permission denied.';});
  }
  function stopRec(){
    if(!recording) return; recording=false;
    if(mic) mic.classList.remove('rec');
    stopMeter();
    try{rec.stop();}catch(e){}
  }
  if(mic){
    mic.style.cursor='pointer';
    mic.addEventListener('click',function(){
      if(hfOn) return;                       // hands-free owns the mic then
      recording?stopRec():startRec();});
  }
  document.addEventListener('keydown',function(e){
    if(e.code==='Space'&&document.activeElement!==say&&!recording&&!hfOn){
      e.preventDefault(); startRec();}});
  document.addEventListener('keyup',function(e){
    if(e.code==='Space'&&document.activeElement!==say&&!hfOn) stopRec();});

  // ---- HANDS-FREE. One press arms it: the mic stays open, speech onset and
  // end are detected from the live level (same math as the meter), each
  // finished utterance is sent on its own, and listening resumes after the
  // reply. No wake word, no holding anything — conversation, not push-to-talk.
  // Survives the HUD's own mtime reloads via localStorage re-arm.
  var hfBtn=document.getElementById('hfbtn');
  var hfOn=false, hfStream=null, hfCtx=null, hfRAF=0;
  var hfRec=null, hfChunks=[], hfTalking=false, hfQuietAt=0, hfLoud=0, hfDrop=false;
  var HF_START=0.075, HF_END=0.045, HF_QUIET_MS=1100, HF_MAX_MS=15000, hfBeganAt=0;

  function hfLabel(){
    if(!hfBtn) return;
    hfBtn.textContent='HANDS-FREE: '+(hfOn?'ON — just talk':'OFF');
    if(hfOn) hfBtn.classList.add('on'); else hfBtn.classList.remove('on');
  }
  function hfSend(blob){
    if(blob.size<2000) return;               // sub-audible blip, not speech
    busy=true; reply.textContent='Transcribing…';
    fetch(API+'/api/listen',{method:'POST',
      headers:{'Content-Type':blob.type||'audio/webm'},body:blob})
      .then(function(r){return r.json();})
      .then(function(d){reply.textContent=d.reply||d.error||d.heard||'';})
      .catch(function(){reply.textContent='Could not reach Jarvis.';})
      .then(function(){busy=false;});
  }
  function hfBegin(){
    if(hfTalking||!hfStream) return;
    try{
      hfRec=new MediaRecorder(hfStream); hfChunks=[]; hfDrop=false;
      hfRec.ondataavailable=function(e){hfChunks.push(e.data);};
      hfRec.onstop=function(){
        if(hfDrop) return;
        hfSend(new Blob(hfChunks,{type:hfRec.mimeType||'audio/webm'}));
      };
      hfRec.start(); hfTalking=true; hfQuietAt=0; hfBeganAt=Date.now();
      if(mic) mic.classList.add('rec');
      if(head) head.textContent='VOICE LINK // HEARING YOU';
    }catch(e){}
  }
  function hfFinish(drop){
    if(!hfTalking) return;
    hfTalking=false; hfDrop=!!drop;
    if(mic) mic.classList.remove('rec');
    try{hfRec.stop();}catch(e){}
  }
  function hfTick(an,buf){
    if(!hfOn) return;
    hfRAF=requestAnimationFrame(function(){hfTick(an,buf);});
    an.getByteTimeDomainData(buf);
    var sum=0;
    for(var i=0;i<buf.length;i++){var d=(buf[i]-128)/128; sum+=d*d;}
    var v=Math.min(1, Math.sqrt(sum/buf.length)*4.2);
    if(lvlBar) lvlBar.style.width=Math.round(v*100)+'%';
    if(mic){
      var j=v>0.06?(Math.random()-0.5)*v*7:0;
      mic.style.transform='scale('+(1+v*0.34).toFixed(3)+') translate('
        +j.toFixed(1)+'px,'+((Math.random()-0.5)*v*7).toFixed(1)+'px)';
      mic.style.boxShadow='0 0 '+(30+v*70).toFixed(0)+'px rgba(255,107,107,'
        +(0.35+v*0.5).toFixed(2)+')';
    }
    // Jarvis mid-reply or a request in flight: drop any partial capture so he
    // never transcribes his own voice, and don't arm on it either.
    if(SPEAKING||busy){ hfFinish(true); hfLoud=0; return; }
    if(!hfTalking){
      if(v>HF_START){ if(++hfLoud>=3) hfBegin(); }
      else hfLoud=0;
    }else{
      if(Date.now()-hfBeganAt>HF_MAX_MS){ hfFinish(false); return; }
      if(v<HF_END){
        if(!hfQuietAt) hfQuietAt=Date.now();
        else if(Date.now()-hfQuietAt>HF_QUIET_MS) hfFinish(false);
      }else hfQuietAt=0;
    }
  }
  function hfStart(){
    if(hfOn||!navigator.mediaDevices) return;
    navigator.mediaDevices.getUserMedia({audio:{
      echoCancellation:true,noiseSuppression:true,autoGainControl:true}})
      .then(function(st){
        hfOn=true; hfStream=st; hfLabel();
        try{localStorage.setItem('jarvis_hf','1');}catch(e){}
        var AC=window.AudioContext||window.webkitAudioContext;
        hfCtx=new AC();
        if(hfCtx.state==='suspended'){try{hfCtx.resume();}catch(e){}}
        var src=hfCtx.createMediaStreamSource(st);
        var an=hfCtx.createAnalyser(); an.fftSize=512; an.smoothingTimeConstant=0.6;
        src.connect(an);
        if(lvl) lvl.classList.add('on');
        if(head) head.textContent='VOICE LINK // HANDS-FREE';
        hfTick(an,new Uint8Array(an.fftSize));
      })
      .catch(function(){
        reply.textContent='Microphone permission denied.';
        try{localStorage.removeItem('jarvis_hf');}catch(e){}
        hfLabel();
      });
  }
  function hfStop(){
    hfOn=false; hfFinish(true);
    try{localStorage.removeItem('jarvis_hf');}catch(e){}
    if(hfRAF) cancelAnimationFrame(hfRAF); hfRAF=0;
    if(hfStream){hfStream.getTracks().forEach(function(t){t.stop();}); hfStream=null;}
    if(hfCtx){try{hfCtx.close();}catch(e){} hfCtx=null;}
    if(lvl) lvl.classList.remove('on');
    if(lvlBar) lvlBar.style.width='0%';
    if(mic){mic.style.transform='';mic.style.boxShadow='';mic.classList.remove('rec');}
    hfLabel();
  }
  if(hfBtn){
    hfLabel();
    hfBtn.addEventListener('click',function(){hfOn?hfStop():hfStart();});
  }
  // Re-arm after the HUD's own mtime-triggered reloads — hands-free must not
  // silently die because the vault graph refreshed underneath the page.
  try{if(localStorage.getItem('jarvis_hf')==='1') hfStart();}catch(e){}

  setInterval(function(){
    var ac=new AbortController(); setTimeout(function(){ac.abort();},2500);
    fetch(API+'/api/status',{signal:ac.signal}).then(function(r){return r.json();}).then(function(s){
      if(head) head.textContent='VOICE LINK // '+
        (s.state==='responding'?'SPEAKING':s.ears?'LISTENING':'STANDBY');
      if(led){var c=s.state==='responding'?'#e0a63c':s.ears?'#3fcf87':'#3ad6c8';
        led.style.background=c;led.style.boxShadow='0 0 7px '+c;}
      SPEAKING = (s.state==='responding') ? 1 : 0;   // the vault lights up
      if(s.heard&&reply&&!busy&&!reply.textContent) reply.textContent='“'+s.heard+'”';
      // brief.json OR the vault changed underneath us -> repaint, don't go stale
      var stamp=(s.brief_mtime||0)+':'+(s.graph_mtime||0);
      if(mtime===null) mtime=stamp;
      else if(stamp!==mtime) location.reload();
      var b=document.getElementById('offline'); if(b) b.style.display='none';
    }).catch(function(){
      var b=document.getElementById('offline'); if(b) b.style.display='block';
      if(head) head.textContent='VOICE LINK // OFFLINE';
      if(say){say.placeholder='Snapshot only — open 127.0.0.1:8787 to talk';
              say.disabled=true;}
      if(send) send.disabled=true;
    });
  },1500);
})();
</script>
</body></html>"""


def _tiles(stats) -> str:
    if not stats:
        return ""
    return ('<div class="tiles">' + "".join(
        f'<div class="tile"><b>{E(s.get("value"))}</b>'
        f'<span>{E(s.get("label"))}</span></div>' for s in stats[:3]) + "</div>")


def _top3(steps) -> str:
    if not steps:
        return '<p class="empty">No objective set.</p>'
    out = []
    for s in steps[:5]:
        text, done = (s.get("text", ""), bool(s.get("done"))) if isinstance(s, dict) \
            else (s, False)
        out.append(f'<li class="{"done" if done else ""}">{E(text)}</li>')
    return f'<ol class="t3">{"".join(out)}</ol>'


def _agenda(events) -> str:
    if not events:
        return '<p class="empty">Calendar clear.</p>'
    out = []
    for ev in events[:9]:
        cls = ("hard" if ev.get("hard_commitment")
               else "new" if ev.get("added_by_jarvis") else "")
        out.append(f'<div class="ag {cls}"><u>{E(ev.get("time",""))}</u>'
                   f'<span>{E(ev.get("title",""))}</span></div>')
    return "".join(out)


DOT = {"ok": "#3fcf87", "warn": "#e0a63c", "info": "#4a9ff5", "off": "#2f5158"}


def _tools(systems) -> str:
    """Accepts 'Name: value' strings or {name, value, state, action} dicts."""
    if not systems:
        return '<p class="empty">No systems reporting.</p>'
    out = []
    for s in systems:
        if isinstance(s, str):
            name, _, value = s.partition(":")
            item = {"name": name, "value": value.strip(), "state": "ok"}
        else:
            item = s
        badge = ""
        if item.get("action"):
            col = DOT.get(item.get("state", "ok"), "#3ad6c8")
            badge = (f'<span class="badge" style="color:{col};border:1px solid {col}">'
                     f'{E(item["action"])}</span>')
        out.append(f'<div class="tool"><i style="background:'
                   f'{DOT.get(item.get("state","ok"))}"></i><b>{E(item.get("name"))}</b>'
                   f'<u>{E(item.get("value"))}</u>{badge}</div>')
    return "".join(out)


_CAP_CACHE: dict = {"at": 0.0, "rows": None}


def capabilities(max_age: int = 300) -> list[dict]:
    """Live status of everything Jarvis can draw on, probed rather than declared.

    A panel that reads its own claims out of brief.json would say "Gmail: connected"
    for as long as the file sat on disk — including the week after the token was
    revoked. These are real probes. Gmail's costs a round trip, so the whole set is
    cached for five minutes; the HUD re-renders on every page load.
    """
    import time
    if _CAP_CACHE["rows"] is not None and time.time() - _CAP_CACHE["at"] < max_age:
        return _CAP_CACHE["rows"]

    rows = []
    try:
        import brain
        st = brain.status()
        if st.get("indexed"):
            c = st.get("counts", {})
            rows.append({"name": "Second Brain (ObsiVault wiki)", "state": "ok",
                         "value": f"{c.get('vault', 0)} pages", "action": "SEARCHABLE"})
            rows.append({"name": "Computer files", "state": "ok",
                         "value": f"{c.get('file', 0):,} indexed",
                         "action": f"{st.get('mb', 0)} MB"})
        else:
            rows.append({"name": "Second Brain + files", "state": "warn",
                         "value": "not indexed", "action": "RUN brain.py index"})
    except Exception as exc:
        rows.append({"name": "Second Brain + files", "state": "warn",
                     "value": str(exc)[:40]})

    try:
        import mail
        ms = mail.status()
        rows.append({"name": "Gmail", "state": "ok" if ms.get("ok") else "warn",
                     "value": ms.get("mailbox") or "offline",
                     "action": "READ-ONLY" if ms.get("ok") else "REAUTH"})
    except Exception as exc:
        rows.append({"name": "Gmail", "state": "warn", "value": str(exc)[:40]})

    try:
        import music
        ok = bool(music.ytdlp())
        rows.append({"name": "Music (YouTube)", "state": "ok" if ok else "warn",
                     "value": "ready" if ok else "yt-dlp missing",
                     "action": '"PLAY …"' if ok else "INSTALL"})
    except Exception as exc:
        rows.append({"name": "Music (YouTube)", "state": "warn", "value": str(exc)[:40]})

    _CAP_CACHE.update({"at": time.time(), "rows": rows})
    return rows


def render(brief: dict, phone: bool = False) -> str:
    now = _dt.datetime.now()
    # Detect the provider that WOULD speak right now. brief.json records whatever
    # spoke when it was written, which goes stale the moment a key is added — the
    # panel was still claiming "Windows SAPI" days after the voice changed.
    from common import load_env_key
    prov = ("elevenlabs" if load_env_key("ELEVENLABS_API_KEY")
            else "deepinfra" if load_env_key("DEEPINFRA_API_KEY")
            else "sapi")
    cloned = prov == "elevenlabs"
    notes = "".join(f'<div class="note">{E(n)}</div>' for n in (brief.get("notes") or []))
    actions = brief.get("calendar_actions") or []

    # The centrepiece is the second brain itself. Rebuilt on demand by vault.py;
    # an empty graph degrades to an empty centre rather than breaking the page.
    graph = read_json(VAULT_GRAPH, default=None) or {"nodes": [], "links": []}
    graph_js = json.dumps({"nodes": graph.get("nodes", []),
                           "links": graph.get("links", [])}, separators=(",", ":"))

    caps = capabilities()
    live = sum(1 for c in caps if c.get("state") == "ok")

    repl = {
        "%%GRAPH%%": graph_js,
        # 12-hour, so the first paint matches what the JS tick renders a second
        # later. %I zero-pads ("01:57"); lstrip gives the 1:57 a clock reads as.
        "%%HH%%": now.strftime("%I").lstrip("0") or "12",
        "%%MM%%": now.strftime("%M"),
        "%%SS%%": now.strftime("%S"),
        "%%AMPM%%": now.strftime("%p"),
        # NOT passed through E(): the separator is a literal character, and escaping
        # it turned the stamp into "Sat &middot; Aug 15" on screen.
        "%%STAMP%%": now.strftime("%a · %b %d").replace(" 0", " ").upper(),
        "%%VERSION%%": f"VERSION {VERSION}",
        "%%PHONEMODE%%": "phone" if phone else "",
        "%%NAVSTATE%%": "IDLE",
        "%%SYNCED%%": "SYNCED",
        "%%VSTATE%%": "STANDBY",
        "%%VLED%%": "#3fcf87" if cloned else "#3ad6c8",
        # Name the provider that will actually speak. Claiming "cloned" or "Windows"
        # when neither is true is the one lie this panel must never tell.
        "%%VFINE%%": ("HANDS-FREE: press once, then just talk &mdash; no wake "
                      "word, no holding. Or click the mic / hold Space for "
                      "push-to-talk. "
                      + ("Cloned voice via ElevenLabs." if cloned else
                         "Voice: Kokoro bm_george &mdash; British, not a clone."
                         if prov == "deepinfra" else
                         "Voice: Windows SAPI &mdash; robotic fallback.")),
        "%%TOOLSTATE%%": ("COMPUTER-USE ARMED" if brief.get("computer_use_armed")
                          else "COMPUTER-USE DISARMED"),
        "%%TILES%%": _tiles(brief.get("stats")),
        "%%TOP3%%": _top3(brief.get("steps")),
        "%%AGENDA%%": _agenda(brief.get("agenda")),
        "%%NOTES%%": notes,
        "%%TOOLS%%": _tools(brief.get("systems")),
        "%%CAPS%%": _tools(caps),
        "%%CAPSTATE%%": f"{live}/{len(caps)} LIVE" if caps else "NONE",
        "%%FOOT%%": (E(actions[0]) if actions else
                     "No calendar writes this run."),
    }
    page = PAGE
    for k, v in repl.items():
        page = page.replace(k, v)
    return page


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("brief", nargs="?", default=str(BRIEF_FILE))
    p.add_argument("--out", default=str(HUD_FILE))
    p.add_argument("--open", action="store_true")
    args = p.parse_args()

    brief = read_json(Path(args.brief), default=None)
    if brief is None:
        print(json.dumps({"ok": False, "error": f"could not read brief at {args.brief}"}))
        return 2

    ensure_state_dir()
    out = Path(args.out)
    out.write_text(render(brief), encoding="utf-8")
    if args.open:
        webbrowser.open(out.resolve().as_uri())
    print(json.dumps({"ok": True, "hud": str(out), "opened": bool(args.open)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
