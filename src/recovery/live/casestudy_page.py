"""The worked example, as a page.

Track 3's jury asks to be walked through one full case from the failure that
started it to the money that came back. This is built for exactly that: five
stages in the rubric's own vocabulary, in order, ending on a figure.

Server-rendered and still. Nothing animates, nothing needs a click, and it can
be read at a glance or paused on for a minute.
"""

from __future__ import annotations

from html import escape

from recovery.live.casestudy import CaseStudy

_STYLE = """
:root{--ground:#06080b;--panel:#0a0e13;--line:#151f2a;--line-hot:#1e2c3b;
  --dim:#546578;--mid:#8397a8;--text:#c7d4e0;--bright:#eaf2f9;
  --blue:#3395ff;--green:#2fd47a;--refuse:#ff5f3d;--amber:#ffb020;
  --mono:ui-monospace,"SF Mono","JetBrains Mono","Fira Code",Menlo,Consolas,monospace}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--ground);color:var(--text);font:13px/1.55 var(--mono);padding:34px 30px 70px}
body::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:0;opacity:.4;
  background-image:linear-gradient(var(--line) 1px,transparent 1px),linear-gradient(90deg,var(--line) 1px,transparent 1px);
  background-size:60px 60px;mask-image:radial-gradient(ellipse 110% 70% at 50% 0%,#000 20%,transparent 72%)}
.sheet{position:relative;z-index:1;max-width:1080px;margin:0 auto}
h1{font-size:15px;letter-spacing:.3em;color:var(--bright);font-weight:700;text-align:center}
.sub{font-size:10.5px;letter-spacing:.19em;color:var(--dim);text-transform:uppercase;text-align:center;margin-top:8px}

.head{border:1px solid var(--line-hot);background:linear-gradient(180deg,#0b1119,#080c11);
  padding:15px 20px;margin:22px 0 6px;display:flex;gap:26px;flex-wrap:wrap;align-items:center;justify-content:center}
.head .k{font-size:9px;letter-spacing:.2em;color:var(--dim);text-transform:uppercase;margin-right:8px}
.head .v{font-size:15px;color:var(--bright)}
.head .v b{color:var(--amber);font-weight:600}

.stage{border-left:2px solid var(--line-hot);margin:0 0 0 14px;padding:22px 0 22px 26px;position:relative}
.stage::before{content:"";position:absolute;left:-7px;top:26px;width:12px;height:12px;
  background:var(--ground);border:2px solid var(--line-hot);border-radius:50%}
.stage.detect::before{border-color:var(--mid)}
.stage.diagnose::before{border-color:var(--amber)}
.stage.intervene::before{border-color:var(--blue)}
.stage.recover::before{border-color:var(--green)}
.stage.measure::before{border-color:var(--green);background:var(--green)}
.stage h2{font-size:12.5px;letter-spacing:.24em;color:var(--bright);font-weight:700;display:flex;align-items:baseline;gap:12px}
.stage h2 .n{font-size:10px;color:var(--dim);letter-spacing:.1em}
.stage.detect h2{color:var(--mid)}
.stage.diagnose h2{color:var(--amber)}
.stage.intervene h2{color:var(--blue)}
.stage.recover h2,.stage.measure h2{color:var(--green)}
.stage .blurb{font-size:11.5px;color:var(--dim);margin-top:7px;line-height:1.6;max-width:78ch}

.step{display:flex;gap:14px;align-items:baseline;padding:8px 0;border-top:1px solid var(--line);margin-top:0}
.step:first-of-type{margin-top:13px}
.step .d{flex:0 0 54px;font-size:10px;letter-spacing:.08em;color:#3f4e5e;text-transform:uppercase}
.step .s{flex:1;font-size:12.5px;word-break:break-word}
.step .g{flex:0 0 auto;font-size:9.5px;color:var(--dim);letter-spacing:.05em}
.step .g b{color:var(--green);font-weight:400}
.step .g i{color:var(--refuse);font-style:normal}
.step.good .s{color:var(--green);font-weight:600}
.step.bad .s{color:var(--refuse)}

.diag{border:1px solid #3a2a10;background:rgba(255,176,32,.05);padding:14px 17px;margin-top:13px}
.diag .row{display:flex;gap:14px;align-items:baseline;padding:4px 0}
.diag .k{flex:0 0 118px;font-size:9.5px;letter-spacing:.14em;color:var(--dim);text-transform:uppercase}
.diag .v{font-size:13px;color:var(--text)}
.diag .v b{color:var(--amber)}

.result{border:1px solid #14432b;background:rgba(47,212,122,.06);padding:20px 24px;margin-top:14px;
  display:flex;align-items:center;gap:26px;flex-wrap:wrap}
.result .big{font-size:38px;font-weight:600;color:var(--green);letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.result .k{font-size:9.5px;letter-spacing:.2em;color:var(--dim);text-transform:uppercase}
.result .v{font-size:14px;color:var(--text);margin-top:5px}
.result .cell{display:flex;flex-direction:column}
.foot{margin-top:26px;font-size:10.5px;color:var(--dim);line-height:1.75;text-align:center}
.foot b{color:var(--mid);font-weight:400}
"""


