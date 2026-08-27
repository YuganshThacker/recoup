"""The compliance x-ray as a printable document.

Deliberately unlike the control room. The console is an instrument -- dark,
dense, live. This is a record: light, typeset, and printable to PDF without
anything else installed. Someone in compliance should be able to open it, read
it, and file it.

It prints its own caveats. A report that states only what it proves, and says
plainly what it does not, is worth more to the person filing it than one that
looks complete.
"""

from __future__ import annotations

from html import escape

from recovery.live.xray import Check, Xray

_VERDICT_TEXT = {
    "clean": "NO EXCEPTIONS",
    "exceptions": "EXCEPTIONS FOUND",
    "empty": "NO RECORD",
}

_STYLE = """
:root{--ink:#15181c;--soft:#5c6570;--rule:#d8dde3;--paper:#fbfcfd;
      --ok:#0f7a45;--bad:#b8321a;--accent:#0b4a8f;
      --serif:ui-serif,Georgia,"Times New Roman",serif;
      --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--paper);color:var(--ink);font:15px/1.6 var(--serif);
     padding:48px 24px 80px;-webkit-font-smoothing:antialiased}
.sheet{max-width:820px;margin:0 auto}
header{border-bottom:2px solid var(--ink);padding-bottom:16px;margin-bottom:26px;
       display:flex;align-items:flex-end;gap:20px;flex-wrap:wrap}
h1{font-size:23px;font-weight:600;letter-spacing:-.01em}
.sub{font:11px/1.5 var(--mono);color:var(--soft);letter-spacing:.06em;margin-top:6px;text-transform:uppercase}
.stamp{margin-left:auto;font:12px/1 var(--mono);letter-spacing:.16em;padding:9px 14px;border:2px solid;white-space:nowrap}
.stamp.clean{color:var(--ok);border-color:var(--ok)}
.stamp.exceptions{color:var(--bad);border-color:var(--bad)}
.stamp.empty{color:var(--soft);border-color:var(--soft)}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:14px;margin-bottom:30px}
.facts div{border-left:2px solid var(--rule);padding-left:11px}
.facts .k{font:9.5px/1 var(--mono);letter-spacing:.14em;color:var(--soft);text-transform:uppercase}
.facts .v{font-size:17px;margin-top:5px}
h2{font:11px/1 var(--mono);letter-spacing:.2em;text-transform:uppercase;color:var(--soft);
   margin:32px 0 12px;padding-bottom:7px;border-bottom:1px solid var(--rule)}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{text-align:left;font:9.5px/1 var(--mono);letter-spacing:.12em;text-transform:uppercase;
   color:var(--soft);padding:0 10px 7px 0;font-weight:400}
td{padding:8px 10px 8px 0;border-top:1px solid var(--rule);vertical-align:top}
td.m,th.m{font-family:var(--mono);font-size:12px}
.check{border-top:1px solid var(--rule);padding:14px 0}
.check .q{font-size:14.5px;display:flex;gap:10px;align-items:baseline}
.check .code{font:10px/1 var(--mono);letter-spacing:.1em;color:var(--soft);border:1px solid var(--rule);padding:4px 6px}
.check .mark{margin-left:auto;font:10px/1 var(--mono);letter-spacing:.14em}
.check.pass .mark{color:var(--ok)}
.check.fail .mark{color:var(--bad)}
.check .d{color:var(--soft);font-size:13px;margin-top:7px;padding-left:34px}
.check .e{font:11.5px/1.75 var(--mono);color:var(--ink);margin-top:8px;padding-left:34px}
.check.fail .e{color:var(--bad)}
.gates{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:8px}
/* Cells carry their own border. A painted container gap leaves a filled
   grey block where the last row runs short, which reads as a missing gate. */
.gates div{border:1px solid var(--rule);padding:11px 12px}
.gates .n{font:9.5px/1 var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--soft)}
.gates .t{font-size:15px;margin-top:6px;font-family:var(--mono)}
.gates .t b{color:var(--bad);font-weight:600}
.caveat{border-left:3px solid var(--accent);background:#f2f6fb;padding:13px 16px;margin-top:12px;font-size:13px;color:#24425f}
footer{margin-top:38px;padding-top:14px;border-top:1px solid var(--rule);
       font:10.5px/1.7 var(--mono);color:var(--soft)}
@media print{body{padding:0;background:#fff}.sheet{max-width:none}.caveat{background:none}}
"""


