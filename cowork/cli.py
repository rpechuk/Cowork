from __future__ import annotations

import argparse
import logging
import sys


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from cowork.server.app import app

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    from cowork.client.tui import run

    run()
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    from cowork import __version__

    print(__version__)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cowork", description="Cowork: collaborative multi-agent chat")
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="Run the Cowork server")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.set_defaults(func=cmd_serve)

    p_tui = sub.add_parser("tui", help="Launch the TUI client (default)")
    p_tui.set_defaults(func=cmd_tui)

    p_version = sub.add_parser("version", help="Print version and exit")
    p_version.set_defaults(func=cmd_version)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        # Default to TUI when no subcommand given.
        return cmd_tui(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
