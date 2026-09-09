"""The execution proof, as a page.

Built to be unmistakable about what it is and is not. Every figure here is a
real Razorpay payment amount confirmed by the provider; none of it is the
recovery lift. The page says so twice, because the one way to spoil this claim
is to let a real payment id sit next to a simulated rupee total.
"""

from __future__ import annotations

from html import escape

from recovery.live.liveproof import ProofView

_STYLE = """
:root{--ground:#06080b;--panel:#0a0e13;--line:#151f2a;--line-hot:#1e2c3b;
  --dim:#546578;--mid:#8397a8;--text:#c7d4e0;--bright:#eaf2f9;
  --blue:#3395ff;--green:#2fd47a;--refuse:#ff5f3d;--amber:#ffb020;
  --mono:ui-monospace,"SF Mono","JetBrains Mono","Fira Code",Menlo,Consolas,monospace}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--ground);color:var(--text);font:13px/1.6 var(--mono);padding:34px 28px 70px}
body::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:0;opacity:.4;
  background-image:linear-gradient(var(--line) 1px,transparent 1px),linear-gradient(90deg,var(--line) 1px,transparent 1px);
  background-size:60px 60px;mask-image:radial-gradient(ellipse 110% 70% at 50% 0%,#000 20%,transparent 72%)}
.sheet{position:relative;z-index:1;max-width:960px;margin:0 auto}
h1{font-size:15px;letter-spacing:.28em;color:var(--bright);font-weight:700;text-align:center}
.sub{font-size:10.5px;letter-spacing:.18em;color:var(--dim);text-transform:uppercase;text-align:center;margin-top:8px}

.split{border:1px solid var(--line-hot);background:linear-gradient(180deg,#0b1119,#080c11);
  padding:15px 20px;margin:22px 0 24px;display:grid;grid-template-columns:1fr 1fr;gap:1px;
  background-color:var(--line)}
.split>div{background:#080c11;padding:13px 17px}
.split .k{font-size:9px;letter-spacing:.2em;color:var(--dim);text-transform:uppercase}
.split .q{font-size:12.5px;color:var(--mid);margin-top:8px;line-height:1.55}
.split .v{font-size:12.5px;margin-top:9px}
.split .this{color:var(--green)}
.split .that{color:var(--amber)}

.step{border-left:2px solid var(--line-hot);margin-left:12px;padding:18px 0 18px 24px;position:relative}
.step::before{content:"";position:absolute;left:-7px;top:22px;width:12px;height:12px;border-radius:50%;
  background:var(--ground);border:2px solid var(--line-hot)}
.step.fail::before{border-color:var(--refuse)}
.step.policy::before{border-color:var(--blue)}
.step.win::before{border-color:var(--green);background:var(--green)}
.step h2{font-size:11.5px;letter-spacing:.22em;font-weight:700;color:var(--mid)}
.step.fail h2{color:var(--refuse)}
.step.policy h2{color:var(--blue)}
.step.win h2{color:var(--green)}
.row{display:flex;gap:14px;padding:6px 0;align-items:baseline;flex-wrap:wrap}
.row .k{flex:0 0 130px;font-size:9.5px;letter-spacing:.13em;color:var(--dim);text-transform:uppercase}
.row .v{font-size:13px;color:var(--text);word-break:break-all}
.row .v b{color:var(--bright);font-weight:600}
.row .v.hot{color:var(--refuse)}
.row .v.good{color:var(--green);font-weight:600}
.row .v.blue{color:var(--blue)}
.big{font-size:34px;font-weight:600;color:var(--green);letter-spacing:-.02em;margin-top:6px}

.note{border-left:3px solid var(--amber);background:rgba(255,176,32,.05);padding:13px 16px;
  margin-top:24px;font-size:11.5px;color:var(--mid);line-height:1.75}
.note b{color:var(--bright);font-weight:600}
.off{border:1px solid var(--line-hot);padding:16px 18px;color:var(--dim);font-size:12px}
"""


