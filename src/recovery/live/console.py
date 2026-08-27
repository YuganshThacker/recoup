"""The control room page.

One self-contained document: no CDN, no build step, no font fetch. The audit
report already holds that line -- a demo that goes dark because a venue's wifi
blocked a font host would be an avoidable way to lose a room.

**Every number on this page is derived in the browser from audit events.**
There is no server-side tally to trust. Hovering a figure shows the events it
was computed from, which is the same rule the rest of the project follows: no
financial number without a traceable source.
"""

from __future__ import annotations

_CONSOLE = r"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Recoup Control Room</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' fill='%2306080b'/><rect x='6' y='7' width='4' height='18' fill='%233395ff'/><rect x='14' y='13' width='4' height='12' fill='%232fd47a'/><rect x='22' y='10' width='4' height='15' fill='%23ff5f3d'/></svg>">
<style>
:root{
  --ground:#06080b; --panel:#0a0e13; --panel-2:#0d131a;
  --line:#151f2a; --line-hot:#1e2c3b;
  --dim:#546578; --mid:#8397a8; --text:#c7d4e0; --bright:#eaf2f9;
  --blue:#3395ff; --blue-dim:#1b4d85;
  --green:#2fd47a; --green-dim:#146b3e;
  --refuse:#ff5f3d; --amber:#ffb020; --violet:#a67cff;
  --mono:ui-monospace,"SF Mono","JetBrains Mono","Fira Code",Menlo,Consolas,monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{
  background:var(--ground); color:var(--text);
  font:12px/1.45 var(--mono); letter-spacing:.01em;
  overflow:hidden; -webkit-font-smoothing:antialiased;
}
/* atmosphere: a faint instrument grid, grain, and a vignette */
body::before{
  content:""; position:fixed; inset:0; pointer-events:none; z-index:0; opacity:.45;
  background-image:linear-gradient(var(--line) 1px,transparent 1px),linear-gradient(90deg,var(--line) 1px,transparent 1px);
  background-size:56px 56px; mask-image:radial-gradient(ellipse 120% 90% at 50% 0%,#000 20%,transparent 75%);
}
body::after{
  content:""; position:fixed; inset:0; pointer-events:none; z-index:9999; opacity:.035;
  background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='140' height='140'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='3'/></filter><rect width='140' height='140' filter='url(%23n)'/></svg>");
}
#app{position:relative;z-index:1;height:100%;display:grid;grid-template-rows:auto minmax(0,1fr) auto;grid-template-columns:1fr 336px}

/* ---------- header instrument bar ---------- */
header{grid-column:1/3;border-bottom:1px solid var(--line-hot);background:linear-gradient(180deg,#0b1119,#080c11);display:flex;align-items:stretch}
.brand{padding:14px 20px;border-right:1px solid var(--line);display:flex;flex-direction:column;justify-content:center;min-width:232px}
.brand h1{font-size:15px;font-weight:700;letter-spacing:.26em;color:var(--bright)}
.brand h1 em{font-style:normal;color:var(--blue)}
.brand .sub{font-size:9.5px;letter-spacing:.2em;color:var(--dim);margin-top:4px;text-transform:uppercase}
.readouts{display:flex;flex:1}
.ro{padding:12px 22px;border-right:1px solid var(--line);display:flex;flex-direction:column;justify-content:center;min-width:150px;position:relative;cursor:help}
.ro .k{font-size:9px;letter-spacing:.2em;color:var(--dim);text-transform:uppercase}
.ro .v{font-size:23px;font-weight:600;color:var(--bright);margin-top:5px;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.ro.money .v{color:var(--green)}
.ro.refuse .v{color:var(--refuse)}
.ro.gates .v{color:var(--blue)}
.ro[data-src]:hover::after{
  content:attr(data-src); position:absolute; top:100%; left:0; z-index:60; width:290px;
  background:#0e1620; border:1px solid var(--line-hot); border-left:2px solid var(--blue);
  padding:9px 11px; font-size:10px; line-height:1.55; color:var(--mid); box-shadow:0 18px 40px rgba(0,0,0,.7);
}
.controls{margin-left:auto;display:flex;align-items:center;gap:14px;padding:0 20px;border-left:1px solid var(--line)}
button{font:inherit;color:var(--text);background:#101821;border:1px solid var(--line-hot);padding:8px 16px;cursor:pointer;letter-spacing:.14em;font-size:10px;text-transform:uppercase;transition:.14s}
button:hover:not(:disabled){border-color:var(--blue);color:var(--bright);background:#13202e}
button:disabled{opacity:.35;cursor:not-allowed}
button.go{border-color:var(--blue-dim);color:#bcdcff}
.pace{display:flex;border:1px solid var(--line-hot)}
.pace button{border:0;border-right:1px solid var(--line);padding:8px 11px}
.pace button:last-child{border-right:0}
.pace button.on{background:var(--blue);color:#04080d;font-weight:700}
.live{display:flex;align-items:center;gap:8px;font-size:9.5px;letter-spacing:.18em;color:var(--dim);text-transform:uppercase}
.dot{width:7px;height:7px;border-radius:50%;background:var(--dim)}
.dot.on{background:var(--green);box-shadow:0 0 0 0 rgba(47,212,122,.6);animation:pulse 1.8s infinite}
@keyframes pulse{70%{box-shadow:0 0 0 9px rgba(47,212,122,0)}100%{box-shadow:0 0 0 0 rgba(47,212,122,0)}}

/* ---------- lanes ---------- */
main{display:grid;grid-template-columns:repeat(5,1fr);overflow:hidden;min-height:0;border-right:1px solid var(--line-hot)}
.lane{border-right:1px solid var(--line);display:flex;flex-direction:column;overflow:hidden;min-height:0;background:linear-gradient(180deg,rgba(255,255,255,.012),transparent 200px)}
.lane:last-child{border-right:0}
.lane-h{padding:10px 13px 9px;border-bottom:1px solid var(--line);display:flex;align-items:baseline;gap:9px;background:var(--panel)}
.lane-h .n{font-size:9px;color:var(--dim)}
.lane-h .t{font-size:10px;letter-spacing:.2em;color:var(--mid);font-weight:600}
.lane-h .c{margin-left:auto;font-size:10px;color:var(--dim);font-variant-numeric:tabular-nums}
.lane[data-lane="GOVERN"] .lane-h{background:linear-gradient(180deg,#0e1017,#0a0e13)}
.lane[data-lane="GOVERN"] .lane-h .t{color:var(--blue)}
.stream{flex:1;min-height:0;overflow:hidden;padding:8px;display:flex;flex-direction:column;gap:6px;-webkit-mask-image:linear-gradient(180deg,#000 82%,transparent 100%);mask-image:linear-gradient(180deg,#000 82%,transparent 100%)}

.ev{border:1px solid var(--line);border-left:2px solid var(--dim);background:var(--panel-2);padding:7px 9px;cursor:pointer;animation:in .16s ease-out;flex:0 0 auto}
@keyframes in{from{opacity:0;transform:translateY(-9px)}to{opacity:1;transform:none}}
.ev:hover{border-color:var(--line-hot);background:#111823}
.ev:focus-visible,.rf:focus-visible{outline:1px solid var(--blue);outline-offset:1px}
.ev .top{display:flex;gap:7px;align-items:baseline;margin-bottom:3px}
.ev .actor{font-size:8.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--dim)}
.ev .case{margin-left:auto;font-size:9px;color:#3f4e5e}
.ev .rep{font-size:8.5px;color:var(--amber);border:1px solid #3a2a10;padding:0 4px;letter-spacing:.06em}
.ev .s{font-size:11px;color:var(--text);line-height:1.4;word-break:break-word}
.ev.agent{border-left-color:var(--violet)}
.ev.rules{border-left-color:var(--blue)}
.ev.webhook{border-left-color:var(--mid)}
.ev.ok{border-left-color:var(--green)}
.ev.ok .s{color:#a9e6c6}
.ev.bad{border-left-color:var(--refuse);background:#180d0a;box-shadow:inset 0 0 26px rgba(255,95,61,.09)}
.ev.bad .s{color:#ffb9a7}
.ev.money .s{color:var(--green);font-weight:600}

/* ---------- refusal reel ---------- */
aside{display:flex;flex-direction:column;overflow:hidden;min-height:0;background:var(--panel)}
.reel-h{padding:11px 14px;border-bottom:1px solid var(--line-hot)}
.reel-h .t{font-size:10px;letter-spacing:.2em;color:var(--refuse);font-weight:700}
.reel-h .d{font-size:9.5px;color:var(--dim);margin-top:5px;line-height:1.5}
.reel{flex:1;min-height:0;overflow:hidden;padding:8px;display:flex;flex-direction:column;gap:6px;-webkit-mask-image:linear-gradient(180deg,#000 85%,transparent 100%);mask-image:linear-gradient(180deg,#000 85%,transparent 100%)}
.rf{border:1px solid #2a140e;border-left:2px solid var(--refuse);background:#120b09;padding:8px 10px;animation:snap .2s cubic-bezier(.2,1.4,.4,1)}
@keyframes snap{from{opacity:0;transform:translateX(14px) scale(.97)}to{opacity:1;transform:none}}
.rf .code{font-size:10.5px;color:var(--refuse);font-weight:700;letter-spacing:.03em}
.rf .why{font-size:10px;color:var(--mid);margin-top:4px;line-height:1.5}
.rf .fix{font-size:9.5px;color:var(--green);margin-top:5px}
.rf .meta{font-size:9px;color:#3f4e5e;margin-top:5px;display:flex;gap:8px}

/* ---------- gate matrix ---------- */
footer{grid-column:1/3;border-top:1px solid var(--line-hot);background:linear-gradient(0deg,#0b1119,#080c11);display:flex;align-items:stretch}
.gm-h{padding:11px 18px;border-right:1px solid var(--line);min-width:232px;display:flex;flex-direction:column;justify-content:center}
.gm-h .t{font-size:10px;letter-spacing:.2em;color:var(--mid);font-weight:600}
.gm-h .d{font-size:9px;color:var(--dim);margin-top:4px;line-height:1.5}
.gm{display:flex;flex:1}
.g{flex:1;border-right:1px solid var(--line);padding:11px 12px;position:relative;transition:background .18s}
.g:last-child{border-right:0}
.g .name{font-size:9.5px;letter-spacing:.1em;color:var(--mid);text-transform:uppercase}
.g .tally{font-size:9px;color:var(--dim);margin-top:6px;font-variant-numeric:tabular-nums}
.g .bar{position:absolute;left:0;bottom:0;height:2px;width:100%;background:var(--line)}
.g .bar i{display:block;height:100%;width:0;background:var(--refuse);transition:width .3s}
.g.pass{background:rgba(47,212,122,.07)}
.g.pass .name{color:var(--green)}
.g.fail{background:rgba(255,95,61,.13)}
.g.fail .name{color:var(--refuse)}
.g.fail::after{content:"REFUSED";position:absolute;top:10px;right:12px;font-size:8px;letter-spacing:.14em;color:var(--refuse)}

/* ---------- drawer ---------- */
.drawer{position:fixed;inset:0 0 0 auto;width:640px;max-width:92vw;background:#080c11;border-left:1px solid var(--line-hot);z-index:200;transform:translateX(100%);transition:transform .22s cubic-bezier(.3,.8,.3,1);display:flex;flex-direction:column;box-shadow:-30px 0 70px rgba(0,0,0,.7)}
.drawer.open{transform:none}
.drawer-h{padding:15px 18px;border-bottom:1px solid var(--line-hot);display:flex;align-items:center;gap:12px}
.drawer-h .t{font-size:12px;color:var(--bright);letter-spacing:.1em}
.drawer-h button{margin-left:auto}
.tl{flex:1;overflow:auto;padding:12px 18px}
.tl table{width:100%;border-collapse:collapse}
.tl td{padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top;font-size:11px}
.tl td.q{color:#3f4e5e;width:30px;text-align:right;font-variant-numeric:tabular-nums}
.tl td.a{color:var(--dim);width:78px;font-size:9px;letter-spacing:.12em;text-transform:uppercase}
.tl td.k{color:var(--mid);width:150px;font-size:10px}
.tl tr.bad td.s{color:#ffb9a7}
.tl tr.bad td.k{color:var(--refuse)}
.empty{color:var(--dim);font-size:10.5px;padding:16px 12px;line-height:1.6}
.gap{border:1px dashed #3a2a10;color:var(--amber);padding:6px 9px;font-size:10px}
</style>
</head>
<body>
<div id="app">
  <header>
    <div class="brand">
      <h1>RECOUP<em>/</em>CTRL</h1>
      <div class="sub">bounded revenue recovery</div>
    </div>
    <div class="readouts">
      <div class="ro money" id="ro-rec" data-src="Sum of amount_paise from every case_detected event whose case later recorded outcome_recorded. Recovery is case-level: the invoice was paid inside the observation window.">
        <div class="k">recovered</div><div class="v" id="v-rec">&#8377;0</div></div>
      <div class="ro" id="ro-spent" data-src="Sum of cost_paise across notice_sent events. Message cost only; the stand-in agent buys no tokens, and this page will not display a spend that was not incurred.">
        <div class="k">spent</div><div class="v" id="v-spent">&#8377;0</div></div>
      <div class="ro refuse" data-src="Count of action_refused events. Every one is a money action the policy engine declined to take, with a named rule and a named remedy.">
        <div class="k">refused</div><div class="v" id="v-ref">0</div></div>
      <div class="ro gates" data-src="Sum of the gates array across every policy_evaluated and action_refused event. All eight gates run on every decision -- no short-circuit -- which is what makes the passes evidence too.">
        <div class="k">gates run</div><div class="v" id="v-gates">0</div></div>
      <div class="ro" data-src="Spent divided by recovered, both derived above. The measured figure across the 900-case R1 batch was Rs 0.0028 per rupee recovered.">
        <div class="k">cost per &#8377;1</div><div class="v" id="v-cpr">&#8377;0.0000</div></div>
      <div class="ro" data-src="Distinct case_id values seen on the stream, against the batch size the run was started with. The stream is live; rendering is paced, so this trails the run until the queue drains.">
        <div class="k">cases</div><div class="v" id="v-cases">0</div></div>
    </div>
    <div class="controls">
      <div class="live"><span class="dot" id="dot"></span><span id="status">connecting</span></div>
      <div class="pace" id="pace">
        <button data-r="6">6/s</button><button data-r="18" class="on">18/s</button><button data-r="0">MAX</button>
      </div>
      <button class="go" id="run">Run batch</button>
    </div>
  </header>

  <main id="lanes"></main>

  <aside>
    <div class="reel-h">
      <div class="t">REFUSAL REEL</div>
      <div class="d">What the system declined to do, and the rule that stopped it. Restraint is the product.</div>
    </div>
    <div class="reel" id="reel"><div class="empty">No refusals yet. Start a batch.</div></div>
  </aside>

  <footer>
    <div class="gm-h">
      <div class="t">GATE MATRIX</div>
      <div class="d">All eight, every decision. Live cell = the most recent evaluation.</div>
    </div>
    <div class="gm" id="gm"></div>
  </footer>
</div>

<div class="drawer" id="drawer">
  <div class="drawer-h"><div class="t" id="drawer-t">case</div><button onclick="closeDrawer()">Close</button></div>
  <div class="tl" id="drawer-b"></div>
</div>

<script>
const LANES=[["01","UNDERSTAND",["case_detected","diagnosis_produced"]],
             ["02","DECIDE",["actions_proposed","arm_assigned","state_changed"]],
             ["03","GOVERN",["policy_evaluated","action_refused"]],
             ["04","ACT",["action_executed","notice_sent","action_scheduled","action_deduped","provider_callback"]],
             ["05","PROVE",["outcome_recorded","case_stopped","correction"]]];
const GATES=["consent","suppression","mandate","attempt_budget","quiet_hours","cooldown","template","channel_economics"];
const MAX_CARDS=26, MAX_REEL=14;

const laneOf={}, laneEl={}, laneCount={};
const gm={}, gTally={};
let rec=0, spent=0, refused=0, gatesRun=0, total=0, batchSize=0;
const amounts={}, credited={};
const queue=[]; let rate=18, drainTimer=null;

(function build(){
  const m=document.getElementById("lanes");
  for(const [n,name,kinds] of LANES){
    kinds.forEach(k=>laneOf[k]=name);
    const d=document.createElement("div"); d.className="lane"; d.dataset.lane=name;
    d.innerHTML=`<div class="lane-h"><span class="n">${n}</span><span class="t">${name}</span><span class="c" id="c-${name}">0</span></div><div class="stream" id="s-${name}"></div>`;
    m.appendChild(d); laneEl[name]=null; laneCount[name]=0;
  }
  LANES.forEach(([,name])=>laneEl[name]=document.getElementById("s-"+name));
  const g=document.getElementById("gm");
  for(const name of GATES){
    gTally[name]={pass:0,fail:0};
    const d=document.createElement("div"); d.className="g"; d.id="g-"+name;
    d.innerHTML=`<div class="name">${name.replace(/_/g," ")}</div><div class="tally" id="t-${name}">0 / 0</div><div class="bar"><i id="b-${name}"></i></div>`;
    g.appendChild(d);
  }
})();

const inr=p=>"₹"+Math.round(p/100).toLocaleString("en-IN");
const set=(id,v)=>document.getElementById(id).textContent=v;
const cases=()=>set("v-cases",Object.keys(amounts).length+(batchSize?" / "+batchSize:""));
const cpr=()=>set("v-cpr","₹"+(rec?spent/rec:0).toFixed(4));

function classOf(e){
  if(e.kind==="action_refused") return "bad";
  if(e.kind==="outcome_recorded") return "money";
  if(e.kind==="action_executed") return /succeeded|recovered/.test(e.summary)?"ok":"";
  return e.actor;
}

/* Consecutive identical events on one case collapse into a single card with a
   count. The stand-in agent is stateless -- the prompt has no "a link was
   already sent" field -- so it will legitimately re-propose the same action,
   and a real model on the same prompt does too. Collapsing is a readability
   fix in the view; every event is still counted, gated and tallied above. */
function collapse(s,e){
  const top=s.firstChild;
  if(!top||top.dataset.case!==e.case_id||top.dataset.summary!==e.summary) return false;
  const n=(+top.dataset.n||1)+1;
  top.dataset.n=n;
  let chip=top.querySelector(".rep");
  if(!chip){ chip=document.createElement("span"); chip.className="rep"; top.querySelector(".top").appendChild(chip); }
  chip.textContent="×"+n;
  return true;
}

function render(e){
  const lane=laneOf[e.kind]; if(!lane) return;
  const s=laneEl[lane];
  laneCount[lane]++; set("c-"+lane,laneCount[lane]);
  if(collapse(s,e)) return;
  const d=document.createElement("div");
  d.className="ev "+classOf(e);
  d.setAttribute("role","button"); d.tabIndex=0;
  d.dataset.case=e.case_id; d.dataset.summary=e.summary;
  d.innerHTML=`<div class="top"><span class="actor">${e.actor}</span><span class="case">${e.case_id.replace("case_","#")}</span></div><div class="s"></div>`;
  d.querySelector(".s").textContent=e.summary;
  d.onclick=()=>openCase(e.case_id);
  d.onkeydown=k=>{if(k.key==="Enter"||k.key===" "){k.preventDefault();openCase(e.case_id)}};
  s.insertBefore(d,s.firstChild);
  while(s.children.length>MAX_CARDS) s.removeChild(s.lastChild);
}

function applyGates(p){
  if(!p||!p.gates) return;
  for(const g of p.gates){
    gatesRun++;
    const cell=document.getElementById("g-"+g.gate); if(!cell) continue;
    const t=gTally[g.gate]; g.passed?t.pass++:t.fail++;
    cell.classList.remove("pass","fail");
    void cell.offsetWidth;
    cell.classList.add(g.passed?"pass":"fail");
    set("t-"+g.gate, t.pass+" / "+t.fail);
    const total=t.pass+t.fail;
    document.getElementById("b-"+g.gate).style.width=(total?100*t.fail/total:0)+"%";
  }
  set("v-gates",gatesRun.toLocaleString());
}

function reel(e){
  const box=document.getElementById("reel");
  const first=box.querySelector(".empty"); if(first) first.remove();
  const gates=(e.payload&&e.payload.gates)||[];
  const bad=gates.filter(g=>!g.passed);
  const d=document.createElement("div"); d.className="rf";
  const code=bad.length?bad.map(g=>g.gate+"="+(g.code||"refused")).join("  "):"schema";
  const why=bad.length?bad[0].explanation:e.summary;
  const fix=bad.find(g=>g.remediation);
  d.innerHTML=`<div class="code"></div><div class="why"></div>${fix?'<div class="fix"></div>':""}<div class="meta"><span></span><span></span></div>`;
  d.querySelector(".code").textContent=code;
  d.querySelector(".why").textContent=why;
  if(fix) d.querySelector(".fix").textContent="→ unblocked by: "+fix.remediation;
  const meta=d.querySelectorAll(".meta span");
  meta[0].textContent=e.case_id.replace("case_","#");
  meta[1].textContent=amounts[e.case_id]!==undefined?inr(amounts[e.case_id])+" not chased":"";
  d.setAttribute("role","button"); d.tabIndex=0;
  d.onclick=()=>openCase(e.case_id);
  d.onkeydown=k=>{if(k.key==="Enter"||k.key===" "){k.preventDefault();openCase(e.case_id)}};
  box.insertBefore(d,box.firstChild);
  while(box.children.length>MAX_REEL) box.removeChild(box.lastChild);
}

function ingest(e){
  total++;
  if(e.kind==="case_detected"&&e.payload&&e.payload.amount_paise!==undefined){
    amounts[e.case_id]=e.payload.amount_paise;
    cases();
  }
  if(e.kind==="outcome_recorded"&&!credited[e.case_id]&&amounts[e.case_id]!==undefined){
    credited[e.case_id]=1; rec+=amounts[e.case_id]; set("v-rec",inr(rec)); cpr();
  }
  if(e.kind==="notice_sent"&&e.payload&&e.payload.cost_paise){
    spent+=e.payload.cost_paise; set("v-spent",inr(spent)); cpr();
  }
  if(e.kind==="action_refused"){ refused++; set("v-ref",refused); reel(e); }
  applyGates(e.payload);
  render(e);
}

/* Render pacing. The stream is live; only the eye is throttled, so a run that
   finishes in two seconds is still readable. Nothing is dropped or reordered. */
function drain(){
  if(rate===0){ while(queue.length) ingest(queue.shift()); return; }
  const n=Math.max(1,Math.round(rate/10));
  for(let i=0;i<n&&queue.length;i++) ingest(queue.shift());
}
drainTimer=setInterval(drain,100);
document.getElementById("pace").onclick=ev=>{
  const b=ev.target.closest("button"); if(!b) return;
  rate=+b.dataset.r;
  [...ev.currentTarget.children].forEach(c=>c.classList.toggle("on",c===b));
};

/* stream */
const dot=document.getElementById("dot");
const es=new EventSource("/api/events");
es.onopen=()=>{dot.classList.add("on");set("status","live")};
es.onerror=()=>{dot.classList.remove("on");set("status","reconnecting")};
es.addEventListener("audit",m=>queue.push(JSON.parse(m.data)));
es.addEventListener("gap",m=>{
  const box=document.getElementById("reel");
  const d=document.createElement("div"); d.className="gap";
  d.textContent=JSON.parse(m.data).missed+" events not shown — this viewer fell behind";
  box.insertBefore(d,box.firstChild);
});

document.getElementById("run").onclick=async ev=>{
  const b=ev.target; b.disabled=true; b.textContent="Running";
  try{
    const r=await fetch("/api/run",{method:"POST",headers:{"X-Recoup-Console":"1"}});
    if(r.status===409) b.textContent="Already running";
  }finally{
    setTimeout(()=>{b.disabled=false;b.textContent="Run batch"},2500);
  }
};

async function openCase(id){
  const d=document.getElementById("drawer");
  document.getElementById("drawer-t").textContent=id;
  const body=document.getElementById("drawer-b");
  body.innerHTML='<div class="empty">loading…</div>';
  d.classList.add("open");
  const r=await fetch("/api/case/"+encodeURIComponent(id));
  if(!r.ok){ body.innerHTML='<div class="empty">no timeline for this case</div>'; return; }
  const {events}=await r.json();
  const t=document.createElement("table");
  for(const e of events){
    const tr=document.createElement("tr");
    if(e.kind==="action_refused") tr.className="bad";
    for(const [cls,val] of [["q",e.seq],["a",e.actor],["k",e.kind],["s",e.summary]]){
      const td=document.createElement("td"); td.className=cls; td.textContent=val; tr.appendChild(td);
    }
    t.appendChild(tr);
  }
  body.innerHTML=""; body.appendChild(t);
}
function closeDrawer(){document.getElementById("drawer").classList.remove("open")}
addEventListener("keydown",e=>{if(e.key==="Escape")closeDrawer()});

fetch("/api/state").then(r=>r.json()).then(s=>{
  batchSize=s.cases_total; cases();
  if(s.status==="running") document.getElementById("run").textContent="Running";
});
</script>
</body>
</html>
"""


def render_console() -> str:
    """The control room, as one document."""
    return _CONSOLE
