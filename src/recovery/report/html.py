"""Render the audit report as one self-contained HTML file.

The track's bar asks for an audit trail. A ledger in memory is not one that
anybody can inspect, so this turns it into a file that opens in a browser with
no server, no build step, and no network -- which also means it can be
committed and read by someone who never runs the code.

Everything is inlined deliberately. A report that depends on a CDN is a report
that stops working the day it matters.
"""

from __future__ import annotations

import html
import json
from typing import Any

_CSS = """
:root{--bg:#0b0f14;--panel:#121821;--line:#1e2733;--ink:#e6edf3;--dim:#8b98a5;
--ok:#3fb950;--bad:#f85149;--warn:#d29922;--accent:#58a6ff;--mono:ui-monospace,
SFMono-Regular,"SF Mono",Menlo,Consolas,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1280px;margin:0 auto;padding:28px 20px 80px}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:15px;margin:32px 0 12px;color:var(--dim);text-transform:uppercase;
letter-spacing:.08em;font-weight:600}
.sub{color:var(--dim);font-size:13px;margin-bottom:8px}
.void{background:rgba(248,81,73,.1);border:1px solid var(--bad);color:#ffb3ae;
padding:12px 14px;border-radius:8px;margin:16px 0;font-size:13px}
.note{background:rgba(210,153,34,.08);border:1px solid rgba(210,153,34,.35);
color:#e8c46a;padding:12px 14px;border-radius:8px;margin:16px 0;font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.07em}
.card .v{font:600 24px/1.25 var(--mono);margin-top:6px}
.card .m{color:var(--dim);font-size:12px;font-family:var(--mono);margin-top:4px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
td.num,th.num{text-align:right;font-family:var(--mono)}
.scroll{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:10px}
tr.case{cursor:pointer}
tr.case:hover{background:#182231}
tr.case.on{background:#1b2740}
.pill{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;
font-family:var(--mono);border:1px solid var(--line)}
.ok{color:var(--ok);border-color:rgba(63,185,80,.4)}
.bad{color:var(--bad);border-color:rgba(248,81,73,.4)}
.warn{color:var(--warn);border-color:rgba(210,153,34,.4)}
.dim{color:var(--dim)}
.controls{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}
input,select{background:var(--panel);border:1px solid var(--line);color:var(--ink);
padding:7px 10px;border-radius:7px;font-size:13px;font-family:inherit}
#tl{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:16px;margin-top:12px;min-height:120px}
.ev{border-left:2px solid var(--line);padding:0 0 14px 16px;margin-left:6px;position:relative}
.ev:last-child{padding-bottom:0}
.ev::before{content:"";position:absolute;left:-5px;top:5px;width:8px;height:8px;
border-radius:50%;background:var(--dim)}
.ev.refuse::before{background:var(--bad)}
.ev.act::before{background:var(--accent)}
.ev.done::before{background:var(--ok)}
.ev .h{font-family:var(--mono);font-size:12px;color:var(--dim)}
.ev .s{margin-top:2px}
.gates{margin-top:8px;display:flex;flex-wrap:wrap;gap:5px}
.gate{font-family:var(--mono);font-size:11px;padding:2px 7px;border-radius:5px;
border:1px solid var(--line);color:var(--dim)}
.gate.p{color:var(--ok);border-color:rgba(63,185,80,.3)}
.gate.f{color:var(--bad);border-color:rgba(248,81,73,.45);background:rgba(248,81,73,.08)}
.why{margin-top:6px;font-size:12px;color:#ffb3ae;font-family:var(--mono)}
.foot{margin-top:40px;color:var(--dim);font-size:12px;border-top:1px solid var(--line);
padding-top:16px}
"""