def render_xray(xray: Xray) -> str:
    """One self-contained page. No fonts fetched, nothing to install."""
    verdict = _VERDICT_TEXT.get(xray.verdict, xray.verdict.upper())
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>Compliance x-ray — {escape(xray.case_id)}</title>",
        f"<style>{_STYLE}</style></head><body><div class='sheet'>",
        "<header><div><h1>Compliance x-ray</h1>",
        f"<div class='sub'>case {escape(xray.case_id)} &middot; "
        "recoup &middot; subscription e-mandate recovery</div></div>",
        f"<div class='stamp {escape(xray.verdict)}'>{escape(verdict)}</div></header>",
    ]

    if xray.verdict == "empty":
        parts.append(
            "<p>No events are recorded against this case, so there is nothing to "
            "attest to. This is reported as an absence of record rather than as "
            "compliance.</p>"
        )
        parts.append("</div></body></html>")
        return "".join(parts)

    parts.append("<div class='facts'>")
    for key, value in (
        ("amount at risk", xray.amount or "not recorded"),
        ("experiment arm", xray.arm or "n/a"),
        ("ledger events", str(xray.events)),
        ("customer contacts", str(len(xray.contacts))),
        ("executions", str(len(xray.money_actions))),
        ("refusals", str(len(xray.refusals))),
    ):
        parts.append(
            f"<div><div class='k'>{escape(key)}</div><div class='v'>{escape(value)}</div></div>"
        )
    parts.append("</div>")

    parts.append("<h2>Checks</h2>")
    for check in xray.checks:
        parts.append(_check(check))

    parts.append("<h2>Gate evaluations</h2><div class='gates'>")
    for name, (passed, refused) in xray.gate_tally.items():
        tally = f"{passed} / {refused}" if not refused else f"{passed} / <b>{refused}</b>"
        parts.append(
            f"<div><div class='n'>{escape(name.replace('_', ' '))}</div>"
            f"<div class='t'>{tally}</div></div>"
        )
    parts.append("</div>")
    parts.append(
        "<p class='caveat'>Passing evaluations are recorded, not only refusals. "
        "Every gate runs on every decision with no short-circuit, which is what "
        "allows this table to show that the whole envelope was applied rather "
        "than only the rule that happened to object first.</p>"
    )

    if xray.contacts:
        parts.append("<h2>Customer contacts</h2><table><tr>")
        parts.append(
            "<th>Seq</th><th>Channel</th><th class='m'>Template</th>"
            "<th>Registered</th><th>Cost</th></tr>"
        )
        for contact in xray.contacts:
            mark = "yes" if contact.registered else "NO"
            parts.append(
                f"<tr><td class='m'>{contact.seq}</td><td>{escape(contact.channel)}</td>"
                f"<td class='m'>{escape(contact.template_id or '—')}</td>"
                f"<td>{mark}</td><td class='m'>{contact.cost_paise}p</td></tr>"
            )
        parts.append("</table>")

    if xray.money_actions:
        parts.append("<h2>Executions against the instrument</h2><table><tr>")
        parts.append("<th>Seq</th><th>Outcome</th><th>Authorised by</th></tr>")
        for action in xray.money_actions:
            authority = (
                f"decision at seq {action.authority_seq}" if action.authorised else "NO PERMIT"
            )
            parts.append(
                f"<tr><td class='m'>{action.seq}</td><td>{escape(action.summary)}</td>"
                f"<td class='m'>{escape(authority)}</td></tr>"
            )
        parts.append("</table>")

    if xray.refusals:
        parts.append("<h2>Refusals</h2><table>")
        for summary in xray.refusals:
            parts.append(f"<tr><td class='m'>{escape(summary)}</td></tr>")
        parts.append("</table>")

    parts.append("<h2>What this report does not claim</h2>")
    for caveat in xray.caveats:
        parts.append(f"<p class='caveat'>{escape(caveat)}</p>")

    parts.append(
        "<footer>Generated from the append-only audit ledger. Every line above "
        "is derived from recorded events; nothing is asserted that the ledger "
        "does not carry.</footer></div></body></html>"
    )
    return "".join(parts)


def _check(check: Check) -> str:
    state = "pass" if check.passed else "fail"
    mark = "NO EXCEPTION" if check.passed else "EXCEPTION"
    parts = [
        f"<div class='check {state}'><div class='q'>",
        f"<span class='code'>{escape(check.code)}</span>",
        f"<span>{escape(check.question)}</span>",
        f"<span class='mark'>{mark}</span></div>",
        f"<div class='d'>{escape(check.detail)}</div>",
    ]
    if check.evidence:
        lines = "<br>".join(escape(line) for line in check.evidence)
        parts.append(f"<div class='e'>{lines}</div>")
    parts.append("</div>")
    return "".join(parts)
