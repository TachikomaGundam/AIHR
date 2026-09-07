// QA for install-cli.js (`opencode-hr` bin): end-to-end against a throwaway
// --config-dir. Zero installs: node builtins only; the CLI is spawned exactly
// as the user's npm global bin would spawn it.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CLI = path.join(HERE, "..", "install-cli.js");
const SELF = JSON.parse(readFileSync(path.join(HERE, "..", "package.json"), "utf8")).version;
const FASTDRAW = JSON.parse(
  readFileSync(path.join(HERE, "..", "..", "fastdraw", "package.json"), "utf8"),
).version;

const SERVER_SPECS = [`opencode-hr-agent@${SELF}`, `opencode-fastdraw@${FASTDRAW}`];
const TUI_SPECS = [`opencode-fastdraw@${FASTDRAW}`];

function hr(args, dir) {
  try {
    const out = execFileSync(process.execPath, [CLI, ...args, "--config-dir", dir], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    });
    return { rc: 0, out };
  } catch (err) {
    return { rc: err.status ?? 1, out: String(err.stdout ?? "") + String(err.stderr ?? "") };
  }
}

const freshDir = () => mkdtempSync(path.join(tmpdir(), "opencode-hr-test-"));
const read = (dir, file) => JSON.parse(readFileSync(path.join(dir, file), "utf8"));

test("install registers both configs with pinned specs", () => {
  const dir = freshDir();
  try {
    const { rc, out } = hr(["install"], dir);
    assert.equal(rc, 0, out);
    assert.deepEqual(read(dir, "opencode.json").plugin, SERVER_SPECS);
    assert.deepEqual(read(dir, "tui.json").plugin, TUI_SPECS);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("install is idempotent and says so", () => {
  const dir = freshDir();
  try {
    hr(["install"], dir);
    const before = readFileSync(path.join(dir, "opencode.json"), "utf8");
    const { rc, out } = hr(["install"], dir);
    assert.equal(rc, 0);
    assert.match(out, /already registered/);
    assert.equal(readFileSync(path.join(dir, "opencode.json"), "utf8"), before, "file untouched on no-op");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("install preserves unrelated keys and foreign plugins, upgrades own pins", () => {
  const dir = freshDir();
  try {
    mkdirSync(dir, { recursive: true });
    writeFileSync(
      path.join(dir, "opencode.json"),
      JSON.stringify({ model: "x", plugin: ["someone-else@1.0.0", `opencode-hr-agent@0.1.0`] }, null, 2),
    );
    const { rc } = hr(["install"], dir);
    assert.equal(rc, 0);
    const cfg = read(dir, "opencode.json");
    assert.equal(cfg.model, "x");
    assert.equal(cfg.plugin[0], "someone-else@1.0.0", "foreign entry keeps its position");
    assert.deepEqual(cfg.plugin.slice(1), SERVER_SPECS, "stale pin upgraded in place, fastdraw appended");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("status: 0 after install, 1 with MISS line when an entry is removed", () => {
  const dir = freshDir();
  try {
    hr(["install"], dir);
    assert.equal(hr(["status"], dir).rc, 0);
    const cfg = read(dir, "tui.json");
    cfg.plugin = [];
    writeFileSync(path.join(dir, "tui.json"), JSON.stringify(cfg));
    const { rc, out } = hr(["status"], dir);
    assert.equal(rc, 1);
    assert.match(out, /MISS/);
    assert.match(out, /opencode-fastdraw/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("uninstall removes only our entries", () => {
  const dir = freshDir();
  try {
    mkdirSync(dir, { recursive: true });
    writeFileSync(path.join(dir, "opencode.json"), JSON.stringify({ plugin: ["someone-else@1.0.0"] }));
    hr(["install"], dir);
    const { rc } = hr(["uninstall"], dir);
    assert.equal(rc, 0);
    assert.deepEqual(read(dir, "opencode.json").plugin, ["someone-else@1.0.0"]);
    assert.deepEqual(read(dir, "tui.json").plugin, []);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("JSONC configs are refused, never rewritten", () => {
  const dir = freshDir();
  try {
    mkdirSync(dir, { recursive: true });
    writeFileSync(path.join(dir, "opencode.jsonc"), '{ "plugin": [] /* comments */ }');
    const { rc, out } = hr(["install"], dir);
    assert.equal(rc, 0);
    assert.match(out, /SKIP server/);
    assert.match(out, /JSONC/);
    assert.equal(out.includes("opencode.jsonc"), true);
    assert.equal(readFileSync(path.join(dir, "opencode.jsonc"), "utf8").includes("comments"), true);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("malformed strict JSON aborts with a refusal, not a clobber", () => {
  const dir = freshDir();
  try {
    mkdirSync(dir, { recursive: true });
    const junk = "{ not json at all";
    writeFileSync(path.join(dir, "opencode.json"), junk);
    const { rc, out } = hr(["install"], dir);
    assert.equal(rc, 1);
    assert.match(out, /REFUSING/);
    assert.equal(readFileSync(path.join(dir, "opencode.json"), "utf8"), junk, "file left exactly as found");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("hardening: planted symlink at a deterministic temp name cannot redirect the write", () => {
  const dir = freshDir();
  const victim = path.join(dir, "victim.txt");
  try {
    writeFileSync(victim, "DO NOT TOUCH");
    const { rc } = hr(["install"], dir); // first write; tmp names are now pid+random, victim untouched either way
    assert.equal(rc, 0);
    assert.equal(readFileSync(victim, "utf8"), "DO NOT TOUCH");
    assert.ok(existsSync(path.join(dir, "opencode.json")));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("hardening: existing file mode survives rewrite; new files land 0600", () => {
  const dir = freshDir();
  try {
    mkdirSync(dir, { recursive: true });
    writeFileSync(
      path.join(dir, "opencode.json"),
      JSON.stringify({ plugin: ["opencode-hr-agent@0.0.1"] }, null, 2),
      { mode: 0o600 },
    );
    hr(["install"], dir);
    assert.equal(statSync(path.join(dir, "opencode.json")).mode & 0o777, 0o600, "0600 preserved");
    hr(["install"], path.join(dir, "sub")); // fresh creation path
    assert.equal(statSync(path.join(dir, "sub", "opencode.json")).mode & 0o777, 0o600, "new file 0600");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("hardening: array-spec entries keep their options; version upgraded in place", () => {
  const dir = freshDir();
  try {
    mkdirSync(dir, { recursive: true });
    writeFileSync(
      path.join(dir, "opencode.json"),
      JSON.stringify({ plugin: [["opencode-hr-agent@0.0.1", { apiKey: "keepme" }]] }),
    );
    hr(["install"], dir);
    const cfg = read(dir, "opencode.json");
    assert.deepEqual(cfg.plugin[0], [`opencode-hr-agent@${SELF}`, { apiKey: "keepme" }]);
    const again = hr(["install"], dir);
    assert.match(again.out, /already registered/, "options-entry counts as registered");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("tui.jsonc is refused exactly like opencode.jsonc", () => {
  const dir = freshDir();
  try {
    mkdirSync(dir, { recursive: true });
    writeFileSync(path.join(dir, "tui.jsonc"), "{ /* commented */ }");
    const { out } = hr(["install"], dir);
    assert.match(out, /SKIP tui/);
    assert.equal(existsSync(path.join(dir, "tui.json")), false, "no shadowing tui.json written");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("usage text on unknown command", () => {
  const dir = freshDir();
  try {
    const { rc, out } = hr(["frobnicate"], dir);
    assert.equal(rc, 2);
    assert.match(out, /usage: opencode-hr \[install\|status\|uninstall\]/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