_JS = """
const D = window.__AUDIT__;
const rows = D.cases, tls = D.timelines;
const tbody = document.getElementById('rows');
const tl = document.getElementById('tl');

function pill(t, c){ return '<span class="pill '+c+'">'+t+'</span>'; }

function render(){
  const q = document.getElementById('q').value.trim().toLowerCase();
  const f = document.getElementById('f').value;
  let out = '';
  let shown = 0;
  for (const c of rows){
    if (f === 'refused' && !c.refusals.length) continue;
    if (f === 'agent' && !c.tail_arm) continue;
    if (f === 'recovered' && !c.recovered) continue;
    if (f === 'traced' && !tls[c.id]) continue;
    if (q && !(c.id+' '+c.reason+' '+c.klass+' '+(c.stop_reason||'')).toLowerCase().includes(q)) continue;
    if (++shown > 400) break;
    const res = c.recovered
      ? pill(c.source, c.source === 'organic' ? 'warn' : 'ok')
      : pill(c.stop_reason || 'none', 'bad');
    out += '<tr class="case" data-id="'+c.id+'">'
      + '<td class="dim" style="font-family:var(--mono)">'+c.id.replace('case_','')+'</td>'
      + '<td>'+pill(c.klass, c.klass==='hard'?'bad':c.klass==='soft'?'ok':'warn')+'</td>'
      + '<td class="dim">'+c.reason+'</td>'
      + '<td class="num">'+c.amount+'</td>'
      + '<td>'+c.arm+(c.tail_arm ? ' <span class="dim">/ '+c.tail_arm.replace('_',' ')+'</span>' : '')+'</td>'
      + '<td class="num">'+c.attempts+'</td>'
      + '<td>'+res+'</td>'
      + '<td class="dim">'+(c.refusals.length ? c.refusals.length+'\\u00d7' : '')+'</td>'
      + '</tr>';
  }
  tbody.innerHTML = out || '<tr><td colspan="8" class="dim">no cases match</td></tr>';
  document.getElementById('count').textContent = shown + ' shown of ' + rows.length;
}

function show(id){
  document.querySelectorAll('tr.case').forEach(r => r.classList.toggle('on', r.dataset.id === id));
  const ev = tls[id];
  if (!ev){ tl.innerHTML = '<div class="dim">No timeline embedded for this case. '
    + 'Timelines are sampled &mdash; see the note above.</div>'; return; }
  const c = rows.find(r => r.id === id);
  let h = '<div style="margin-bottom:14px"><b>'+c.id+'</b> &middot; '
    + c.amount+' &middot; '+c.reason+' ('+c.klass+') &middot; '+c.arm
    + (c.tail_arm ? ' / '+c.tail_arm.replace('_',' ') : '')+'</div>';
  for (const e of ev){
    const refused = e.kind === 'action_refused';
    const cls = refused ? 'refuse' : (e.kind === 'outcome_recorded' ? 'done'
      : (e.kind === 'action_executed' || e.kind === 'notice_sent') ? 'act' : '');
    h += '<div class="ev '+cls+'">';
    h += '<div class="h">'+e.seq+' &middot; '+e.at+' &middot; '+e.actor+' &middot; '+e.kind+'</div>';
    h += '<div class="s">'+e.summary+'</div>';
    if (e.gates){
      h += '<div class="gates">';
      for (const g of e.gates){
        h += '<span class="gate '+(g.passed?'p':'f')+'">'+g.gate+(g.passed?'':' \\u2717')+'</span>';
      }
      h += '</div>';
      for (const g of e.gates){
        if (!g.passed){
          h += '<div class="why">'+g.code+': '+g.explanation
            + (g.remediation ? ' \\u2192 unblocked by '+g.remediation : '')+'</div>';
        }
      }
    }
    h += '</div>';
  }
  tl.innerHTML = h;
}

tbody.addEventListener('click', e => {
  const tr = e.target.closest('tr.case');
  if (tr) show(tr.dataset.id);
});
document.getElementById('q').addEventListener('input', render);
document.getElementById('f').addEventListener('change', render);
render();
const first = Object.keys(tls)[0];
if (first) show(first);
"""


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _lift_card(title: str, lift: dict[str, Any], extra: str = "") -> str:
    sign = "ok" if lift["value"] > 0 and not lift["straddles_zero"] else "warn"
    if lift["value"] < 0 and not lift["straddles_zero"]:
        sign = "bad"
    ci = f"95% CI [{lift['ci_low']:+.4f}, {lift['ci_high']:+.4f}]"
    if lift["straddles_zero"]:
        ci += " — straddles zero"
    return (
        f'<div class="card"><div class="k">{_esc(title)}</div>'
        f'<div class="v {sign}">{lift["value"]:+.4f}</div>'
        f'<div class="m">{_esc(ci)}</div>'
        f'<div class="m">n={lift["treatment_n"]}/{lift["control_n"]}{_esc(extra)}</div></div>'
    )


def _counts_table(title: str, counts: dict[str, int]) -> str:
    if not counts:
        return ""
    rows = "".join(
        f"<tr><td>{_esc(k)}</td><td class='num'>{v}</td></tr>" for k, v in counts.items()
    )
    return (
        f"<h2>{_esc(title)}</h2><div class='scroll'><table>"
        f"<tr><th>{_esc(title)}</th><th class='num'>count</th></tr>{rows}</table></div>"
    )


