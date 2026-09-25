#!/bin/sh
# AIHR turnkey installer — POSIX sh (dash/bash/sh compatible), no bashisms.
#
# Places the bundle tree D = $HOME/.aihr (app/ pg/ share/; data/ is owned by
# `hr`, never touched here) and hands over to `hr install-post`.
# Windows: use scripts/install.ps1.
#
# Usage:
#   sh install.sh [--version V] [--bundle aihr-V-os-arch.tar.gz] [--port N]
#                 [--reinstall] [--no-run-installer]
set -eu

REPO="${AIHR_REPO:-TachikomaGundam/AIHR}"
D="$HOME/.aihr"
VERSION=""
BUNDLE=""
PORT=""
REINSTALL=0
NO_RUN=0
WORK=""

say() { printf '%s\n' "$*"; }
err() { printf 'error: %s\n' "$*" >&2; exit 1; }
cleanup() { [ -n "$WORK" ] && rm -rf "$WORK" || true; }
trap cleanup EXIT INT TERM

usage() {
    cat <<'USAGE'
usage: sh install.sh [--version V] [--bundle FILE.tar.gz] [--port N]
                     [--reinstall] [--no-run-installer]

  --version V            release version to fetch (default: GitHub latest)
  --bundle FILE          install this local tarball instead of downloading
  --port N               database port handed to `hr install-post`
  --reinstall            replace app/ pg/ share/ in an existing install
                         (data/ is never touched)
  --no-run-installer     place files only; skip `hr install-post` / `hr db-up`

  env AIHR_MIRROR=URL    fallback download mirror prefix
                         (default https://gh-proxy.com/; bytes always stay
                         pinned to the github-issued checksum sidecar)
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --version) [ $# -ge 2 ] || err "--version needs a value"; VERSION=$2; shift 2 ;;
        --bundle)  [ $# -ge 2 ] || err "--bundle needs a value"; BUNDLE=$2; shift 2 ;;
        --port)    [ $# -ge 2 ] || err "--port needs a value"; PORT=$2; shift 2 ;;
        --reinstall) REINSTALL=1; shift ;;
        --no-run-installer) NO_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; err "unknown argument: $1" ;;
    esac
done

[ -z "$PORT" ] || case "$PORT" in *[!0-9]*|'') err "--port must be a number: $PORT" ;; esac

if [ "$(id -u)" -eq 0 ]; then
    err "user-scoped only: run as the user who will use AIHR (no sudo/root)"
fi

uname_s=$(uname -s)
uname_m=$(uname -m)
case "$uname_s" in
    Linux)  OS=linux ;;
    Darwin) OS=macos ;;
    MINGW*|MSYS*|CYGWIN*|Windows_NT)
        err "Windows: run scripts/install.ps1 in PowerShell instead" ;;
    *) err "unsupported platform: $uname_s" ;;
esac
case "$uname_m" in
    x86_64|amd64)  ARCH=x86_64 ;;
    aarch64|arm64) ARCH=aarch64 ;;
    *) err "unsupported architecture: $uname_m" ;;
esac

command -v curl >/dev/null 2>&1 || err "curl is required"
if command -v sha256sum >/dev/null 2>&1; then
    SHA_TOOL="sha256sum"
elif command -v shasum >/dev/null 2>&1; then
    SHA_TOOL="shasum -a 256"
else
    err "need sha256sum or shasum to verify downloads"
fi

resolve_latest() {
    tag=""
    if command -v gh >/dev/null 2>&1; then
        tag=$(gh api "repos/$REPO/releases/latest" --jq .tag_name 2>/dev/null || true)
    fi
    if [ -z "$tag" ]; then
        tag=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
            | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)
    fi
    [ -n "$tag" ] || err "could not resolve the latest release; pass --version V"
    VERSION=${tag#v}
    say "resolved latest version: v$VERSION"
}

asset_id() { # the numeric id that precedes the wanted "name" in api JSON
    curl -fsS --connect-timeout 10 -H 'Accept: application/vnd.github+json' \
        "https://api.github.com/repos/$REPO/releases/tags/v$VERSION" \
        | awk -v want="\"name\": \"$1\"" \
            'match($0, /"id": [0-9]+/) {id = substr($0, RSTART + 6, RLENGTH - 6)}
             index($0, want) {print id; exit}'
}

fetch_api_asset() { # authoritative download through the api octet lane
    aid=$(asset_id "$1")
    [ -n "$aid" ] || return 1
    curl -fL --connect-timeout 10 --retry 2 --speed-limit 4096 --speed-time 30 --proto '=https' \
        -H 'Accept: application/octet-stream' \
        "https://api.github.com/repos/$REPO/releases/assets/$aid" -o "$2"
}

