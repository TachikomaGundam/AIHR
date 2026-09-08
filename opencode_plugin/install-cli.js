#!/usr/bin/env node
// opencode-hr install — one-shot, idempotent registration of the HR plugin
// pair into OpenCode's user config. Ships inside opencode-hr-agent so that
// `npm install -g opencode-hr-agent && opencode-hr install` is the whole
// setup: no hand-editing JSON. Lives up to the plugin's security posture:
// node built-ins only, no network, no shell, no lifecycle scripts.

import { randomBytes } from "node:crypto";
import { chmodSync, mkdirSync, readFileSync, renameSync, existsSync, statSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";

// Floating @latest specs: a registered config must never decay into a stale
// pin (that is what rotted the 192.168.10.191 fresh-machine deploy). `hr setup`
// prints the concrete resolved versions right after install for auditability.
const PLUGINS = {
  server: ["opencode-hr-agent@latest", "opencode-fastdraw@latest"],
  tui: ["opencode-fastdraw@latest"],
};

function configDir() {
  const flag = process.argv.indexOf("--config-dir");
  if (flag !== -1 && process.argv[flag + 1]) return path.resolve(process.argv[flag + 1]);
  return process.env.OPENCODE_CONFIG_DIR ?? path.join(homedir(), ".config", "opencode");
}

function target(kind) {
  const dir = configDir();
  const base = kind === "tui" ? "tui" : "opencode";
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
    throw new Error(`REFUSING to touch ${file}: not strict JSON (comments?). Add the plugin entries by hand instead:\n  ${JSON.stringify(PLUGINS)}`);
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(`REFUSING to touch ${file}: top level is not an object`);
  }
  return parsed;
}

const nameOf = (spec) => (Array.isArray(spec) ? spec[0] : spec).replace(/@[^@/]+$/, "");

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
    const curSpec = Array.isArray(cur) ? cur[0] : cur;
    if (curSpec === spec) continue; // right version; keep user's options entry intact
    if (Array.isArray(cur)) {
      cur[0] = spec; // upgrade in place, preserve attached options object
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

function install() {
  let touched = 0;
  for (const [kind, targetFile] of [["server", target("server")], ["tui", target("tui")]]) {
    if (typeof targetFile === "object" && targetFile.refuse) {
      console.log(`SKIP ${kind}: ${targetFile.refuse} is JSONC (has comments) — add manually:\n  "plugin": ${JSON.stringify(PLUGINS[kind === "server" ? "server" : "tui"])}`);
      continue;
    }
    const cfg = readConfig(targetFile);
    const { out, changed } = mergePlugins(cfg.plugin, kind === "server" ? PLUGINS.server : PLUGINS.tui);
    if (!changed.length) {
      console.log(`OK   ${targetFile} (already registered)`);
      continue;
    }
    cfg.plugin = out;
    writeAtomic(targetFile, cfg);
    for (const c of changed) console.log(`${c.startsWith("+") ? "ADD " : "SET "} ${targetFile}\n     ${c}`);
    touched++;
  }
  console.log(
    touched
      ? "\nRestart opencode, then expect hr_* + fastdraw_* tools (and /fastdraw in the TUI).\nUninstall with: opencode-hr uninstall"
      : "\nNothing to change — both files already declare the @latest plugins.",
  );
  return 0;
}

function status() {
  let bad = 0;
  for (const [kind, file] of [["server", target("server")], ["tui", target("tui")]]) {
    if (typeof file === "object" && file.refuse) {
      console.log(`JSONC ${kind} config: ${file.refuse} (check plugin array by eye)`);
      continue;
    }
    const cfg = existsSync(file) ? readConfig(file) : {};
    const have = (Array.isArray(cfg.plugin) ? cfg.plugin : []).map(nameOf);
    const missing = (kind === "server" ? PLUGINS.server : PLUGINS.tui).map(nameOf).filter((n) => !have.includes(n));
    console.log(`${missing.length ? "MISS " : "OK   "} ${file}${missing.length ? ` missing: ${missing.join(", ")}` : ""}`);
    if (missing.length) bad = 1;
  }
  return bad;
}

function uninstall() {
  for (const [kind, file] of [["server", target("server")], ["tui", target("tui")]]) {
    if (typeof file === "object" && file.refuse) {
      console.log(`SKIP ${kind}: ${file.refuse} — remove entries by hand`);
      continue;
    }
    if (!existsSync(file)) continue;
    const cfg = readConfig(file);
    if (!Array.isArray(cfg.plugin)) continue;
    const names = [...PLUGINS.server, ...PLUGINS.tui].map(nameOf);
    const before = cfg.plugin.length;
    cfg.plugin = cfg.plugin.filter((s) => !names.includes(nameOf(s)));
    if (cfg.plugin.length !== before) {
      writeAtomic(file, cfg);
      console.log(`DEL  ${file} (-${before - cfg.plugin.length})`);
    } else {
      console.log(`OK   ${file} (nothing to remove)`);
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