def render(payload: dict[str, Any]) -> str:
    """Produce the complete HTML document."""
    p = payload["primary"]
    lift = p["lift"]
    mode = payload["agent_mode"]
    model = payload["model"] or "—"

    void = ""
    if payload.get("void_reason"):
        void = f'<div class="void"><b>R2 ABLATION VOID.</b> {_esc(payload["void_reason"])}</div>'

    cards = [
        f'<div class="card"><div class="k">Recovery rate — treatment</div>'
        f'<div class="v">{lift["treatment_rate"]:.4f}</div>'
        f'<div class="m">n={lift["treatment_n"]}</div></div>',
        f'<div class="card"><div class="k">Recovery rate — holdout</div>'
        f'<div class="v dim">{lift["control_rate"]:.4f}</div>'
        f'<div class="m">n={lift["control_n"]}</div></div>',
        _lift_card("Lift (primary)", lift),
        f'<div class="card"><div class="k">Incremental recovered</div>'
        f'<div class="v ok">{_esc(p["incremental"])}</div>'
        f'<div class="m">gross {_esc(p["gross"])}</div>'
        f'<div class="m">outreach {_esc(p["outreach_cost"])}</div></div>',
    ]

    class_rows = "".join(
        f"<tr><td>{_esc(k)}</td>"
        f"<td class='num'>{v['treatment_rate']:.3f}</td>"
        f"<td class='num'>{v['control_rate']:.3f}</td>"
        f"<td class='num'>{v['value']:+.3f}</td>"
        f"<td class='num dim'>{v['treatment_n']}/{v['control_n']}</td></tr>"
        for k, v in payload["by_class"].items()
    )

    ablation = payload["ablation"]
    ab_cards = "".join(_lift_card(f"tail — {k}", v) for k, v in ablation["by_subtype"].items())

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Audit report — {_esc(payload["label"])}</title>
<style>{_CSS}</style></head><body><div class="wrap">

<h1>Recovery audit — {_esc(payload["label"])}</h1>
<div class="sub">seed {payload["seed"]} · agent mode <b>{_esc(mode)}</b> · model {_esc(model)}
· {payload["n"]} cases</div>

<div class="note">Simulation results. The world model encodes the hypothesis that retry
timing matters, so these runs cannot confirm that hypothesis — only show that the policy
exploits the structure it is given, and that the machinery and measurement work end to
end. See <code>src/recovery/sim/world.py</code>.</div>

<h2>Primary — did the system beat the platform default?</h2>
<div class="grid">{"".join(cards)}</div>

<h2>Recovery by decline class</h2>
<div class="scroll"><table>
<tr><th>class</th><th class="num">treatment</th><th class="num">holdout</th>
<th class="num">delta</th><th class="num">n</th></tr>{class_rows}</table></div>

<h2>Secondary — did the model beat the deterministic fallback?</h2>
{void}
<div class="grid">{_lift_card("tail — overall", ablation["overall"])}{ab_cards}</div>

{_counts_table("Policy refusals by gate code", payload["refusals"])}
{_counts_table("Stopping rules fired", payload["stops"])}
{_counts_table("Exception list", payload["exceptions"])}

<h2>Case ledger — click a row for its full audit trail</h2>
<div class="sub">{_esc(payload["timeline_note"])}</div>
<div class="controls">
  <input id="q" placeholder="filter by id, reason, class, stop reason…" style="min-width:280px">
  <select id="f">
    <option value="traced">with embedded timeline</option>
    <option value="all">all cases</option>
    <option value="refused">had a policy refusal</option>
    <option value="agent">model was in the loop</option>
    <option value="recovered">recovered</option>
  </select>
  <span class="dim" id="count" style="align-self:center"></span>
</div>
<div class="scroll"><table>
<tr><th>case</th><th>class</th><th>reason</th><th class="num">amount</th><th>arm</th>
<th class="num">attempts</th><th>outcome</th><th>refusals</th></tr>
<tbody id="rows"></tbody></table></div>

<div id="tl"></div>

<div class="foot">Generated by <code>python -m recovery.batch --report</code>. Every figure
here is computed from the run named above; nothing is illustrative. Metric hierarchy is
fixed in <code>docs/analysis-plan.md</code>, which was pushed before any batch data
existed.</div>

</div>
<script>window.__AUDIT__ = {json.dumps(payload, separators=(",", ":"))};</script>
<script>{_JS}</script>
</body></html>"""