fetch_bulk() { # bulk bytes: github, mirror, api octet, in that order. Each
    # lane is only a pipe: integrity is pinned later by the github-lane
    # sidecar, so a lying or stalling lane can delay the install but can
    # never substitute bytes. Stall guards (--speed-limit/--speed-time)
    # bound each attempt instead of the hours-long curl --retry hangs
    # (testbed-box acceptance run of 2026-09-24: github.com redirect target
    # stalled at 0 B/s while gh-proxy served 1.4 MB/s on the same LAN).
    curl -fL --connect-timeout 15 --speed-limit 4096 --speed-time 30 --retry 1 --proto '=https' \
        -o "$2" "$1" && return 0
    say "github lane failed or stalled; trying mirror"
    curl -fL --connect-timeout 15 --speed-limit 4096 --speed-time 60 --retry 1 --proto '=https' \
        -o "$2" "${AIHR_MIRROR:-https://gh-proxy.com/}$1" && return 0
    say "mirror lane failed; trying api octet lane (slow, but authoritative)"
    fetch_api_asset "$(basename -- "$1")" "$2"
}

if [ -n "$BUNDLE" ]; then
    [ -f "$BUNDLE" ] || err "bundle not found: $BUNDLE"
    ARCHIVE=$BUNDLE
    if [ -z "$VERSION" ]; then
        VERSION=$(basename "$BUNDLE" | sed -n 's/^aihr-\([^-]*\)-.*/\1/p')
        [ -n "$VERSION" ] || VERSION="unknown"
    fi
else
    [ -n "$VERSION" ] || resolve_latest
    ASSET="aihr-$VERSION-$OS-$ARCH.tar.gz"
    BASE="https://github.com/$REPO/releases/download/v$VERSION/$ASSET"
    WORK=$(mktemp -d "${TMPDIR:-/tmp}/aihr-install.XXXXXX")
    say "downloading $BASE"
    # sidecar first, and never through the mirror: it is the integrity anchor
    curl -fL --connect-timeout 10 --retry 2 --speed-limit 4096 --speed-time 30 --proto '=https' \
        -o "$WORK/$ASSET.sha256" "$BASE.sha256" \
        || fetch_api_asset "$ASSET.sha256" "$WORK/$ASSET.sha256" \
        || err "checksum sidecar missing for $ASSET"
    fetch_bulk "$BASE" "$WORK/$ASSET" \
        || err "download failed: $BASE (exists for $OS-$ARCH? pass --version)"
    ARCHIVE="$WORK/$ASSET"
fi

ARCHIVE_DIR=$(unset CDPATH; cd -- "$(dirname -- "$ARCHIVE")" && pwd)
ARCHIVE_NAME=$(basename -- "$ARCHIVE")
if [ -f "$ARCHIVE_DIR/$ARCHIVE_NAME.sha256" ]; then
    say "verifying sha256..."
    (unset CDPATH; cd -- "$ARCHIVE_DIR" && $SHA_TOOL -c "$ARCHIVE_NAME.sha256" >/dev/null) \
        || err "checksum mismatch for $ARCHIVE_NAME — refusing to install"
    say "checksum ok"
elif [ -z "$WORK" ]; then
    say "warning: no $ARCHIVE_NAME.sha256 next to the bundle — checksum NOT verified"
fi

if [ -d "$D" ] && [ "$REINSTALL" -eq 0 ]; then
    err "$D already exists — pass --reinstall to refresh app/ pg/ share/ (data/ is never touched)"
fi

STAGE=${WORK:-$(mktemp -d "${TMPDIR:-/tmp}/aihr-stage.XXXXXX")}
[ -n "$WORK" ] || WORK=$STAGE
mkdir -p "$STAGE/root"
tar -xzf "$ARCHIVE" -C "$STAGE/root"
[ -x "$STAGE/root/app/hr" ] || err "corrupt bundle: app/hr missing or not executable"
[ -x "$STAGE/root/pg/bin/initdb" ] || err "corrupt bundle: pg/bin/initdb missing"

mkdir -p "$D"
for comp in app pg share; do
    rm -rf "${D:?}/${comp}"
    mv "$STAGE/root/$comp" "$D/$comp"
done
say "placed bundle at $D (data/ untouched)"

if [ "$NO_RUN" -eq 1 ]; then
    say ""
    say "files placed; next steps:"
    say "  \"\$D/app/hr\" install-post${PORT:+ --port $PORT}"
    say "  \"\$D/app/hr\" db-up${PORT:+ --port $PORT}"
else
    if [ -n "$PORT" ]; then
        "$D/app/hr" install-post --port "$PORT" || err "hr install-post failed — see output above; D=$D"
    else
        "$D/app/hr" install-post || err "hr install-post failed — see output above; D=$D"
    fi
    if [ -n "$PORT" ]; then
        "$D/app/hr" db-up --port "$PORT" && say "database up" \
            || say "warning: hr db-up failed — start it later with: \"$D/app/hr\" db-up"
    else
        "$D/app/hr" db-up && say "database up" \
            || say "warning: hr db-up failed — start it later with: \"$D/app/hr\" db-up"
    fi
fi

say ""
say "AIHR $VERSION ($OS-$ARCH) installed at $D"
say "  PATH hint:  export PATH=\"\$HOME/.aihr/bin:\$PATH\""
say "  uninstall:  hr self-uninstall --yes"
