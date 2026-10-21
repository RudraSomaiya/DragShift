"""Command-line entry point: `dragshift` and `python -m dragshift`.

dragshift                     run in the foreground with console logs (FR-6.1)
dragshift --background        relaunch detached with pythonw.exe, no console (FR-6.2)
dragshift ops <paths...>      list the Ops valid for these files (FR-6.4)
dragshift convert <paths...> --to <fmt>
dragshift tool <op-id> <paths...> [--preset discord]
"""

import argparse
import subprocess
import sys

from dragshift import __version__

# CreateProcess flags: no console, and not killed by Ctrl+C in the console that started it
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dragshift",
        description="Drag a file, tap a key, and a wheel converts it or runs a tool on it.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--background", action="store_true", help="run without a console window (FR-6.2)"
    )
    sub = parser.add_subparsers(dest="command", metavar="command")
    p = sub.add_parser("ops", help="list the operations valid for these files")
    p.add_argument("paths", nargs="+")
    p = sub.add_parser("convert", help="convert files, e.g. convert a.png --to jpg")
    p.add_argument("paths", nargs="+")
    p.add_argument("--to", required=True, metavar="FMT", help="a Convert petal, e.g. jpg, mp3")
    p = sub.add_parser("tool", help="run an operation by id, e.g. tool tools.image.compress a.png")
    p.add_argument("op_id")
    p.add_argument("paths", nargs="+")
    p.add_argument("--preset", help="a size preset, e.g. discord (FR-4.3)")
    return parser


def relaunch_in_background() -> int:
    """FR-6.2: start ``pythonw -m dragshift`` detached, and return at once."""
    from dragshift.platform.autostart import pythonw

    subprocess.Popen(
        [str(pythonw()), "-m", "dragshift"],
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    print("DragShift is running in the background. Logs: %LOCALAPPDATA%\\DragShift\\logs")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command is not None:
        from dragshift import cli  # imported here so --help and --version stay instant

        if args.command == "ops":
            return cli.ops(args.paths)
        if args.command == "convert":
            return cli.convert(args.paths, args.to)
        return cli.tool(args.op_id, args.paths, args.preset)
    if args.background and sys.stdout is not None:  # under pythonw already: just run
        return relaunch_in_background()
    from dragshift.app import run

    return run()


if __name__ == "__main__":
    raise SystemExit(main())
