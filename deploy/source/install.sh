#!/bin/sh
# Prepare a pinned full-extras uv checkout and configure its native role.
set -eu
if ! command -v uv >/dev/null 2>&1; then
    echo "uv required: https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
fi
if ! command -v git >/dev/null 2>&1; then
    echo "git required for Workgate source installation" >&2
    exit 1
fi
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec uv run --no-project --python 3.14 python "$here/install.py" "$@"
