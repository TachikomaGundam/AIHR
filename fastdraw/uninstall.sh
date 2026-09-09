#!/usr/bin/env bash
# FastDraw uninstaller — the exact inverse of install.sh, zero residue.
#
#   bash uninstall.sh            # uses the same env overrides as install.sh
#
# Removes: the "plugin" entry install.sh added (JSONC-safe, comment- and
# formatting-preserving), the fetched package dir, and every backup/created
# file recorded in $PKG_DIR/.install-manifest by install.sh. Installs made
# BEFORE the manifest existed cannot identify their .bak files automatically —
# those are listed for a human decision instead of being glob-deleted.
#
# Env: OPENCODE_CONFIG_DIR (same meaning as in install.sh).
set -euo pipefail

CONFIG_DIR="${OPENCODE_CONFIG_DIR:-$HOME/.config/opencode}"
PKG_DIR="$CONFIG_DIR/plugins/fastdraw"
MANIFEST="$PKG_DIR/.install-manifest"

info() { printf '\033[1;34m→\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*"; }

# Strip the exact string entry from a config file's top-level "plugin" array,
# keeping every other byte (comments included) intact. Length-preserving
# `clean` scan mirrors install.sh so spans map 1:1 onto the raw text.
strip_entry() {
  local file="$1"
  [ -f "$file" ] || return 0
  local out
  if out=$(FASTDRAW_PLUGIN_PATH="$PKG_DIR" FASTDRAW_UNINSTALL_FILE="$file" node -e '
const fs = require("fs");
const file = process.env.FASTDRAW_UNINSTALL_FILE;
const entry = process.env.FASTDRAW_PLUGIN_PATH;
const orig = fs.readFileSync(file, "utf8");

let clean = "";
for (let i = 0; i < orig.length; ) {
  const c = orig[i];
  if (c === "\"" || c === "\x27") {
    const q = c; clean += c; i++;
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
  } else { clean += c; i++; }
}

// locate top-level "plugin" key -> "[" index (same logic as install.sh)
let pluginIdx = -1, pluginKeySeen = false;
{
  let inS = null, d = 0, tokStart = -1;
  for (let i = 0; i < clean.length; i++) {
    const c = clean[i];
    if (inS) {
      if (c === "\\") { i++; continue; }
      if (c === inS) {
        inS = null;
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
        tokStart = -1; continue;
      }
      continue;
    }
    if (c === "\"" || c === "\x27") { inS = c; tokStart = i; continue; }
    if (c === "{" || c === "[") { d++; continue; }
    if (c === "}" || c === "]") { d--; continue; }
  }
}
if (!pluginKeySeen || pluginIdx === -1) { console.log("noop:no-plugin-array"); process.exit(0); }

let close = -1;
{
  let inS = null, arr = 0;
  for (let i = pluginIdx; i < clean.length; i++) {
    const c = clean[i];
    if (inS) { if (c === "\\") { i++; continue; } if (c === inS) inS = null; continue; }
    if (c === "\"" || c === "\x27") { inS = c; continue; }
    if (c === "[") arr++;
    else if (c === "]" && --arr === 0) { close = i; break; }
  }
}
if (close === -1) { console.log("noop:unterminated-array"); process.exit(0); }

// collect string element spans inside the array
const spans = [];
for (let i = pluginIdx + 1; i < close; i++) {
  const c = clean[i];
  if (c === "\"" || c === "\x27") {
    const q = c, start = i; i++; let s = "";
    while (i < close) {
      if (clean[i] === "\\" && i + 1 < close) { s += clean[i + 1]; i += 2; continue; }
      if (clean[i] === q) break;
      s += clean[i]; i++;
    }
    spans.push({ start, end: i, value: s });
  }
}
const hits = spans.filter((sp) => sp.value === entry);
if (hits.length === 0) { console.log("noop:entry-absent"); process.exit(0); }

// remove from last hit to first so earlier spans keep their indices
let text = orig;
for (const hit of [...hits].reverse()) {
  let a = hit.start, b = hit.end + 1; // exclusive
  const prevComma = text.slice(0, a).lastSearchComma ? 0 : 0; // placeholder, replaced below
  // find a comma BEFORE the token (between previous element and this one)
  let pa = a - 1;
  while (pa >= 0 && /[ \t\r\n]/.test(text[pa])) pa--;
  if (pa >= 0 && text[pa] === ",") {
    b = swallow(text, b, "\n");
    a = pa; // take the comma with it
    // and the indentation that followed the comma stays before pa — fine
  } else {
    // first element (or no preceding comma): take a trailing comma instead
    let nb = b;
    while (nb < text.length && /[ \t\r\n]/.test(text[nb])) nb++;
    if (text[nb] === ",") b = swallow(text, nb + 1, "\n");
    else b = swallow(text, b, "\n");
  }
  text = text.slice(0, a) + text.slice(b);
}
function swallow(s, from, until) {
  let i = from;
  while (i < s.length && s[i] !== "\n" && /[ \t\r]/.test(s[i])) i++;
  if (i < s.length && s[i] === "\n") i++;
  return i;
}

// If the file we CREATED for install is now an empty plugin list, delete it.
let emptied = false;
try {
  const parsed = JSON.parse(text);
  const keys = Object.keys(parsed ?? {});
  if (keys.length === 1 && keys[0] === "plugin" && Array.isArray(parsed.plugin) && parsed.plugin.length === 0) emptied = true;
} catch { /* JSONC or complex: keep the file */ }
if (emptied) { fs.unlinkSync(file); console.log("deleted-empty:" + file); process.exit(0); }
fs.writeFileSync(file, text);
console.log("stripped:" + hits.length);
' 2>&1); then
    case "$out" in
      stripped:*)   ok "removed entry from $file (${out#stripped:})" ;;
      deleted-empty:*) ok "deleted $file (created by install, plugin list now empty)" ;;
      noop:*)       info "$file: ${out#noop:} — left untouched" ;;
      *)            warn "unexpected result for $file: $out" ;;
    esac
  else
    warn "could not edit $file — remove this entry from its \"plugin\" array by hand:"
    warn "  \"$PKG_DIR\""
    warn "  ($out)"
  fi
}

