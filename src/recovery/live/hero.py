"""The cold open: thirty seconds before the console.

The video is produced separately and may simply not be there when the demo
runs, so **the typographic form is the real design and the video is an
upgrade.** A hero that breaks without its asset is a hero that breaks on stage.

Drop a file at ``assets/hero.mp4`` (or ``.webm``) and it plays full-bleed with
the same title card over it. Either way the page leads into the console.
"""

from __future__ import annotations

from html import escape
from pathlib import Path

HERO_FILES: tuple[str, ...] = ("hero.mp4", "hero.webm")
"""Filenames the hero route will serve, in order of preference.

A fixed allowlist rather than a path parameter: this is the only place the
console reads a file off disk, and a route that accepts no filename cannot be
walked out of its directory."""

MEDIA_TYPES: dict[str, str] = {".mp4": "video/mp4", ".webm": "video/webm"}


def find_hero_media(directory: Path) -> str | None:
    """The hero video's filename, if one is present."""
    for name in HERO_FILES:
        candidate = directory / name
        if candidate.is_file():
            return name
    return None


_STYLE = """
:root{--ink:#eef4fb;--dim:#7c8ba0;--blue:#3395ff;--green:#2fd47a;--ground:#05070a;
      --mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{background:var(--ground);color:var(--ink);font-family:var(--mono);overflow:hidden}
video{position:fixed;inset:0;width:100%;height:100%;object-fit:cover;z-index:0}
.veil{position:fixed;inset:0;z-index:1;
      background:radial-gradient(ellipse 80% 60% at 50% 45%,rgba(5,7,10,.35),rgba(5,7,10,.92))}
.grid{position:fixed;inset:0;z-index:1;opacity:.5;
  background-image:linear-gradient(#101a26 1px,transparent 1px),linear-gradient(90deg,#101a26 1px,transparent 1px);
  background-size:64px 64px;
  -webkit-mask-image:radial-gradient(ellipse 70% 60% at 50% 50%,#000,transparent 78%);
  mask-image:radial-gradient(ellipse 70% 60% at 50% 50%,#000,transparent 78%)}
main{position:relative;z-index:2;height:100%;display:flex;flex-direction:column;
     align-items:center;justify-content:center;text-align:center;padding:40px;gap:26px}
.eyebrow{font-size:11px;letter-spacing:.42em;color:var(--dim);text-transform:uppercase}
h1{font-size:clamp(38px,8.5vw,104px);font-weight:700;letter-spacing:-.03em;line-height:.94;
   text-wrap:balance}
h1 em{font-style:normal;color:var(--blue)}
.claim{font-size:clamp(13px,1.7vw,17px);color:var(--dim);line-height:1.75;max-width:56ch;letter-spacing:.01em}
.claim b{color:var(--ink);font-weight:500}
.figures{display:flex;gap:0;flex-wrap:wrap;justify-content:center;border:1px solid #16202b}
.fig{padding:14px 26px;border-right:1px solid #16202b;min-width:150px}
.fig:last-child{border-right:0}
.fig .v{font-size:24px;font-weight:600;letter-spacing:-.01em}
.fig .v.up{color:var(--green)}
.fig .v.down{color:#ff5f3d}
.fig .k{font-size:9px;letter-spacing:.19em;color:var(--dim);text-transform:uppercase;margin-top:7px}
a.enter{display:inline-block;margin-top:6px;font-size:11px;letter-spacing:.24em;text-transform:uppercase;
  color:var(--ink);text-decoration:none;border:1px solid #22303f;padding:14px 30px;transition:.16s}
a.enter:hover,a.enter:focus-visible{border-color:var(--blue);background:#0d1721;outline:none}
.slot{font-size:10.5px;color:#48566a;letter-spacing:.09em;line-height:1.8}
@media (prefers-reduced-motion:no-preference){
  main>*{animation:rise .7s cubic-bezier(.16,.8,.3,1) both}
  main>*:nth-child(1){animation-delay:.05s}
  main>*:nth-child(2){animation-delay:.18s}
  main>*:nth-child(3){animation-delay:.34s}
  main>*:nth-child(4){animation-delay:.5s}
  main>*:nth-child(5){animation-delay:.66s}
  main>*:nth-child(6){animation-delay:.8s}
  @keyframes rise{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
}
"""


def render_hero(*, media: str | None) -> str:
    """The cold open. Self-contained; the video, if any, is same-origin."""
    video = (
        f'<video autoplay muted loop playsinline src="/hero/media" '
        f'aria-hidden="true" data-file="{escape(media)}"></video>'
        if media
        else ""
    )
    slot = (
        ""
        if media
        else (
            '<div class="slot">No cold open loaded &mdash; drop a clip at '
            "<b>assets/hero.mp4</b> and it plays behind this card.</div>"
        )
    )
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Recoup</title>"
        "<link rel='icon' href=\"data:image/svg+xml,"
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
        "<rect width='32' height='32' fill='%2305070a'/>"
        "<rect x='6' y='7' width='4' height='18' fill='%233395ff'/>"
        "<rect x='14' y='13' width='4' height='12' fill='%232fd47a'/>"
        "<rect x='22' y='10' width='4' height='15' fill='%23ff5f3d'/></svg>\">"
        f"<style>{_STYLE}</style></head><body>"
        f"{video}"
        '<div class="grid"></div><div class="veil"></div>'
        "<main>"
        '<div class="eyebrow">Razorpay AI Buildathon &middot; Track 03</div>'
        "<h1>RECOUP<em>/</em>CTRL</h1>"
        '<p class="claim">A failed recurring charge is not one problem. '
        "We did not build an AI we assume works &mdash; we built a system that can "
        "<b>prove when AI helps, and when it doesn&rsquo;t.</b></p>"
        '<div class="figures">'
        '<div class="fig"><div class="v up">+21.6</div><div class="k">system vs default</div></div>'
        '<div class="fig"><div class="v down">&minus;21.2</div><div class="k">model at timing</div></div>'
        '<div class="fig"><div class="v up">+12.0</div><div class="k">model at reading</div></div>'
        "</div>"
        '<a class="enter" href="/">Enter the control room</a>'
        f"{slot}"
        "</main></body></html>"
    )
