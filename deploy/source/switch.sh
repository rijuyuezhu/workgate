#!/bin/sh
# Explicit Linux Control switch to an already prepared pinned release.
set -eu
if ! command -v uv >/dev/null 2>&1; then
    echo "uv is required for Workgate source cutover" >&2
    exit 1
fi
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec uv run --no-project --python 3.14 python "$here/switch.py" "$@"
