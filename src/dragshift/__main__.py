"""Command-line entry point: `dragshift` and `python -m dragshift`."""

import argparse

from dragshift import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dragshift",
        description="Drag a file, tap a key, and a wheel converts it or runs a tool on it.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    from dragshift.app import run  # imported here so --help and --version stay instant

    return run()


if __name__ == "__main__":
    raise SystemExit(main())
