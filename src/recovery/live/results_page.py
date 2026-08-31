"""The three results, in one frame.

Built to be filmed. Large type, high contrast, nothing animates on load, and it
holds still for as long as the camera needs. The one thing on it that is not a
number is the mapping underneath -- what each part of the system is for -- which
is the claim the numbers above are there to support.

R2 is rendered in the refusal colour and stated plainly as the model losing. A
results screen that made every row look like a win would be the exact thing this
project spends its effort not being.
"""

from __future__ import annotations

from html import escape

from recovery.live.results import MAPPING, Result, Results

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
  background-size:64px 64px;mask-image:radial-gradient(ellipse 100% 75% at 50% 0%,#000 25%,transparent 74%)}
#app{position:relative;z-index:1;height:100%;display:flex;flex-direction:column;
  justify-content:center;gap:40px;padding:40px 46px;max-width:1500px;margin:0 auto}

.head{text-align:center}
h1{font-size:15px;letter-spacing:.34em;color:var(--bright);font-weight:700}
.head .s{font-size:10.5px;letter-spacing:.2em;color:var(--dim);text-transform:uppercase;margin-top:9px}

.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:20px}
.r{border:1px solid var(--line);background:var(--panel);padding:24px 24px 20px;display:flex;
  flex-direction:column;gap:0;position:relative;overflow:hidden}
.r::after{content:"";position:absolute;left:0;top:0;bottom:0;width:2px;background:var(--dim)}
.r.system::after{background:var(--blue)}
.r.model_wins::after{background:var(--green)}
.r.model_loses::after{background:var(--refuse)}
.r.off::after{background:var(--line-hot)}
.r .k{font-size:11px;letter-spacing:.24em;color:var(--dim);font-weight:700}
.r .q{font-size:12.5px;color:var(--mid);margin-top:10px;line-height:1.55;min-height:3.1em}
.r .v{font-size:52px;line-height:1;font-weight:600;letter-spacing:-.03em;margin-top:16px;
  font-variant-numeric:tabular-nums;color:var(--bright)}
.r.system .v{color:var(--blue)}
.r.model_wins .v{color:var(--green)}
.r.model_loses .v{color:var(--refuse)}
.r.off .v{font-size:22px;color:var(--dim)}
.r .d{font-size:12px;color:var(--mid);margin-top:12px;font-variant-numeric:tabular-nums}
.r .rate{font-size:12px;color:var(--mid);margin-top:7px}
.r .rate b{color:var(--bright);font-weight:600}
.r .verdict{font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;margin-top:14px}
.r.system .verdict{color:var(--blue)}
.r.model_wins .verdict{color:var(--green)}
.r.model_loses .verdict{color:var(--refuse)}
.r .src{margin-top:auto;padding-top:16px;font-size:10px;color:#3f4e5e;border-top:1px solid var(--line)}
.r .src b{color:var(--mid);font-weight:400}

.map{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--line);
  border:1px solid var(--line)}
.map div{background:var(--panel);padding:18px 20px;display:flex;align-items:baseline;gap:12px}
.map .a{font-size:13px;letter-spacing:.16em;color:var(--bright);font-weight:600}
.map .arrow{color:var(--line-hot)}
.map .b{font-size:13px;letter-spacing:.16em;color:var(--mid)}

.foot{text-align:center;font-size:11px;color:var(--dim);line-height:1.75}
.foot b{color:var(--mid);font-weight:400}
"""


def render_results(results: Results) -> str:
    """One self-contained page. Every figure comes from a committed report."""
    blocks = "".join(_block(r) for r in (results.r1, results.r2, results.r3))
    mapping = "".join(
        f"<div><span class='a'>{escape(a)}</span>"
        f"<span class='arrow'>&rarr;</span>"
        f"<span class='b'>{escape(b)}</span></div>"
        for a, b in MAPPING
    )
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Measured results</title>"
        "<link rel='icon' href=\"data:image/svg+xml,"
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
        "<rect width='32' height='32' fill='%2306080b'/>"
        "<rect x='5' y='14' width='5' height='11' fill='%233395ff'/>"
        "<rect x='13' y='19' width='5' height='6' fill='%23ff5f3d'/>"
        "<rect x='21' y='8' width='5' height='17' fill='%232fd47a'/></svg>\">"
        f"<style>{_STYLE}</style></head><body><div id='app'>"
        "<div class='head'><h1>MEASURED RESULTS</h1>"
        "<div class='s'>every figure read from the raw output committed in reports/</div></div>"
        f"<div class='grid'>{blocks}</div>"
        f"<div class='map'>{mapping}</div>"
        "<div class='foot'>The model <b>loses at timing and wins at understanding.</b> "
        "Not a contradiction &mdash; it is why the system routes rather than delegates.<br>"
        "Recovery outcomes are simulated; the policy engine, the model calls and the "
        "audit trail are not.</div>"
        "</div></body></html>"
    )


def _block(r: Result) -> str:
    state = r.verdict if r.available else "off"
    parts = [
        f"<div class='r {escape(state)}'>",
        f"<div class='k'>{escape(r.key)}</div>",
        f"<div class='q'>{escape(r.question)}</div>",
        f"<div class='v'>{escape(r.headline)}</div>",
        f"<div class='d'>{escape(r.detail)}</div>",
    ]
    if r.baseline and r.model:
        parts.append(
            f"<div class='rate'>keywords <b>{escape(r.baseline)}</b> &rarr; "
            f"model <b>{escape(r.model)}</b> on policy facts</div>"
        )
    elif r.n:
        parts.append(f"<div class='rate'>n = {escape(r.n)}</div>")

    if r.available:
        parts.append(f"<div class='verdict'>{_verdict_text(state)}</div>")
    parts.append(f"<div class='src'>read from <b>{escape(r.source)}</b></div></div>")
    return "".join(parts)


_VERDICT_TEXT = {
    "system": "the system wins",
    "model_wins": "the model wins",
    "model_loses": "the model loses",
}


def _verdict_text(state: str) -> str:
    return _VERDICT_TEXT.get(state, "")
