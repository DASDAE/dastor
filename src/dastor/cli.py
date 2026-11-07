"""The `dastor` command."""

from __future__ import annotations

import argparse
import sys

from dastor.auth import hash_key, new_key


def main(argv: list[str] | None = None) -> int:
    """Run the dastor command line."""
    parser = argparse.ArgumentParser(prog="dastor", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="serve the spools in a config file")
    serve.add_argument("config", help="path to the YAML config")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    commands.add_parser("new-key", help="print a new API key and its digest")
    commands.add_parser("hash-key", help="print the digest of a key read from stdin")
    args = parser.parse_args(argv)

    if args.command == "new-key":
        key, digest = new_key()
        print(f"key:        {key}\nkey_sha256: {digest}")
    elif args.command == "hash-key":
        print(hash_key(sys.stdin.readline().strip()))
    else:
        import uvicorn  # noqa: PLC0415

        from dastor.app import create_app  # noqa: PLC0415
        from dastor.config import Settings  # noqa: PLC0415

        # One process: each would otherwise index and refresh the spools itself.
        app = create_app(Settings.from_yaml(args.config))
        uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
