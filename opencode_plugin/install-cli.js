#!/usr/bin/env node
// opencode-hr install — one-shot, idempotent registration of the HR plugin
// pair into OpenCode's user config. Ships inside opencode-hr-agent so that
// `npm install -g opencode-hr-agent && opencode-hr install` is the whole
// setup: no hand-editing JSON. Lives up to the plugin's security posture:
// node built-ins only, no network, no shell, no lifecycle scripts.
//
// OpenCode V2 layout: every plugin (server part and TUI part alike) is one
// entry in the `plugins` array of opencode.json(c). Packages that export
// `./tui` alongside their server part auto-load their TUI half, so nothing
// is written to the V1-era tui.json anymore (legacy entries there, in the
// old `plugin` key, and in cli.json are cleaned on write).

import { randomBytes } from "node:crypto";
import { chmodSync, mkdirSync, readFileSync, renameSync, existsSync, statSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";

// Floating @latest specs: a registered config must never decay into a stale
// pin (that is what rotted the pinned-testbed fresh-machine deploy). `hr setup`
// prints the concrete resolved versions right after install for auditability.
const PLUGINS = ["opencode-hr-agent@latest", "opencode-fastdraw@latest"];

function configDir() {
  const flag = process.argv.indexOf("--config-dir");
  if (flag !== -1 && process.argv[flag + 1]) return path.resolve(process.argv[flag + 1]);
  return process.env.OPENCODE_CONFIG_DIR ?? path.join(homedir(), ".config", "opencode");
}

function target(base) {
  const dir = configDir();
  const json = path.join(dir, `${base}.json`);
  const jsonc = path.join(dir, `${base}.jsonc`);
  // Never parse/rewrite JSONC by machine; point the human at the one line to add.
  if (existsSync(jsonc) && !existsSync(json)) return { refuse: jsonc };
  return json;
}

function readConfig(file) {
  if (!existsSync(file)) return {};
  let parsed;
  try {
    parsed = JSON.parse(readFileSync(file, "utf8"));
  } catch {
    throw new Error(`REFUSING to touch ${file}: not strict JSON (comments?). Add the plugin entries by hand instead:\n  "plugins": ${JSON.stringify(PLUGINS)}`);
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(`REFUSING to touch ${file}: top level is not an object`);
  }
  return parsed;
}

// Entry shapes accepted across V1/V2 history: "name@ver", ["name@ver", opts],
// { package: "name@ver", ... }. Normalise to the bare package name.
const nameOf = (spec) => {
  if (Array.isArray(spec)) return String(spec[0]).replace(/@[^@/]+$/, "");
  if (spec && typeof spec === "object" && typeof spec.package === "string")
    return spec.package.replace(/@[^@/]+$/, "");
  return String(spec).replace(/@[^@/]+$/, "");
};

function mergePlugins(list, wanted) {
  const out = Array.isArray(list) ? [...list] : [];
  const changed = [];
  for (const spec of wanted) {
    const name = nameOf(spec);
    const idx = out.findIndex((s) => nameOf(s) === name);
    if (idx === -1) {
      out.push(spec);
      changed.push(`+ ${spec}`);
      continue;
    }
    const cur = out[idx];
    const curSpec = Array.isArray(cur) ? cur[0] : cur && typeof cur === "object" ? cur.package : cur;
    if (curSpec === spec) continue; // right version; keep user's options entry intact
    if (Array.isArray(cur)) {
      cur[0] = spec; // upgrade in place, preserve attached options object
      changed.push(`~ ${curSpec} -> ${spec} (options kept)`);
    } else if (cur && typeof cur === "object") {
      cur.package = spec; // V2 object entry: upgrade in place, options kept
      changed.push(`~ ${curSpec} -> ${spec} (options kept)`);
    } else {
      out[idx] = spec;
      changed.push(`~ ${curSpec} -> ${spec}`);
    }
  }
  return { out, changed };
}

function writeAtomic(file, data) {
  mkdirSync(path.dirname(file), { recursive: true });
  let mode = 0o600;
  try {
    mode = statSync(file).mode & 0o777;
  } catch {
    /* new file: keep 0600 — opencode configs may hold provider keys */
  }
  // O_EXCL (wx): a pre-planted symlink or file at the temp path fails closed
  // instead of being followed; random suffix makes collisions non-deterministic.
  const tmp = `${file}.${process.pid}.${randomBytes(4).toString("hex")}.tmp`;
  try {
    writeFileSync(tmp, JSON.stringify(data, null, 2) + "\n", { mode, flag: "wx" });
    chmodSync(tmp, mode);
    renameSync(tmp, file);
  } catch (err) {
    try {
      chmodSync(file, mode);
    } catch {
      /* best-effort mode restore; rename already preserved it in the common case */
    }
    throw err;
  }
}

const HR_NAMES = () => PLUGINS.map(nameOf);

// Remove our entries from any list under any key; returns [list, removedCount].
function stripOurs(list) {
  if (!Array.isArray(list)) return [list, 0];
  const names = HR_NAMES();
  const kept = list.filter((s) => !names.includes(nameOf(s)));
  return [kept, list.length - kept.length];
}

// Sweep V1 relics: old `plugin` key in the main config, the auto-migrated
// cli.json TUI list, and the legacy tui.json. Only touches files that exist
// and only removes entries belonging to this plugin pair.
function sweepLegacy(mainFile) {
  const notes = [];
  const dir = path.dirname(mainFile);
  const cleanFile = (file, keys) => {
    if (!existsSync(file)) return;
    let cfg;
    try {
      cfg = JSON.parse(readFileSync(file, "utf8"));
    } catch {
      notes.push(`LEGACY-SKIP ${file}: not strict JSON, leave by hand`);
      return;
    }
    if (cfg === null || typeof cfg !== "object" || Array.isArray(cfg)) return;
    let removed = 0;
    for (const key of keys) {
      const [kept, n] = stripOurs(cfg[key]);
      if (n) {
        if (kept.length) cfg[key] = kept;
        else delete cfg[key];
        removed += n;
      }
    }
    if (removed) {
      writeAtomic(file, cfg);
      notes.push(`LEGACY ${file} (-${removed} V1 entries)`);
    }
  };
  cleanFile(mainFile, ["plugin"]);
  cleanFile(path.join(dir, "cli.json"), ["plugins", "plugin"]);
  cleanFile(path.join(dir, "tui.json"), ["plugins", "plugin"]);
  return notes;
}

function install() {
  const file = target("opencode");
  if (typeof file === "object" && file.refuse) {
    console.log(`SKIP: ${file.refuse} is JSONC (has comments) — add manually:\n  "plugins": ${JSON.stringify(PLUGINS)}`);
    return 0;
  }
  const cfg = readConfig(file);
  const { out, changed } = mergePlugins(cfg.plugins, PLUGINS);
  if (changed.length) {
    cfg.plugins = out;
    writeAtomic(file, cfg);
    for (const c of changed) console.log(`${c.startsWith("+") ? "ADD " : "SET "} ${file}\n     ${c}`);
  } else {
    console.log(`OK   ${file} (already registered)`);
  }
  for (const note of sweepLegacy(file)) console.log(note);
  console.log(
    changed.length
      ? "\nConfig reloads automatically (/reload or next start): expect hr_* + fastdraw_* tools and /fastdraw in the TUI.\nUninstall with: opencode-hr uninstall"
      : "\nNothing to change — config already declares the @latest plugins.",
  );
  return 0;
}

function status() {
  const file = target("opencode");
  if (typeof file === "object" && file.refuse) {
    console.log(`JSONC config: ${file.refuse} (check plugins array by eye)`);
    return 0;
  }
  const cfg = existsSync(file) ? readConfig(file) : {};
  const have = (Array.isArray(cfg.plugins) ? cfg.plugins : []).map(nameOf);
  const missing = HR_NAMES().filter((n) => !have.includes(n));
  console.log(`${missing.length ? "MISS " : "OK   "} ${file}${missing.length ? ` missing: ${missing.join(", ")}` : ""}`);
  const dir = path.dirname(file);
  for (const [f, keys] of [[file, ["plugin"]], [path.join(dir, "cli.json"), ["plugins", "plugin"]], [path.join(dir, "tui.json"), ["plugins", "plugin"]]]) {
    if (!existsSync(f)) continue;
    try {
      const c = JSON.parse(readFileSync(f, "utf8"));
      for (const key of keys) {
        const [, n] = stripOurs(c && c[key]);
        if (n) console.log(`WARN ${f}: ${n} legacy '${key}' entr${n === 1 ? "y" : "ies"} — run: opencode-hr install`);
      }
    } catch {
      /* unreadable relic files are reported by install, not status */
    }
  }
  return missing.length ? 1 : 0;
}

function uninstall() {
  const file = target("opencode");
  const files = [[file, ["plugins", "plugin"]]];
  if (typeof file !== "object") {
    const dir = path.dirname(file);
    files.push([path.join(dir, "cli.json"), ["plugins", "plugin"]], [path.join(dir, "tui.json"), ["plugins", "plugin"]]);
  }
  for (const [f, keys] of files) {
    if (typeof f === "object" && f.refuse) {
      console.log(`SKIP: ${f.refuse} — remove entries by hand`);
      continue;
    }
    if (!existsSync(f)) continue;
    const cfg = readConfig(f);
    let removed = 0;
    for (const key of keys) {
      const [kept, n] = stripOurs(cfg[key]);
      if (n) {
        if (kept.length) cfg[key] = kept;
        else delete cfg[key];
        removed += n;
      }
    }
    if (removed) {
      writeAtomic(f, cfg);
      console.log(`DEL  ${f} (-${removed})`);
    } else {
      console.log(`OK   ${f} (nothing to remove)`);
    }
  }
  console.log("\nNote: opencode keeps installed copies under ~/.cache/opencode/packages; delete there if you also want the code gone.");
  return 0;
}

const cmd = process.argv[2] ?? "install";
try {
  const rc = cmd === "install" ? install() : cmd === "status" ? status() : cmd === "uninstall" ? uninstall() : (console.log("usage: opencode-hr [install|status|uninstall]"), 2);
  process.exitCode = rc;
} catch (err) {
  console.error(`opencode-hr ${cmd} failed: ${err instanceof Error ? err.message : String(err)}`);
  process.exitCode = 1;
}
