#!/bin/sh
# Prepare a versioned source runtime; service setup/pairing is a separate step.
set -eu
if ! command -v uv >/dev/null 2>&1; then
    echo "uv is required: https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
fi
if ! command -v git >/dev/null 2>&1; then
    echo "git is required before Workgate source preparation" >&2
    exit 1
fi
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec uv run --no-project --python 3.14 python "$here/prepare.py" "$@"
