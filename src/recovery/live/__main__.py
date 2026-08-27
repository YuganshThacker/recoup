"""Start the control room.

    python -m recovery.live                 # http://127.0.0.1:8765
    python -m recovery.live --open          # and open a browser
    python -m recovery.live --cases 40

Loopback only unless ``--host`` says otherwise, and the warning it prints when
you do override it is not decorative: the red-team panel drives real policy
code, so a console reachable from the venue network is that panel handed to the
room.
"""

from __future__ import annotations

import argparse
import threading
import webbrowser

from recovery.env import load_dotenv
from recovery.live.app import DEFAULT_DEMO_CASES, ControlRoom, build_router
from recovery.live.hero import find_hero_media
from recovery.live.server import ConsoleServer


def main() -> None:
    parser = argparse.ArgumentParser(prog="recovery.live")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1", help="Override at your own risk.")
    parser.add_argument("--cases", type=int, default=DEFAULT_DEMO_CASES)
    parser.add_argument("--open", action="store_true", help="Open a browser on start.")
    parser.add_argument("--run", action="store_true", help="Start a batch immediately.")
    args = parser.parse_args()

    # Before the room is built: the downtime panel reads credentials at
    # construction, and a console that silently ran without them would show
    # "not connected" for a feed that was configured all along.
    load_dotenv()

    room = ControlRoom(cases=args.cases)
    server = ConsoleServer(build_router(room), host=args.host, port=args.port)
    server.start()

    print(f"  Recoup control room  {server.url}")
    print(f"  cold open            {server.url}/hero")
    print(f"  batch: {args.cases} cases, tail-enriched")

    # Stated at startup rather than discovered on stage.
    feed = room.downtime.view()
    if feed.available:
        gating = sum(feed.summary.values())
        print(f"  downtime: live, {len(feed.outages)} published, {gating} gating")
    else:
        print(f"  downtime: not connected — {feed.reason}")

    hero = find_hero_media(room.assets)
    print(f"  cold open: {hero}" if hero else "  cold open: no clip (drop one at assets/hero.mp4)")
    if args.host != "127.0.0.1":
        print(f"  WARNING: bound to {args.host}, not loopback. The console can start runs.")
    print("  ctrl-c to stop\n")

    if args.open:
        # After the server is listening, so the first request cannot race the bind.
        threading.Timer(0.4, webbrowser.open, args=(server.url,)).start()
    if args.run:
        room.start_run()

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


if __name__ == "__main__":
    main()