if [ ! -d "$PKG_DIR" ]; then
  info "no package dir at $PKG_DIR (nothing fetched by install.sh here)"
fi

# 1) configs: prefer the manifest's exact file list; fall back to the standard trio.
if [ -f "$MANIFEST" ]; then
  info "uninstalling via install manifest"
  seen_files=""
  while IFS= read -r line; do
    case "$line" in
      CREATED=*|CONFIG=*)
        f="${line#*=}"
        case " $seen_files " in *" $f "*) continue ;; esac
        seen_files="$seen_files $f"
        strip_entry "$f"
        ;;
    esac
  done < "$MANIFEST"
fi
for f in "$CONFIG_DIR/opencode.jsonc" "$CONFIG_DIR/opencode.json" "$CONFIG_DIR/tui.json"; do
  strip_entry "$f"
done

# 2) backups created by install.sh (manifest-listed only; never globbed).
if [ -f "$MANIFEST" ]; then
  while IFS= read -r line; do
    case "$line" in
      BACKUP=*)
        b="${line#BACKUP=}"
        [ -n "$b" ] || continue
        if [ -f "$b" ]; then rm -f "$b"; ok "removed backup $b"; fi
        ;;
    esac
  done < "$MANIFEST"
else
  if [ -d "$PKG_DIR" ]; then
    leftover=$(find "$CONFIG_DIR" -maxdepth 1 -name '*.bak-*' -newer /dev/null 2>/dev/null | head -5 || true)
    if [ -n "$leftover" ]; then
      warn "pre-manifest install: cannot prove which backups are ours — leaving these for you:"
      printf '  %s\n' $leftover
    fi
  fi
fi

# 3) the fetched package itself (manifest lives inside and goes with it).
if [ -d "$PKG_DIR" ]; then
  rm -rf "$PKG_DIR"
  ok "removed $PKG_DIR"
fi

echo
ok "FastDraw uninstalled. Restart opencode to drop /fastdraw and the fastdraw_* tools."
info "npm-path installs (opencode-fastdraw from the registry): npm uninstall -g opencode-fastdraw"
info "opencode may have cached copies under ~/.cache/opencode/packages — 'hr setup --uninstall' cleans ours."
