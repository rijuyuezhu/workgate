"""Run a direct, unsupervised Control for local first-run pairing only."""

import sys

from ..main import _build_parser


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("temporary Control requires one config path")
    # Invoking a parsed handler directly deliberately skips the normal
    # Parent/Child supervisor; the setup command owns this exact process.
    args = _build_parser().parse_args(["control", "--config", sys.argv[1]])
    args.handler(args)


if __name__ == "__main__":
    main()
