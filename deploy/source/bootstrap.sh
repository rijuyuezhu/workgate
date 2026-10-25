#!/bin/sh
# Public bootstrap. URL must itself use this same full commit SHA.
# curl -fsSL https://raw.githubusercontent.com/rijuyuezhu/workgate/SHA/deploy/source/bootstrap.sh |
#     sh -s -- vRELEASE SHA --role control
set -eu
tag=${1-}
expected=${2-}
if [ "$#" -lt 4 ]; then
    echo "usage: bootstrap.sh vRELEASE FULL_COMMIT_SHA --role control|executor|colocate [options]" >&2
    exit 2
fi
shift 2
if ! printf '%s\n' "$tag" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+([abrc][0-9]+)?$'; then
    echo "invalid release tag (floating branches are not supported)" >&2
    exit 2
fi
case "$expected" in
    *[!0-9a-f]*) echo "expected commit must be a lowercase 40-digit SHA" >&2; exit 2 ;;
esac
if [ "${#expected}" -ne 40 ]; then
    echo "expected commit must be a lowercase 40-digit SHA" >&2
    exit 2
fi
if ! command -v git >/dev/null 2>&1; then
    echo "git is required before Workgate bootstrap" >&2
    exit 1
fi
export GIT_TERMINAL_PROMPT=0
scratch=$(mktemp -d "${TMPDIR:-/tmp}/workgate-bootstrap.XXXXXXXX")
trap 'rm -rf -- "$scratch"' EXIT
trap 'exit 1' HUP INT TERM

checkout="$scratch/release"
git clone --depth 1 --single-branch --branch "$tag" -- \
    https://github.com/rijuyuezhu/workgate.git "$checkout" >&2
actual=$(git -C "$checkout" rev-parse HEAD)
tagged=$(git -C "$checkout" rev-parse --verify "refs/tags/$tag^{commit}")
if [ "$actual" != "$expected" ] || [ "$tagged" != "$expected" ]; then
    echo "release tag and expected commit differ; refusing downloaded code" >&2
    exit 1
fi
installer="$checkout/deploy/source/install.sh"
if [ ! -f "$installer" ] || [ -L "$installer" ]; then
    echo "pinned release does not contain a regular source installer" >&2
    exit 1
fi
# uv is the sole bootstrap dependency we can install without admin rights.
if ! command -v uv >/dev/null 2>&1; then
    uv_bin=${UV_INSTALL_DIR:-${XDG_BIN_HOME:-"$HOME/.local/bin"}}
    if [ ! -x "$uv_bin/uv" ]; then
        if ! command -v curl >/dev/null 2>&1; then
            echo "curl is required to install uv: https://docs.astral.sh/uv/" >&2
            exit 1
        fi
        curl -fsSL https://astral.sh/uv/install.sh -o "$scratch/uv-install.sh"
        UV_INSTALL_DIR="$uv_bin" UV_NO_MODIFY_PATH=1 sh "$scratch/uv-install.sh"
    fi
    PATH="$uv_bin:$PATH"
    export PATH
    if ! command -v uv >/dev/null 2>&1; then
        echo "uv installer did not provide an executable in $uv_bin" >&2
        exit 1
    fi
fi
# curl | sh consumes stdin. Reattach a TTY for interactive pairing if present.
if ( : </dev/tty ) 2>/dev/null; then
    sh "$installer" --tag "$tag" --expected-commit "$expected" "$@" </dev/tty
else
    sh "$installer" --tag "$tag" --expected-commit "$expected" "$@" </dev/null
fi