def render_case_study(study: CaseStudy | None) -> str:
    """One self-contained page. Every value comes from a real run."""
    if study is None:
        body = (
            "<div class='diag'><div class='v'>No case in this batch recovered attributably, "
            "so there is no worked example to show. A walk through a case that did not work "
            "is not the thing that was asked for.</div></div>"
        )
    else:
        body = _head(study) + _detect(study) + _diagnose(study) + _rest(study) + _result(study)

    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Worked example</title>"
        "<link rel='icon' href=\"data:image/svg+xml,"
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
        "<rect width='32' height='32' fill='%2306080b'/>"
        "<circle cx='16' cy='8' r='3' fill='%238397a8'/>"
        "<circle cx='16' cy='16' r='3' fill='%233395ff'/>"
        "<circle cx='16' cy='24' r='3' fill='%232fd47a'/></svg>\">"
        f"<style>{_STYLE}</style></head><body><div class='sheet'>"
        "<h1>ONE CASE, ALL THE WAY THROUGH</h1>"
        "<div class='sub'>detect &middot; diagnose &middot; intervene &middot; recover &middot; measure</div>"
        f"{body}</div></body></html>"
    )


def _head(s: CaseStudy) -> str:
    return (
        "<div class='head'>"
        f"<span><span class='k'>case</span><span class='v'>{escape(s.case_id)}</span></span>"
        f"<span><span class='k'>at risk</span><span class='v'><b>{escape(s.amount)}</b></span></span>"
        f"<span><span class='k'>gates run</span><span class='v'>{s.gates_run}</span></span>"
        f"<span><span class='k'>window</span><span class='v'>{s.days} days</span></span>"
        "</div>"
    )


def _stage(name: str, index: int, blurb: str, inner: str) -> str:
    return (
        f"<div class='stage {name.lower()}'>"
        f"<h2><span class='n'>{index:02d}</span>{escape(name)}</h2>"
        f"<div class='blurb'>{escape(blurb)}</div>{inner}</div>"
    )


def _steps(stage: object) -> str:
    rows = []
    for t in stage.steps:  # type: ignore[attr-defined]
        # Classify on the event kind, not on the word "failed". The detected
        # charge failure and the event name payment.failed both contain it, and
        # neither is a failure of ours -- they are the premise.
        cls = ""
        if t.kind in ("outcome_recorded", "action_executed") and (
            "recovered" in t.summary or "succeeded" in t.summary
        ):
            cls = " good"
        elif t.kind == "action_refused" or t.summary.startswith("debit failed"):
            cls = " bad"
        gates = ""
        if t.gates_run:
            passed = t.gates_run - t.gates_refused
            gates = (
                f"<span class='g'><b>{passed}</b> passed"
                + (f" &middot; <i>{t.gates_refused} refused</i>" if t.gates_refused else "")
                + "</span>"
            )
        rows.append(
            f"<div class='step{cls}'><span class='d'>day {t.day}</span>"
            f"<span class='s'>{escape(t.summary)}</span>{gates}</div>"
        )
    return "".join(rows)


def _detect(s: CaseStudy) -> str:
    stage = s.stages[0]
    return _stage(stage.name, 1, stage.blurb, _steps(stage))


def _diagnose(s: CaseStudy) -> str:
    d = s.diagnosis
    inner = (
        "<div class='diag'>"
        f"<div class='row'><span class='k'>provider said</span>"
        f"<span class='v'>{escape(d.reason)}</span></div>"
        f"<div class='row'><span class='k'>class</span>"
        f"<span class='v'><b>{escape(d.decline_class.upper())}</b></span></div>"
        f"<div class='row'><span class='k'>therefore</span>"
        f"<span class='v'>{escape(d.permits)}</span></div>"
        "</div>"
    )
    return _stage(
        "DIAGNOSE",
        2,
        "The class decides what is even permitted. This is a conclusion, not an "
        "event, so it is derived rather than logged.",
        inner,
    )


def _rest(s: CaseStudy) -> str:
    return "".join(
        _stage(stage.name, index, stage.blurb, _steps(stage))
        for index, stage in enumerate(s.stages[1:], start=3)
    )


def _result(s: CaseStudy) -> str:
    return (
        "<div class='result'>"
        f"<div class='cell'><div class='k'>recovered</div><div class='big'>{escape(s.recovered_amount)}</div></div>"
        f"<div class='cell'><div class='k'>attribution</div><div class='v'>"
        "attributed, not organic</div></div>"
        f"<div class='cell'><div class='k'>cost</div><div class='v'>{s.attempts} debit attempt"
        f"{'' if s.attempts == 1 else 's'} &middot; {s.messages} message"
        f"{'' if s.messages == 1 else 's'}</div></div>"
        f"<div class='cell'><div class='k'>gates</div><div class='v'>{s.gates_run} run"
        f"{f' &middot; {s.gates_refused} refused' if s.gates_refused else ''}</div></div>"
        "</div>"
        "<div class='foot'>The loop closed on a payment, not a chart. "
        "Recovery is <b>attributed</b> only when the invoice was paid inside the observation "
        "window; a customer who paid unprompted is counted as organic and excluded from lift.<br>"
        "Simulated outcome. The delivery verification, the decline taxonomy, the gates and the "
        "audit trail are not.</div>"
    )
