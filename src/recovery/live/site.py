"""Build the static evidence site.

What someone reviewing the submission clicks through when nobody is there to
narrate: the audit report, and a set of per-case compliance attestations. No
server, no control surface, nothing to trigger -- which is the point. The live
console drives real policy code and belongs on a loopback interface; this is
the half that is safe to leave on the internet.

It is generated from a real run rather than assembled by hand, so it rebuilds
after any change and cannot drift from the code it describes.

    python -m recovery.live.site --out site/
"""

from __future__ import annotations

import argparse
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path

from recovery.domain.events import AuditEvent, EventKind
from recovery.live.app import ControlRoom
from recovery.live.xray import Xray, build_xray
from recovery.live.xray_page import render_xray

REPO_URL = "https://github.com/YuganshThacker/recoup"
DEFAULT_AUDIT_REPORT = Path("reports/audit.html")
DEFAULT_MAX_XRAYS = 6


@dataclass(frozen=True, slots=True)
class XrayEntry:
    case_id: str
    path: str
    verdict: str
    amount: str | None
    refusals: int
    contacts: int
    events: int


@dataclass(frozen=True, slots=True)
class SiteManifest:
    xrays: tuple[XrayEntry, ...]
    audit: str | None
    built_at: str


def pick_cases(histories: dict[str, list[AuditEvent]], *, limit: int) -> list[str]:
    """The cases worth attesting to.

    Cases the report found something wrong with come first, then cases the
    policy engine had most to say about. A site that surfaced its findings only
    by luck of the sort order would be burying the one thing it exists to show;
    an attestation on a case where nothing happened proves nothing either way.
    """

    def interest(case_id: str) -> tuple[int, int, int]:
        events = histories[case_id]
        report = build_xray(case_id, events)
        refusals = sum(1 for e in events if e.kind is EventKind.ACTION_REFUSED)
        return len(report.exceptions), refusals, len(events)

    return sorted(histories, key=interest, reverse=True)[:limit]


def build_site(
    out: Path,
    *,
    cases: int = 60,
    max_xrays: int = DEFAULT_MAX_XRAYS,
    audit_report: Path | None = None,
) -> SiteManifest:
    """Run a batch and write the site. Returns what was written."""
    out.mkdir(parents=True, exist_ok=True)

    room = ControlRoom(cases=cases)
    room.start_run()
    room.wait(timeout=600)
    histories = {c: room.store.read_case(c) for c in room.store.all_cases()}

    xray_dir = out / "xray"
    xray_dir.mkdir(exist_ok=True)
    entries = []
    for case_id in pick_cases(histories, limit=max_xrays):
        report = build_xray(case_id, histories[case_id])
        name = f"xray/{case_id}.html"
        (out / name).write_text(render_xray(report), encoding="utf-8")
        entries.append(_entry(case_id, name, report))

    audit = _copy_audit(out, audit_report)

    manifest = SiteManifest(
        xrays=tuple(entries),
        audit=audit,
        built_at=datetime.now(UTC).strftime("%d %B %Y"),
    )
    (out / "index.html").write_text(_index(manifest), encoding="utf-8")

    # GitHub Pages runs output through Jekyll unless told not to, which drops
    # anything it decides looks like a template.
    (out / ".nojekyll").write_text("")
    return manifest


def _entry(case_id: str, name: str, report: Xray) -> XrayEntry:
    return XrayEntry(
        case_id=case_id,
        path=name,
        verdict=report.verdict,
        amount=report.amount,
        refusals=len(report.refusals),
        contacts=len(report.contacts),
        events=report.events,
    )


def _copy_audit(out: Path, source: Path | None) -> str | None:
    """Carry the audit report across, if one has been generated.

    Absence is normal: the report comes from a separate batch run and may
    simply not have been made yet.
    """
    path = source if source is not None else DEFAULT_AUDIT_REPORT
    if not path.is_file():
        return None
    shutil.copyfile(path, out / "audit.html")
    return "audit.html"


