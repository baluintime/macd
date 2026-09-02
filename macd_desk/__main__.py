"""Run the desk: `python -m macd_desk --port 9000`."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .app import create_app
from .state import DEFAULT_STATE_PATH


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m macd_desk",
        description="Serve the Upstox MACD options desk on a port of your choosing.",
    )
    parser.add_argument(
        "-p", "--port", type=int, default=int(os.environ.get("MACD_DESK_PORT", os.environ.get("PORT", 8000))),
        help="port to listen on (default: 8000, or $MACD_DESK_PORT / $PORT)",
    )
    parser.add_argument(
        "-H", "--host", default=os.environ.get("MACD_DESK_HOST", "127.0.0.1"),
        help="interface to bind (default: 127.0.0.1; use 0.0.0.0 to expose on the LAN)",
    )
    parser.add_argument(
        "-s", "--state", type=Path, default=Path(os.environ.get("MACD_DESK_STATE", DEFAULT_STATE_PATH)),
        help=f"desk state file (default: {DEFAULT_STATE_PATH})",
    )
    parser.add_argument("--debug", action="store_true", help="reload on code changes")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    app = create_app(args.state)
    shown_host = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    print(f"MACD options desk → http://{shown_host}:{args.port}  (state: {args.state})")
    app.run(host=args.host, port=args.port, debug=args.debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
