#!/usr/bin/env bash
# FastDraw installer for opencode 2.x.
#
#   FASTDRAW_REPO=user/repo bash install.sh
#
# Env overrides:
#   FASTDRAW_REPO=user/repo FASTDRAW_REF=tag-or-branch FASTDRAW_SUBDIR=fastdraw OPENCODE_CONFIG_DIR=/path
set -euo pipefail

REPO="${FASTDRAW_REPO:-}"
REF="${FASTDRAW_REF:-main}"
# Subdirectory the plugin lives in on the repo (AIHR keeps it in fastdraw/;
# set empty for a standalone fastdraw repo).
SUBDIR="${FASTDRAW_SUBDIR-fastdraw}"
CONFIG_DIR="${OPENCODE_CONFIG_DIR:-$HOME/.config/opencode}"
PKG_DIR="$CONFIG_DIR/plugins/fastdraw"

if [ -z "$REPO" ]; then
  printf 'FASTDRAW_REPO is required (for example, user/repo)\n' >&2
  exit 2
fi

info() { printf '\033[1;34m→\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*"; }

info "Installing FastDraw from github.com/$REPO@$REF"
mkdir -p "$PKG_DIR"
# server.ts / tui.ts import ./roles.js, ./origins.js, ./omo.js and
# ./home-reach.js (TypeScript source resolved by opencode's bundler) — every
# file must be fetched or the plugin fails module resolution at first load.
FETCH_BASE="https://raw.githubusercontent.com/$REPO/$REF"
[ -n "$SUBDIR" ] && FETCH_BASE="$FETCH_BASE/$SUBDIR"
for f in server.ts tui.ts roles.ts origins.ts omo.ts home-reach.ts package.json; do
  curl -fsSL "$FETCH_BASE/$f" -o "$PKG_DIR/$f"
  ok "fetched $f"
done

# OpenCode 2.x does NOT inject host node_modules for local plugin directories
# (verified against v2.2.7: a bare `@opencode/plugin` import fails to resolve
# unless the directory has its own node_modules). Install prod deps here.
if command -v npm >/dev/null 2>&1; then
  info "installing plugin dependencies (npm install --omit=dev)"
  if (cd "$PKG_DIR" && npm install --omit=dev --no-audit --no-fund --loglevel=error); then
    ok "plugin dependencies installed"
  else
    warn "npm install failed — run it manually: (cd $PKG_DIR && npm install --omit=dev)"
  fi
else
  warn "npm not found — plugin will fail to load until dependencies exist in $PKG_DIR"
fi

# Register the plugin path in the ONE OpenCode 2.x plugins array:
#   "plugins" in opencode.jsonc (or opencode.json).
# One entry loads every part of the plugin (server hooks/tools AND the TUI
# command/keybind) — tui.json registration is obsolete under 2.x. Entries in
# the legacy "plugin" key still load on 2.x, so an existing legacy entry
# counts as registered (no duplicate entry is added; opencode dedups by
# resolved path, but we avoid writing a second one anyway).
#
# The edit is done by an inline node script (node is guaranteed present: it is
# required to run opencode). GNU sed -i -E misbehaves on macOS/BSD, so the
# portable editor below is comment-aware and never rewrites the file wholesale:
# existing comments, formatting and unrelated keys are preserved byte-for-byte.
# Before any edit the file is backed up as <file>.bak-YYYYMMDD-HHMMSS.
register() {
  local file="$1"
  local entry="$PKG_DIR"

  local out rc
  if out=$(FASTDRAW_PLUGIN_PATH="$entry" node - "$file" 2>&1 <<'NODE'
const fs = require("fs");
const path = require("path");

const file = process.argv[2];
const entry = process.env.FASTDRAW_PLUGIN_PATH;
if (!entry) { console.log("fail:FASTDRAW_PLUGIN_PATH not set"); process.exit(1); }

if (!fs.existsSync(file)) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(
    file,
    '{\n  "plugins": [\n    ' + JSON.stringify(entry) + '\n  ]\n}\n'
  );
  console.log("ok:created");
  process.exit(0);
}

const orig = fs.readFileSync(file, "utf8");

// clean: same length as orig; string contents preserved, comments -> spaces
let clean = "";
for (let i = 0; i < orig.length; ) {
  const c = orig[i];
  if (c === '"' || c === "'") {
    const q = c;
    clean += c;
    i++;
    while (i < orig.length) {
      if (orig[i] === "\\" && i + 1 < orig.length) { clean += orig[i] + orig[i + 1]; i += 2; continue; }
      clean += orig[i];
      if (orig[i] === q) { i++; break; }
      i++;
    }
  } else if (c === "/" && orig[i + 1] === "/") {
    while (i < orig.length && orig[i] !== "\n") { clean += " "; i++; }
  } else if (c === "/" && orig[i + 1] === "*") {
    i += 2;
    while (i < orig.length) {
      if (orig[i] === "*" && orig[i + 1] === "/") { clean += "  "; i += 2; break; }
      clean += orig[i] === "\n" ? "\n" : " ";
      i++;
    }
  } else {
    clean += c;
    i++;
  }
}

// Locate top-level "plugins" (V2, our write target) and legacy "plugin"
// (read-only for exists-check) array positions at object depth 1 (strings are
// opaque — braces inside string values never count towards depth).
let pluginsIdx = -1;   // the "plugins" array bracket
let legacyIdx = -1;    // the legacy "plugin" array bracket
let pluginsKeySeen = false;
let legacyKeySeen = false;
{
  let inS = null, d = 0, tokStart = -1;
  for (let i = 0; i < clean.length; i++) {
    const c = clean[i];
    if (inS) {
      if (c === "\\") { i++; continue; }
      if (c === inS) {
        inS = null;
        // token ended at i: key if next non-ws char is ":" and depth is 1
        if (tokStart !== -1 && d === 1) {
          let j = i + 1;
          while (j < clean.length && /[ \t\r\n]/.test(clean[j])) j++;
          if (clean[j] === ":") {
            const key = clean.slice(tokStart + 1, i);
            j++;
            while (j < clean.length && /[ \t\r\n]/.test(clean[j])) j++;
            if (key === "plugins") {
              if (!pluginsKeySeen) pluginsKeySeen = true;
              if (pluginsIdx === -1 && clean[j] === "[") pluginsIdx = j;
            } else if (key === "plugin") {
              if (!legacyKeySeen) legacyKeySeen = true;
              if (legacyIdx === -1 && clean[j] === "[") legacyIdx = j;
            }
          }
        }
        tokStart = -1;
        continue;
      }
      continue;
    }
    if (c === '"' || c === "'") { inS = c; tokStart = i; continue; }
    if (c === "{" || c === "[") { d++; continue; }
    if (c === "}" || c === "]") { d--; continue; }
  }
}
if (pluginsKeySeen && pluginsIdx === -1) {
  console.log('fail:"plugins" key exists but its value is not an array');
  process.exit(1);
}

function arrayElements(openIdx) {
  let close = -1;
  let inS = null, arr = 0;
  for (let i = openIdx; i < clean.length; i++) {
    const c = clean[i];
    if (inS) {
      if (c === "\\") { i++; continue; }
      if (c === inS) inS = null;
      continue;
    }
    if (c === '"' || c === "'") { inS = c; continue; }
    if (c === "[") arr++;
    else if (c === "]" && --arr === 0) { close = i; break; }
  }
  if (close === -1) throw new Error('"plugins" array is unterminated');
  const elements = [];
  for (let i = openIdx + 1; i < close; i++) {
    const c = clean[i];
    if (c === '"' || c === "'") {
      let s = "", q = c;
      i++;
      while (i < close) {
        if (clean[i] === "\\" && i + 1 < close) { s += clean[i + 1]; i += 2; continue; }
        if (clean[i] === q) break;
        s += clean[i];
        i++;
      }
      elements.push(s);
    }
  }
  return { close, elements };
}

try {
  // exists-check across BOTH arrays (legacy entry counts).
  if (legacyIdx !== -1 && arrayElements(legacyIdx).elements.includes(entry)) {
    console.log("ok:exists-legacy");
    process.exit(0);
  }
  const result = (() => {
    if (pluginsIdx === -1) {
      const rootBrace = clean.indexOf("{");
      if (rootBrace === -1) throw new Error("file is not a JSON object");
      return {
        newText:
          orig.slice(0, rootBrace + 1) +
          '\n  "plugins": [\n    ' + JSON.stringify(entry) + "\n  ]," +
          orig.slice(rootBrace + 1),
      };
    }
    const { close, elements } = arrayElements(pluginsIdx);
    if (elements.includes(entry)) throw new Error("exists");
    const wantComma = elements.length > 0 && !/,\s*$/.test(clean.slice(pluginsIdx + 1, close));
    // Walk back over whitespace AND blanked comments (clean has comments as
    // spaces) so the comma lands right after the last real token — never
    // inside a trailing line comment.
    let insAt = close;
    while (insAt - 1 > pluginsIdx && /[ \t\r\n]/.test(clean[insAt - 1])) insAt--;
    return {
      newText:
        orig.slice(0, insAt) +
        (wantComma ? "," : "") +
        "\n    " + JSON.stringify(entry) +
        orig.slice(insAt),
    };
  })();

  const pad = (n) => String(n).padStart(2, "0");
  const now = new Date();
  const bak =
    file +
    ".bak-" + now.getFullYear() + pad(now.getMonth() + 1) + pad(now.getDate()) +
    "-" + pad(now.getHours()) + pad(now.getMinutes()) + pad(now.getSeconds());
  fs.copyFileSync(file, bak);
  fs.writeFileSync(file, result.newText);
  console.log("ok:backup: " + bak);
} catch (e) {
  if (e.message === "exists") { console.log("ok:exists"); process.exit(0); }
  console.log("fail:" + e.message);
  process.exit(1);
}
NODE
); then
    rc=0
  else
    rc=$?
  fi
  case "$out" in
    ok:created)
      ok "created $file with FastDraw registered"
      printf 'CREATED=%s\n' "$file" >> "$PKG_DIR/.install-manifest"
      ;;
    ok:exists)         ok "$file already registers FastDraw" ;;
    ok:exists-legacy)  ok "$file already registers FastDraw (legacy \"plugin\" key)" ;;
    ok:backup:*)
      ok "registered in $file (backup: ${out#ok:backup: })"
      printf 'BACKUP=%s\n' "${out#ok:backup: }" >> "$PKG_DIR/.install-manifest"
      ;;
    fail:*)
      warn "could not auto-edit $file — add this to its \"plugins\" array manually:"
      warn "  \"$entry\""
      warn "  ($out)"
      ;;
    *)
      warn "unexpected output from register script in $file ($rc): $out"
      warn "  add this to its \"plugins\" array manually:"
      warn "  \"$entry\""
      ;;
  esac
}

# Single registration: opencode.jsonc (JSONC) or opencode.json (strict JSON).
# Under OpenCode 2.x one entry loads the whole plugin — server hooks,
# fastdraw_* tools, the /fastdraw slash command and the <leader>m keybind.
if [ -f "$CONFIG_DIR/opencode.jsonc" ]; then
  register "$CONFIG_DIR/opencode.jsonc"
elif [ -f "$CONFIG_DIR/opencode.json" ]; then
  register "$CONFIG_DIR/opencode.json"
else
  register "$CONFIG_DIR/opencode.json"
fi

echo
ok "FastDraw installed → $PKG_DIR"
info "Restart opencode, then press <leader>m or type /fastdraw"
