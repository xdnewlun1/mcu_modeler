"""`chipem serve`: run a Session behind a local web UI (see chipem/web/server.py for the threading model)."""
from __future__ import annotations

import sys


def add_args(p) -> None:
    p.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=8765, help="TCP port (default 8765; 0 = any free port)")
    p.add_argument("--open", action="store_true", help="open the UI in the default web browser")
    p.add_argument("--chunk", type=float, default=5.0, metavar="MS",
                   help="virtual milliseconds emulated per worker chunk (default 5)")
    p.add_argument("--start", action="store_true", help="start running immediately")
    p.add_argument("--verbose", action="store_true", help="log HTTP requests")
    p.add_argument("--view", default=None, metavar="CHIP",
                   help="with --board: chip selected in the UI at start (default: the first chip); every chip is shown")


def main(args) -> int:
    from .session import Session
    from .web.server import WebApp
    session = Session.from_args(args)
    if session.is_board:
        # the web UI draws every chip of the board; --view picks the chip selected at start (its pin table,
        # Discovery tab and readouts) and the chip that unqualified names refer to. The UI itself always uses
        # qualified names ("B.PTC6", "B.UART0").
        session.set_view(getattr(args, "view", None) or next(iter(session.chips)))
        print(f"board {session.board.name}: chips {', '.join(session.chips)}; initially selected: {session.view}",
              file=sys.stderr)
    app = WebApp(session, getattr(args, "host", "127.0.0.1"), getattr(args, "port", 8765),
                 realtime=session.pacer is not None, chunk_ms=getattr(args, "chunk", 5.0),
                 verbose=getattr(args, "verbose", False))
    d = session.desc
    print(f"chipem serve — {d.name}, firmware {getattr(session.image, 'source', '-')}\n"
          f"UI at {app.url}   (Ctrl-C to quit)", file=sys.stderr)
    from .hostserial import print_endpoints
    print_endpoints(session)
    if getattr(args, "start", False):
        app.worker.running = True
    if getattr(args, "open", False):
        import webbrowser
        webbrowser.open(app.url)
    try:
        app.serve_forever()
    finally:
        app.session.close()          # the board editor may have replaced the session it started with
    return 0
