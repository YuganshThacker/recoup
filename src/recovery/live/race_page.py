"""The counterfactual race, as a page.

Deliberately simpler than the console. The console is an instrument with a lot
happening at once; this has one job, and a viewer has about twenty seconds to
understand it: same case, same world, two policies, different outcome.

Everything on the page is rendered from ``/api/race``. No figure is written into
the markup -- the divergence rate especially, which exists to stop a single
winning case reading as a cherry-picked one and would be worthless if it were a
string in a template.
"""

from __future__ import annotations

_STYLE = """
:root{
  --ground:#06080b;--panel:#0a0e13;--line:#151f2a;--line-hot:#1e2c3b;
  --dim:#546578;--mid:#8397a8;--text:#c7d4e0;--bright:#eaf2f9;
  --blue:#3395ff;--green:#2fd47a;--refuse:#ff5f3d;--amber:#ffb020;
  --mono:ui-monospace,"SF Mono","JetBrains Mono","Fira Code",Menlo,Consolas,monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{background:var(--ground);color:var(--text);font:13px/1.5 var(--mono);overflow:hidden}
body::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:0;opacity:.4;
  background-image:linear-gradient(var(--line) 1px,transparent 1px),linear-gradient(90deg,var(--line) 1px,transparent 1px);
  background-size:60px 60px;mask-image:radial-gradient(ellipse 110% 80% at 50% 0%,#000 25%,transparent 72%)}
#app{position:relative;z-index:1;height:100%;display:flex;flex-direction:column;padding:26px 34px 20px;gap:16px;max-width:1400px;margin:0 auto}

h1{font-size:15px;letter-spacing:.3em;color:var(--bright);font-weight:700;text-align:center}
.sub{font-size:10.5px;letter-spacing:.2em;color:var(--dim);text-transform:uppercase;text-align:center;margin-top:7px}

.truth{border:1px solid var(--line-hot);background:linear-gradient(180deg,#0b1119,#080c11);padding:13px 18px;
  display:flex;align-items:center;justify-content:center;gap:26px;flex-wrap:wrap}
.truth .lbl{font-size:9px;letter-spacing:.22em;color:var(--amber);text-transform:uppercase}
.truth .v{font-size:15px;color:var(--bright)}
.truth .v b{color:var(--amber);font-weight:600}
.truth .sep{color:var(--line-hot)}

.arms{flex:1;min-height:0;display:grid;grid-template-columns:1fr 1fr;gap:18px}
.arm{border:1px solid var(--line);display:flex;flex-direction:column;min-height:0;background:var(--panel)}
.arm-h{padding:12px 16px;border-bottom:1px solid var(--line);display:flex;align-items:baseline;gap:10px}
.arm-h .n{font-size:12.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--mid);font-weight:600}
.arm-h .p{margin-left:auto;font-size:9px;color:#3f4e5e}
.arm.win{border-color:#14432b}
.arm.win .arm-h{background:rgba(47,212,122,.06)}
.arm.win .arm-h .n{color:var(--green)}
.arm.lose{border-color:#3a1a12}
.arm.lose .arm-h{background:rgba(255,95,61,.06)}
.arm.lose .arm-h .n{color:var(--refuse)}

.rows{flex:1;min-height:0;overflow:hidden;padding:10px 0}
.row{display:flex;gap:13px;padding:5px 16px;align-items:baseline;opacity:0;animation:in .22s ease-out forwards}
@keyframes in{from{opacity:0;transform:translateX(-7px)}to{opacity:1;transform:none}}
.row .d{flex:0 0 52px;font-size:10px;letter-spacing:.09em;color:#3f4e5e;text-transform:uppercase}
.row .s{font-size:12.5px;color:var(--text);line-height:1.45}
.row.debit .s{color:var(--amber)}
.row.fail .s{color:var(--refuse)}
.row.good .s{color:var(--green);font-weight:600}
.row.stop .s{color:var(--refuse);font-weight:600}
.row.mark .d{color:var(--blue)}

.out{border-top:1px solid var(--line);padding:14px 16px;display:flex;align-items:baseline;gap:14px}
.out .k{font-size:9px;letter-spacing:.2em;color:var(--dim);text-transform:uppercase}
.out .v{font-size:26px;font-weight:600;letter-spacing:-.01em;font-variant-numeric:tabular-nums}
.arm.win .out .v{color:var(--green)}
.arm.lose .out .v{color:var(--refuse)}
.out .cost{margin-left:auto;text-align:right;font-size:10.5px;color:var(--dim);line-height:1.6}

footer{display:flex;align-items:center;gap:18px;flex-wrap:wrap;justify-content:center;text-align:center}
.stat{font-size:11px;color:var(--mid)}
.stat b{color:var(--bright);font-weight:600}
.note{font-size:10px;color:var(--dim);max-width:76ch;line-height:1.6}
.sim{font-size:9px;letter-spacing:.19em;color:var(--amber);border:1px solid #3a2a10;padding:5px 10px;text-transform:uppercase}
button{font:inherit;color:var(--text);background:#101821;border:1px solid var(--line-hot);
  padding:9px 20px;cursor:pointer;letter-spacing:.16em;font-size:10px;text-transform:uppercase;transition:.14s}
button:hover{border-color:var(--blue);color:var(--bright);background:#13202e}
button:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
.loading{grid-column:1/3;display:flex;align-items:center;justify-content:center;color:var(--dim);font-size:11px}
@media (prefers-reduced-motion:reduce){.row{animation:none;opacity:1}}
"""

