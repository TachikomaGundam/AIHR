// node:test coverage for opencode_plugin/hr-invocation.ts (plan hr-agent-021, todo 3).
//
// Zero-install by design: this file imports ONLY node builtins plus the pure
// helper leaf (which itself imports node:fs / node:os / node:path and nothing
// else — no SDK, no child_process). It never imports server.ts (Metis F2: its
// value-import of @opencode-ai/plugin makes it un-importable by bare node).
//
// A1 dual-mode import: CI runs official node >= 22.18 where type-stripping
// lets `import("../hr-invocation.ts")` resolve directly; local box binaries
// without TypeScript support (e.g. Ubuntu's v22.22.1) throw
// ERR_UNKNOWN_FILE_EXTENSION, and the tsc-erased sibling .js (A1 recipe) is
// used instead. One test file is green in both modes.

import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { chmodSync, existsSync, mkdtempSync, rmSync, statSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import path from "node:path";

let hr;
try {
  hr = await import("../hr-invocation.ts");
} catch (err) {
  if (err?.code === "ERR_UNKNOWN_FILE_EXTENSION" || err?.code === "ERR_MODULE_NOT_FOUND") {
    hr = await import("../hr-invocation.js"); // tsc-erased build (A1 local recipe)
  } else {
    throw err;
  }
}

// ---------------------------------------------------------------------------
// Env hygiene: every test starts from a snapshotted HR_HOME/cwd and leaves
// behind no temp dirs. rm -rf touches ONLY paths created via mkdtempSync in
// this file; locked (chmod-000) roots are chmod-restored before removal.
// ---------------------------------------------------------------------------

/** @type {string[]} mkdtemp roots created during the current test */
const tempRoots = [];

function mkTempRoot() {
  const dir = mkdtempSync(path.join(tmpdir(), "hr-test-"));
  tempRoots.push(dir);
  return dir;
}

const envStack = [];

beforeEach(() => {
  envStack.push({
    hadHrHome: Object.prototype.hasOwnProperty.call(process.env, "HR_HOME"),
    hrHome: process.env.HR_HOME,
    cwd: process.cwd(),
  });
});

afterEach(() => {
  const snap = envStack.pop();
  if (snap.hadHrHome) {
    process.env.HR_HOME = snap.hrHome;
  } else {
    delete process.env.HR_HOME;
  }
  process.chdir(snap.cwd);
  while (tempRoots.length > 0) {
    const dir = tempRoots.pop();
    chmodSync(dir, 0o700); // undo any chmod-000 lock so rm can recurse
    rmSync(dir, { recursive: true, force: true });
  }
});

// ---------------------------------------------------------------------------
// ensureHrHome
// ---------------------------------------------------------------------------

test("ensureHrHome creates nested dirs under a temp root and returns the resolved path", () => {
  const base = mkTempRoot();
  const target = path.join(base, "a", "b", "c");
  const result = hr.ensureHrHome(target);
  assert.equal(result, path.resolve(target));
  assert.ok(statSync(result).isDirectory());

  // "resolved" means normalized too: an inner .. segment must not survive,
  // and the skipped-through dir must NOT be created.
  const raw = `${base}/up/../down/nested`;
  const resolved2 = hr.ensureHrHome(raw);
  assert.equal(resolved2, path.join(base, "down", "nested"));
  assert.ok(existsSync(path.join(base, "down", "nested")));
  assert.equal(existsSync(path.join(base, "up")), false);
});

test("ensureHrHome is idempotent: a second call on the same path returns the same resolved path", () => {
  const base = mkTempRoot();
  const target = path.join(base, "nested", "deeper");
  const first = hr.ensureHrHome(target);
  const second = hr.ensureHrHome(target);
  assert.equal(second, first);
  assert.ok(statSync(second).isDirectory());
});

test("ensureHrHome default parameter honors process.env.HR_HOME", () => {
  const base = mkTempRoot();
  const fromEnv = path.join(base, "from-env");
  process.env.HR_HOME = fromEnv;
  const result = hr.ensureHrHome();
  assert.equal(result, path.resolve(fromEnv));
  assert.ok(existsSync(result));
});

// The os.homedir()-based `~/hr` fallback is deliberately NOT exercised:
// homedir() cannot be redirected without a mock (banned) and creating the
// real ~/hr on the QA box is forbidden (plan todo 3 MUST-NOT). Guard
// comment: homedir branch safety is covered indirectly — as long as HR_HOME
// is set, the default parameter must resolve to the env value and never to
// a homedir-derived path, and an explicit argument must win over the env.
test("ensureHrHome: HR_HOME env wins over the homedir fallback; explicit arg wins over env (real ~/hr untouched)", () => {
  const base = mkTempRoot();
  const fromEnv = path.join(base, "env-wins");
  process.env.HR_HOME = fromEnv;
  const result = hr.ensureHrHome();
  assert.notEqual(result, path.join(homedir(), "hr"));
  assert.equal(result, path.resolve(fromEnv));

  const explicit = path.join(base, "explicit");
  assert.equal(hr.ensureHrHome(explicit), path.resolve(explicit));
  assert.ok(existsSync(explicit));
});

// ---------------------------------------------------------------------------
// buildHrArgs — argv fixtures pinned 1:1 against the pre-rewire server.ts
// tool bodies (git show 35b64c2:opencode_plugin/server.ts:28-77). deepEqual
// on the exact array; no re-implementation of the builder logic here.
// ---------------------------------------------------------------------------

test("buildHrArgs status -> ['status'] and ignores irrelevant args", () => {
  assert.deepEqual(hr.buildHrArgs("status"), ["status"]);
  assert.deepEqual(hr.buildHrArgs("status", { task: "x", preset: "p", setState: true }), ["status"]);
});

test("buildHrArgs recommend -> ['recommend','--task',task]; missing task emits '' placeholder", () => {
  assert.deepEqual(hr.buildHrArgs("recommend", { task: "hire senior engineer" }), [
    "recommend",
    "--task",
    "hire senior engineer",
  ]);
  assert.deepEqual(hr.buildHrArgs("recommend"), ["recommend", "--task", ""]);
});

test("buildHrArgs apply covers every optional-flag permutation", async (t) => {
  await t.test("bare", () => {
    assert.deepEqual(hr.buildHrArgs("apply", {}), ["apply"]);
  });
  await t.test("+preset", () => {
    assert.deepEqual(hr.buildHrArgs("apply", { preset: "gold" }), ["apply", "--preset", "gold"]);
  });
  await t.test("+setState", () => {
    assert.deepEqual(hr.buildHrArgs("apply", { setState: true }), ["apply", "--set-state"]);
  });
  await t.test("+both (preset precedes --set-state)", () => {
    assert.deepEqual(hr.buildHrArgs("apply", { preset: "gold", setState: true }), [
      "apply",
      "--preset",
      "gold",
      "--set-state",
    ]);
  });
  await t.test("setState:false emits nothing", () => {
    assert.deepEqual(hr.buildHrArgs("apply", { setState: false }), ["apply"]);
  });
  await t.test('preset:"" (falsy) emits nothing', () => {
    assert.deepEqual(hr.buildHrArgs("apply", { preset: "" }), ["apply"]);
  });
});

test("buildHrArgs apply-preview covers every optional-flag permutation", async (t) => {
  await t.test("bare", () => {
    assert.deepEqual(hr.buildHrArgs("apply-preview", {}), ["apply-preview"]);
  });
  await t.test("+preset", () => {
    assert.deepEqual(hr.buildHrArgs("apply-preview", { preset: "p2" }), [
      "apply-preview",
      "--preset",
      "p2",
    ]);
  });
  await t.test("+setState", () => {
    assert.deepEqual(hr.buildHrArgs("apply-preview", { setState: true }), [
      "apply-preview",
      "--set-state",
    ]);
  });
  await t.test("+both", () => {
    assert.deepEqual(hr.buildHrArgs("apply-preview", { preset: "p2", setState: true }), [
      "apply-preview",
      "--preset",
      "p2",
      "--set-state",
    ]);
  });
});

test("buildHrArgs apply-rollback -> ['apply-rollback',backup]; missing backup emits '' placeholder", () => {
  assert.deepEqual(hr.buildHrArgs("apply-rollback", { backup: "backup-2026-09-06.json" }), [
    "apply-rollback",
    "backup-2026-09-06.json",
  ]);
  assert.deepEqual(hr.buildHrArgs("apply-rollback"), ["apply-rollback", ""]);
});

test("buildHrArgs apply-backups -> ['apply-backups']", () => {
  assert.deepEqual(hr.buildHrArgs("apply-backups"), ["apply-backups"]);
});

test("buildHrArgs with an unknown tool name throws TypeError (no silent default arm)", () => {
  // Runtime-only value: the HrToolName union rejects this at compile time;
  // at runtime the exhaustive switch must fall through to the never-guard.
  const bogusTool = "not-a-real-tool";
  assert.throws(() => hr.buildHrArgs(bogusTool, {}), {
    name: "TypeError",
    message: "unknown HR tool: not-a-real-tool",
  });
});

// ---------------------------------------------------------------------------
// classifyHrError — exactly 4 branches, pinned verbatim from
// hr-invocation.ts. Order matters: (a) spawn-ENOENT, (b) HR_HOME unusable,
// (c) engine missing (err.stderr ONLY — Metis F5), (d) legacy fallback.
// ---------------------------------------------------------------------------

const SPAWN_NOT_FOUND =
  "HR command failed: the HR engine launcher `python3` was not found on this machine. " +
  "Install Python 3 or fix PATH so that `python3` resolves, then retry.";

const hrHomeUnusable = (p) =>
  `HR command failed: HR_HOME "${p}" is not usable. ` +
  "Fix its permissions (e.g. chmod) or export HR_HOME=<writable path>, then retry.";

const ENGINE_MISSING =
  "HR command failed: the HR engine package is missing (`No module named 'hr'`). " +
  "Install it with `python3 -m pip install --user -U aihr`. " +
  "On PEP 668 managed distributions add `--break-system-packages` to that command, or use a virtual environment.";

const fallback = (detail) => `HR command failed: ${detail}\nRun \`hr --help\` for command usage.`;

test("classifyHrError (a): spawn-side ENOENT -> python3 launcher guidance", () => {
  const err = Object.assign(new Error("spawn python3 ENOENT"), {
    code: "ENOENT",
    syscall: "spawn python3",
  });
  assert.equal(hr.classifyHrError(err, "/tmp/anywhere"), SPAWN_NOT_FOUND);
  assert.ok(SPAWN_NOT_FOUND.startsWith("HR command failed: "));

  // bare "spawn" (no suffix) also matches the prefix rule
  const bare = Object.assign(new Error("spawn ENOENT"), { code: "ENOENT", syscall: "spawn" });
  assert.equal(hr.classifyHrError(bare, "/tmp/anywhere"), SPAWN_NOT_FOUND);

  // negative control: ENOENT with an unrelated syscall must NOT take (a)
  const openErr = Object.assign(new Error("open failed"), { code: "ENOENT", syscall: "open" });
  assert.equal(hr.classifyHrError(openErr, "/tmp/anywhere"), fallback("open failed"));
});

// Non-root precondition for the chmod-000 leg: the QA box and CI runners
// both execute as an unprivileged user (uid != 0), where mode 000 on a
// user-owned dir yields a genuine EACCES from mkdirSync.
test("classifyHrError (b): HR_HOME unusable -> guidance names HR_HOME, the path, and chmod/perms", async (t) => {
  await t.test("real unwritable dir (chmod-000 mkdtemp child) throws EACCES and classifies to (b)", () => {
    const base = mkTempRoot();
    const lockedTarget = path.join(base, "locked", "nested");
    chmodSync(base, 0o000);
    let caught = null;
    try {
      hr.ensureHrHome(lockedTarget);
    } catch (err) {
      caught = err;
    } finally {
      chmodSync(base, 0o700); // restore before any cleanup touches the tree
    }
    assert.ok(caught, "ensureHrHome must throw on an unwritable HR_HOME");
    assert.equal(caught.code, "EACCES");
    const text = hr.classifyHrError(caught, path.resolve(lockedTarget));
    assert.equal(text, hrHomeUnusable(path.resolve(lockedTarget)));
    assert.ok(text.includes("HR_HOME"));
    assert.ok(text.includes(path.resolve(lockedTarget)));
    assert.match(text, /chmod|perm/i);
  });

  await t.test("synthetic EACCES / EPERM codes classify to (b)", () => {
    for (const code of ["EACCES", "EPERM"]) {
      const err = Object.assign(new Error(`${code} denied`), { code });
      assert.equal(hr.classifyHrError(err, "/home/x/hr"), hrHomeUnusable("/home/x/hr"));
    }
  });

  await t.test("ENOENT with syscall mkdir (recursive-create race) classifies to (b)", () => {
    const err = Object.assign(new Error("mkdir failed"), { code: "ENOENT", syscall: "mkdir" });
    const text = hr.classifyHrError(err, "/nope/hr");
    assert.equal(text, hrHomeUnusable("/nope/hr"));
    assert.match(text, /chmod|perm/i);
  });
});

test("classifyHrError (c): engine-missing marker on stderr -> install guidance, never sudo", async (t) => {
  const cases = [
    ["single-quoted string", "No module named 'hr'"],
    ["double-quoted string", 'No module named "hr"'],
    ["bare string", "No module named hr"],
    [
      "multi-line stderr around the marker",
      "Traceback (most recent call last):\n  File \"<frozen runpy>\", line 198\nModuleNotFoundError: No module named 'hr'\n",
    ],
  ];
  for (const [label, stderr] of cases) {
    await t.test(label, () => {
      const err = Object.assign(new Error("Command failed"), { code: 1, stderr });
      const text = hr.classifyHrError(err, "/tmp/hrhome");
      assert.equal(text, ENGINE_MISSING);
      assert.ok(text.includes("python3 -m pip install --user -U aihr"));
      assert.ok(text.includes("--break-system-packages"));
      assert.equal(text.includes("sudo"), false);
    });
  }

  await t.test("Buffer stderr is stringified and matches", () => {
    const err = Object.assign(new Error("Command failed"), {
      code: 1,
      stderr: Buffer.from("No module named 'hr'"),
    });
    assert.equal(hr.classifyHrError(err, "/tmp/hrhome"), ENGINE_MISSING);
  });

  await t.test("non-matching stderr does NOT take (c)", () => {
    const err = Object.assign(new Error("Command failed"), {
      code: 1,
      stderr: 'ModuleNotFoundError: No module named "numpy"',
    });
    assert.equal(hr.classifyHrError(err, "/tmp/hrhome"), fallback("Command failed"));
  });
});

test("classifyHrError (c) reads err.stderr ONLY: numeric-code error whose MESSAGE carries the marker lands in (d) [Metis F5 guard]", () => {
  const message = "Command failed: python3 -m hr status\nNo module named 'hr'";
  const err = Object.assign(new Error(message), { code: 1, stderr: "" });
  const text = hr.classifyHrError(err, "/tmp/hrhome");
  assert.equal(text, fallback(message));
  assert.ok(text.startsWith("HR command failed: "));
  assert.ok(text.includes("hr --help"));
  assert.equal(text.includes("--break-system-packages"), false);
});

test("classifyHrError (d): fallback preserves the legacy prefix for Errors and non-Error inputs", async (t) => {
  await t.test("Error('boom')", () => {
    const text = hr.classifyHrError(new Error("boom"), "/tmp/hrhome");
    assert.ok(text.startsWith("HR command failed: boom"));
    assert.equal(text, fallback("boom"));
    assert.ok(text.includes("hr --help"));
  });

  await t.test("non-Error input is String()-ed", () => {
    assert.equal(hr.classifyHrError("plain string", "/tmp/hrhome"), fallback("plain string"));
    assert.equal(hr.classifyHrError({ note: "shapeless" }, "/tmp/hrhome"), fallback("[object Object]"));
  });

  await t.test("null/undefined throws classify to (d), never crash (todo 3 flag hardening)", () => {
    assert.equal(hr.classifyHrError(null, "/tmp/hrhome"), fallback("null"));
    assert.equal(hr.classifyHrError(undefined, "/tmp/hrhome"), fallback("undefined"));
  });
});
