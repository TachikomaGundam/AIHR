#!/usr/bin/env bash
# FastDraw installer for opencode.
#
#   FASTDRAW_REPO=user/repo bash install.sh
#
# Env overrides:
#   FASTDRAW_REPO=user/repo   FASTDRAW_REF=tag-or-branch   OPENCODE_CONFIG_DIR=/path
set -euo pipefail

REPO="${FASTDRAW_REPO:-}"
REF="${FASTDRAW_REF:-main}"
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
# server.ts / tui.ts import ./roles.js and ./origins.js (TypeScript source
# resolved by opencode's bundler) — every file must be fetched or the plugin
# fails module resolution at first load.
for f in server.ts tui.ts roles.ts origins.ts package.json; do
  curl -fsSL "https://raw.githubusercontent.com/$REPO/$REF/$f" -o "$PKG_DIR/$f"
  ok "fetched $f"
done

# Register the plugin path in a config file's "plugin" array. Idempotent.
#
# IMPORTANT: opencode has TWO separate plugin arrays that must BOTH list the
# plugin for it to be active:
#   • "plugin" in opencode.jsonc (or opencode.json) → server-side plugins
#     (config hooks, agent tools like fastdraw_*).
#   • "plugin" in tui.json → TUI-side plugins (the /fastdraw command and
#     the <leader>m keybind).
# Missing either file = half the plugin loads (typically the TUI piece is
# silently skipped, so /fastdraw and <leader>m disappear while tools still work).
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
    '{\n  "plugin": [\n    ' + JSON.stringify(entry) + '\n  ]\n}\n'
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

// Locate the top-level "plugin" key: a string token whose content is
// "plugin", followed by ":" then "[" at object depth 1 (strings are opaque —
// braces inside string values never count towards depth).
let pluginIdx = -1;
let pluginKeySeen = false;
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
            if (clean.slice(tokStart + 1, i) === "plugin" && !pluginKeySeen) pluginKeySeen = true;
            if (clean.slice(tokStart + 1, i) === "plugin" && pluginIdx === -1) {
              j++;
              while (j < clean.length && /[ \t\r\n]/.test(clean[j])) j++;
              if (clean[j] === "[") pluginIdx = j;
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
if (pluginKeySeen && pluginIdx === -1) {
  console.log('fail:"plugin" key exists but its value is not an array');
  process.exit(1);
}

try {
  const result = (() => {
    if (pluginIdx === -1) {
      const rootBrace = clean.indexOf("{");
      if (rootBrace === -1) throw new Error("file is not a JSON object");
      return {
        newText:
          orig.slice(0, rootBrace + 1) +
          '\n  "plugin": [\n    ' + JSON.stringify(entry) + "\n  ]," +
          orig.slice(rootBrace + 1),
      };
    }
    let close = -1;
    {
      let inS = null, arr = 0;
      for (let i = pluginIdx; i < clean.length; i++) {
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
    }
    if (close === -1) throw new Error('"plugin" array is unterminated');
    const elements = [];
    for (let i = pluginIdx + 1; i < close; i++) {
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
    if (elements.includes(entry)) throw new Error("exists");
    const wantComma = elements.length > 0 && !/,\s*$/.test(clean.slice(pluginIdx + 1, close));
    let insAt = close;
    while (insAt - 1 > pluginIdx && /[ \t\r\n]/.test(orig[insAt - 1])) insAt--;
    return {
      newText:
        orig.slice(0, insAt) +
        (wantComma ? "," : "") +
        "\n    " + JSON.stringify(entry) + "\n  " +
        orig.slice(close),
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
    ok:created)  ok "created $file with FastDraw registered" ;;
    ok:exists)   ok "$file already registers FastDraw" ;;
    ok:backup:*) ok "registered in $file (backup: ${out#ok:backup: })" ;;
    fail:*)
      warn "could not auto-edit $file — add this to its \"plugin\" array manually:"
      warn "  \"$entry\""
      warn "  ($out)"
      ;;
    *)
      warn "unexpected output from register script in $file ($rc): $out"
      warn "  add this to its \"plugin\" array manually:"
      warn "  \"$entry\""
      ;;
  esac
}

# Server-side registration: opencode.jsonc (JSONC) or opencode.json (strict JSON).
# This is what loads the config() hook and exposes fastdraw_* tools to agents.
if [ -f "$CONFIG_DIR/opencode.jsonc" ]; then
  register "$CONFIG_DIR/opencode.jsonc"
elif [ -f "$CONFIG_DIR/opencode.json" ]; then
  register "$CONFIG_DIR/opencode.json"
else
  register "$CONFIG_DIR/opencode.json"
fi

# TUI-side registration: tui.json (STRICT JSON — separate plugin system from
# opencode.jsonc). This is what exposes the /fastdraw command and the <leader>m
# keybind in the TUI. Skipping this file = silent loss of the TUI half.
register "$CONFIG_DIR/tui.json"

echo
ok "FastDraw installed → $PKG_DIR"
info "Restart opencode, then press <leader>m or type /fastdraw"