_SCRIPT = r"""
const inr = p => "₹" + Math.round(p/100).toLocaleString("en-IN");
const el = document.getElementById.bind(document);

function classify(e){
  const s = e.summary;
  if (/recovered/.test(s)) return "good";
  if (/debit succeeded/.test(s)) return "good";
  if (/debit failed/.test(s)) return "fail";
  if (/^stopped:/.test(s)) return "stop";
  if (/retry_debit|debit/.test(s)) return "debit";
  return "";
}

/* Only the lines a viewer can follow in twenty seconds. The full ledger is in
   the console and the audit report; repeating it here would bury the story. */
function readable(events){
  return events.filter(e =>
    e.kind === "case_detected" ||
    e.kind === "notice_sent" ||
    e.kind === "action_executed" ||
    e.kind === "outcome_recorded" ||
    e.kind === "case_stopped");
}

function label(e){
  const s = e.summary;
  if (e.kind === "case_detected") return "payment fails";
  if (e.kind === "notice_sent") return "pre-debit notice";
  if (/debit succeeded/.test(s)) return "retry → succeeded";
  if (/debit failed/.test(s)) return "retry → failed";
  if (/^sent /.test(s)) return s.replace(/^sent /, "sent ");
  if (e.kind === "case_stopped") return s.replace("stopped: ", "stopped — ");
  if (e.kind === "outcome_recorded") return s;
  return s;
}

function paint(arm, node, speed){
  const box = node.querySelector(".rows");
  box.innerHTML = "";
  const rows = readable(arm.events);
  rows.forEach((e, i) => {
    const d = document.createElement("div");
    d.className = "row " + classify(e);
    d.style.animationDelay = (i * speed) + "ms";
    d.innerHTML = '<span class="d"></span><span class="s"></span>';
    d.querySelector(".d").textContent = "day " + e.day;
    d.querySelector(".s").textContent = label(e);
    box.appendChild(d);
  });
  return rows.length;
}

async function load(){
  const r = await fetch("/api/race" + location.search);
  const d = await r.json();
  if (d.error) { el("arms").innerHTML = '<div class="loading">' + d.error + "</div>"; return; }

  el("truth-amount").textContent = inr(d.amount_paise);
  el("truth-reason").textContent = d.decline_reason;
  el("truth-day").textContent = d.recoverable_from_day === null
    ? "never recoverable" : "day " + d.recoverable_from_day;
  el("case-id").textContent = d.case_id;

  el("stat").innerHTML = "";
  const stat = document.createElement("span");
  stat.className = "stat";
  stat.innerHTML = "<b></b> of <b></b> cases diverged &middot; <b></b> &middot; the planner is the only variable";
  const b = stat.querySelectorAll("b");
  b[0].textContent = d.diverged; b[1].textContent = d.total;
  b[2].textContent = (d.rate * 100).toFixed(1) + "%";
  el("stat").appendChild(stat);
  el("note").textContent = d.note;

  render(d, 0);
  el("replay").onclick = () => render(d, 180);
}

function render(d, speed){
  for (const [side, arm] of [["left", d.default], ["right", d.recoup]]){
    const node = el(side);
    node.className = "arm " + (arm.recovered ? "win" : "lose");
    node.querySelector(".n").textContent = arm.label;
    node.querySelector(".p").textContent = arm.planner;
    paint(arm, node, speed);
    node.querySelector(".v").textContent = arm.recovered ? inr(arm.recovered_paise) : inr(0);
    node.querySelector(".k").textContent = arm.recovered ? "recovered" : "lost";
    node.querySelector(".cost").textContent =
      arm.attempts + " debit attempt" + (arm.attempts === 1 ? "" : "s")
      + " · " + arm.messages + " message" + (arm.messages === 1 ? "" : "s");
  }
}

load();
"""


def render_race() -> str:
    """One self-contained page. Every figure comes from the API."""
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Counterfactual race</title>"
        "<link rel='icon' href=\"data:image/svg+xml,"
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
        "<rect width='32' height='32' fill='%2306080b'/>"
        "<rect x='5' y='9' width='9' height='4' fill='%23ff5f3d'/>"
        "<rect x='5' y='19' width='22' height='4' fill='%232fd47a'/></svg>\">"
        f"<style>{_STYLE}</style></head><body><div id='app'>"
        "<div><h1>COUNTERFACTUAL RACE</h1>"
        "<div class='sub'>same customer &middot; same world &middot; "
        "<span id='case-id'></span></div></div>"
        "<div class='truth'>"
        "<span class='lbl'>ground truth</span>"
        "<span class='v'><b id='truth-amount'></b> outstanding</span>"
        "<span class='sep'>|</span>"
        "<span class='v' id='truth-reason'></span>"
        "<span class='sep'>|</span>"
        "<span class='v'>money available <b id='truth-day'></b></span>"
        "</div>"
        "<div class='arms' id='arms'>" + _arm_markup("left") + _arm_markup("right") + "</div>"
        "<footer>"
        "<span id='stat'></span>"
        "<button id='replay'>Replay</button>"
        "<span class='sim'>simulation</span>"
        "<div class='note' id='note'></div>"
        "</footer>"
        f"</div><script>{_SCRIPT}</script></body></html>"
    )


def _arm_markup(side: str) -> str:
    return (
        f"<div class='arm' id='{side}'>"
        "<div class='arm-h'><span class='n'></span><span class='p'></span></div>"
        "<div class='rows'></div>"
        "<div class='out'><span class='k'></span><span class='v'></span>"
        "<span class='cost'></span></div>"
        "</div>"
    )