def render_proof(view: ProofView) -> str:
    body = _body(view)
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Execution proof</title>"
        "<link rel='icon' href=\"data:image/svg+xml,"
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
        "<rect width='32' height='32' fill='%2306080b'/>"
        "<path d='M7 17l5 5 12-12' stroke='%232fd47a' stroke-width='4' fill='none'/></svg>\">"
        f"<style>{_STYLE}</style></head><body><div class='sheet'>"
        "<h1>EXECUTION PROOF</h1>"
        "<div class='sub'>one real payment &middot; razorpay test mode &middot; verified server-side</div>"
        + _split()
        + body
        + "</div></body></html>"
    )


def _split() -> str:
    """The distinction this page exists to protect."""
    return (
        "<div class='split'>"
        "<div><div class='k'>experimental proof</div>"
        "<div class='q'>Does the recovery strategy work?</div>"
        "<div class='v that'>R1 / R2 / R3 &middot; controlled &middot; simulated &middot; reproducible</div></div>"
        "<div><div class='k'>execution proof &mdash; this page</div>"
        "<div class='q'>Can the system operate against real payment infrastructure?</div>"
        "<div class='v this'>Razorpay test mode &middot; real payment state &middot; n = 1</div></div>"
        "</div>"
    )


def _row(key: str, value: str, cls: str = "") -> str:
    return f"<div class='row'><span class='k'>{escape(key)}</span><span class='v {cls}'>{value}</span></div>"


def _body(view: ProofView) -> str:
    if not view.available or view.proof is None:
        return f"<div class='off'>Not connected &mdash; {escape(view.reason or 'no reason given')}</div>"

    p = view.proof
    parts = []

    if p.failure:
        parts.append(
            "<div class='step fail'><h2>A REAL PAYMENT FAILED</h2>"
            + _row("payment id", f"<b>{escape(p.failure.payment_id)}</b>")
            + _row("status", escape(p.failure.status), "hot")
            + _row("method", escape(p.failure.method))
            + _row("provider said", escape(p.failure.error_reason or "&mdash;"), "hot")
            + "</div>"
        )
        parts.append(
            "<div class='step policy'><h2>OUR TAXONOMY HAD NEVER SEEN THAT CODE</h2>"
            + _row(
                "classified",
                escape((p.decline_class.value if p.decline_class else "?").upper()),
                "blue",
            )
            + _row(
                "retry permitted",
                "no" if not p.retry_permitted else "yes",
                "hot" if not p.retry_permitted else "",
            )
            + _row("policy engine", escape(p.policy_refusal or "&mdash;"), "hot")
            + _row("remediation", escape(p.policy_remediation or "&mdash;"), "good")
            + "</div>"
        )

    if p.recovery:
        parts.append(
            "<div class='step win'><h2>A REAL PAYMENT SUCCEEDED ON THE SAME ORDER</h2>"
            + _row("payment id", f"<b>{escape(p.recovery.payment_id)}</b>")
            + _row("status", escape(p.recovery.status), "good")
            + _row("method", escape(p.recovery.method))
            + _row("order", escape(p.order_id or "&mdash;"))
            + _row("link", f"{escape(p.link_status)} &middot; {escape(p.link_url)}")
            + "<div class='row'><span class='k'>recovered</span>"
            f"<span class='v'><span class='big'>{escape(p.recovered_amount)}</span></span></div>"
            + _row(
                "attribution",
                "same order id as the failure &mdash; the loop closed"
                if p.attributed
                else "no shared order; not attributable",
                "good" if p.attributed else "hot",
            )
            + "</div>"
        )
    else:
        parts.append(
            "<div class='step'><h2>NO RECOVERY YET</h2>"
            + _row("link status", escape(p.link_status))
            + _row("amount paid", escape(p.recovered_amount))
            + "</div>"
        )

    parts.append(
        "<div class='note'>"
        "Every figure on this page is a payment amount <b>Razorpay confirms</b>, read back "
        "from the API. It is <b>n = 1</b>, and it is not the recovery lift: R1's figure comes "
        "from a controlled simulated experiment and is reported separately, because a real "
        "payment id sitting next to a modelled rupee total would turn one into a claim about "
        "the other.<br><br>"
        "The failure was an accident &mdash; an international test card against a domestic-only "
        "account. That is why it is worth showing: <b>the code was not in our registry</b>, and "
        "the system classified it UNKNOWN and refused to retry rather than guessing. Retrying "
        "an international card against this account would fail every time."
        "</div>"
    )
    return "".join(parts)