_STYLE = """
:root{--ink:#15181c;--soft:#5c6570;--rule:#d8dde3;--paper:#fbfcfd;
      --ok:#0f7a45;--bad:#b8321a;--accent:#0b4a8f;
      --serif:ui-serif,Georgia,"Times New Roman",serif;
      --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--paper);color:var(--ink);font:15px/1.65 var(--serif);padding:52px 24px 80px}
.sheet{max-width:820px;margin:0 auto}
header{border-bottom:2px solid var(--ink);padding-bottom:18px;margin-bottom:30px}
h1{font-size:25px;font-weight:600;letter-spacing:-.01em}
.sub{font:11px/1.6 var(--mono);color:var(--soft);letter-spacing:.06em;margin-top:7px;text-transform:uppercase}
.lede{margin:22px 0 0;color:var(--soft);max-width:62ch}
h2{font:11px/1 var(--mono);letter-spacing:.2em;text-transform:uppercase;color:var(--soft);
   margin:36px 0 14px;padding-bottom:7px;border-bottom:1px solid var(--rule)}
.card{display:block;border:1px solid var(--rule);padding:15px 17px;margin-bottom:9px;
      text-decoration:none;color:inherit;transition:border-color .14s}
.card:hover,.card:focus-visible{border-color:var(--accent);outline:none}
.card .t{font-size:16px;display:flex;align-items:baseline;gap:11px;flex-wrap:wrap}
.card .t .id{font-family:var(--mono);font-size:13px}
.card .t .mark{margin-left:auto;font:9.5px/1 var(--mono);letter-spacing:.13em;
               padding:4px 7px;border:1px solid}
.mark.clean{color:var(--ok);border-color:var(--ok)}
.mark.exceptions{color:var(--bad);border-color:var(--bad)}
.card .d{font:11.5px/1.7 var(--mono);color:var(--soft);margin-top:7px}
.note{border-left:3px solid var(--accent);background:#f2f6fb;padding:13px 16px;margin:14px 0;
      font-size:13.5px;color:#24425f}
footer{margin-top:42px;padding-top:15px;border-top:1px solid var(--rule);
       font:10.5px/1.8 var(--mono);color:var(--soft)}
a{color:var(--accent)}
"""


def _index(manifest: SiteManifest) -> str:
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>Recoup &mdash; evidence</title>",
        f"<style>{_STYLE}</style></head><body><div class='sheet'>",
        "<header><h1>Recoup &mdash; evidence</h1>",
        "<div class='sub'>bounded revenue recovery &middot; razorpay ai buildathon &middot; track 03</div>",
        "</header>",
        "<p class='lede'>Recoup detects a failed recurring charge, decides what is worth doing, "
        "and <b>cannot take a money action its policy engine has not permitted.</b> "
        "These are the records it produces. Nothing here is a control surface: there is "
        "nothing to trigger and no state to change.</p>",
    ]

    if manifest.audit:
        parts += [
            "<h2>The audit report</h2>",
            f"<a class='card' href='{manifest.audit}'>",
            "<div class='t'>Full run, every case</div>",
            "<div class='d'>Case timelines, model proposals, refusal traces and the measured "
            "results, in one self-contained file.</div></a>",
        ]

    parts.append("<h2>Compliance attestations</h2>")
    parts.append(
        "<p class='note'>One per case, built from the append-only ledger. Five checks, each "
        "answering a question the ledger settles &mdash; and each able to come back negative. "
        "A report that can only say &ldquo;pass&rdquo; attests to nothing.</p>"
    )
    for entry in manifest.xrays:
        verdict = "NO EXCEPTIONS" if entry.verdict == "clean" else "EXCEPTIONS"
        parts += [
            f"<a class='card' href='{escape(entry.path)}'>",
            f"<div class='t'><span class='id'>{escape(entry.case_id)}</span>",
            f"<span class='mark {escape(entry.verdict)}'>{verdict}</span></div>",
            f"<div class='d'>{escape(entry.amount or 'amount not recorded')} &middot; "
            f"{entry.events} ledger events &middot; {entry.contacts} contacts &middot; "
            f"{entry.refusals} refusals</div></a>",
        ]

    parts += [
        "<h2>Source</h2>",
        f"<a class='card' href='{REPO_URL}'><div class='t'>The repository</div>",
        "<div class='d'>Architecture, the pre-registered analysis plan, and the results "
        "including the ones that go against the model.</div></a>",
        f"<footer>Generated {escape(manifest.built_at)} from a run of the committed code. "
        "The live console is not deployed: it drives real policy code and binds loopback "
        "by design.</footer>",
        "</div></body></html>",
    ]
    return "".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(prog="recovery.live.site")
    parser.add_argument("--out", type=Path, default=Path("site"))
    parser.add_argument("--cases", type=int, default=60)
    parser.add_argument("--xrays", type=int, default=DEFAULT_MAX_XRAYS)
    args = parser.parse_args()

    manifest = build_site(args.out, cases=args.cases, max_xrays=args.xrays)
    print(f"  site: {args.out}")
    print(f"  audit report: {manifest.audit or 'not found, skipped'}")
    for entry in manifest.xrays:
        print(f"    {entry.path}  {entry.verdict}  {entry.refusals} refusals")


if __name__ == "__main__":
    main()
