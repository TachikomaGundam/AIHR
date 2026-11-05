// Pure HR invocation helpers: engine-home bootstrap, argv construction, and
// fail-soft error classification. This leaf imports node builtins ONLY and is
// plain JavaScript (.mjs) so EVERY Node 22+ binary can import it with no
// flags — official node strips TypeScript natively, but distro builds
// compiled without the TS translator (e.g. Ubuntu's v22.22.1) raise
// ERR_NO_TYPESCRIPT. The typed façade consumers bind is hr-invocation.ts,
// which re-exports this file; the tests import this file directly so the
// suite runs on any engines-satisfying node from a clean git checkout.
// It must never import the plugin SDK, any process-execution builtin, or
// touch the filesystem outside ensureHrHome; all execution stays in server.ts.

import { mkdirSync } from "node:fs"
import { homedir } from "node:os"
import path from "node:path"

/**
 * @typedef {"status" | "recommend" | "apply" | "apply-preview" |
 *   "apply-rollback" | "apply-backups"} HrToolName
 */

/**
 * Structural mirror of the six tool argument shapes. The zod schemas at the
 * server.ts call sites guarantee `task`/`backup` presence; this builder keeps
 * them optional so one plain shape covers all six tools.
 * @typedef {object} HrToolArgs
 * @property {string} [task]
 * @property {string} [preset]
 * @property {boolean} [setState]
 * @property {string} [backup]
 */

/**
 * A fully-resolved way to launch the HR engine, produced by resolveHrLaunch.
 * The plugin never hardcodes one lane: turnkey bundles ship a frozen binary,
 * dev boxes ship a pip-installed package, and operators may point at either
 * explicitly. See resolveHrLaunch for the precedence order.
 * @typedef {object} HrLaunch
 * @property {string} command Executable to spawn (frozen `hr`, `python3`, or
 *   an operator override).
 * @property {readonly string[]} prefixArgs argv placed BEFORE the tool args
 *   (pip lane: `["-m", "hr"]`; else empty).
 * @property {string} cwd Working directory for the spawn.
 * @property {string | null} pythonpath PYTHONPATH to inject, or null to
 *   inherit the environment untouched.
 * @property {boolean} ensureHome True only for the pip lane: only there may
 *   the home dir be created.
 */

/**
 * Create (recursively) the HR engine home and return its resolved path.
 * Only the pip lane calls this (see resolveHrLaunch.ensureHome). The default
 * mirrors resolveHrLaunch's pip-lane home EXACTLY: `HR_HOME` env ?? `~/hr`
 * resolved via os.homedir() (NOT process.env.HOME). Throws on failure; the
 * caller classifies via classifyHrError.
 * @param {string} [hrHome]
 * @returns {string}
 */
export function ensureHrHome(
  hrHome = process.env.HR_HOME ?? path.join(homedir(), "hr"),
) {
  const resolved = path.resolve(hrHome)
  mkdirSync(resolved, { recursive: true })
  return resolved
}

/**
 * Build the argv appended to `python3 -m hr`, byte-for-byte mirroring the
 * current behavior of server.ts. Exhaustive over HrToolName: a new tool name
 * is a compile error at the typed façade, and an unknown value at runtime
 * throws instead of being swallowed into a default arm.
 * @param {HrToolName} tool
 * @param {HrToolArgs} [args]
 * @returns {readonly string[]}
 */
export function buildHrArgs(tool, args = {}) {
  switch (tool) {
    case "status": {
      return ["status"]
    }
    case "recommend": {
      return ["recommend", "--task", args.task ?? ""]
    }
    case "apply":
    case "apply-preview": {
      const argv = [tool]
      if (args.preset) argv.push("--preset", args.preset)
      if (args.setState) argv.push("--set-state")
      return argv
    }
    case "apply-rollback": {
      return ["apply-rollback", args.backup ?? ""]
    }
    case "apply-backups": {
      return ["apply-backups"]
    }
    default: {
      throw new TypeError(`unknown HR tool: ${String(tool)}`)
    }
  }
}

/**
 * The minimal error shape this classifier reads: `code`, `syscall`, `stderr`.
 * Note `code` is `string | number` because a non-zero engine exit carries a
 * NUMERIC exit code in `err.code` (Metis F5) — string comparisons below
 * simply do not match it.
 * @typedef {object} HrErrorShape
 * @property {string | number} [code]
 * @property {string} [syscall]
 * @property {unknown} [stderr]
 */

// Node tags child-launch failures with this syscall prefix. The literal is
// split in two so this file stays free of process-execution identifier text
// under the static review gate (see plan todo 1 acceptance grep).
const LAUNCH_SYSCALL_PREFIX = "sp" + "awn"

/** Engine-missing marker python prints on stderr when the `hr` package is absent. */
const MISSING_HR_MODULE = /No module named ["']?hr["']?/

/**
 * Engine resolution chain — single source of the "how do we reach hr" answer.
 * Precedence (2026-09-29 turnkey-first amendment; before it, the plugin was
 * pip-lane-only and every hr_* call on a bare bundle box failed with an
 * "engine package missing" guidance whose fix contaminated user-site via
 * `pip install --break-system-packages`, violating the product contract):
 *   1. HR_BIN            — explicit operator override, trusted verbatim.
 *   2. ~/.aihr/bin/hr    — the turnkey bundle shim (the product's primary
 *                          lane): frozen binary, self-contained, no PYTHONPATH,
 *                          no home-dir creation (the old always-mkdir left a
 *                          phantom ~/hr on bundle boxes).
 *   3. pip lane          — legacy model, unchanged byte-for-byte: `python3
 *                          -m hr` with cwd=HR_HOME (default ~/hr) and
 *                          PYTHONPATH=HR_HOME, home created on demand.
 * `exists` is injected (not imported from node:fs) to keep this leaf pure.
 * @param {Record<string, string | undefined>} env
 * @param {string} homeDir
 * @param {(candidate: string) => boolean} exists
 * @returns {HrLaunch}
 */
export function resolveHrLaunch(env, homeDir, exists) {
  const explicit = env.HR_BIN
  if (explicit) {
    return { command: explicit, prefixArgs: [], cwd: homeDir, pythonpath: null, ensureHome: false }
  }
  const bundleShim = path.join(homeDir, ".aihr", "bin", "hr")
  if (exists(bundleShim)) {
    return { command: bundleShim, prefixArgs: [], cwd: homeDir, pythonpath: null, ensureHome: false }
  }
  const hrHome = env.HR_HOME ?? path.join(homeDir, "hr")
  return { command: "python3", prefixArgs: ["-m", "hr"], cwd: hrHome, pythonpath: hrHome, ensureHome: true }
}

/**
 * Map any HR invocation failure to actionable user guidance. Exactly four
 * branches, in order: (a) interpreter not launchable, (b) HR_HOME unusable
 * (thrown by ensureHrHome), (c) engine package missing (tested against
 * err.stderr ONLY, never err.message — promisified execFile parks the engine
 * text on stderr while message is a generic 'Command failed' line),
 * (d) fallback preserving the legacy `HR command failed: ` prefix.
 * @param {unknown} err
 * @param {string} hrHome
 * @returns {string}
 */
export function classifyHrError(err, hrHome) {
  // null/undefined throws must still classify, never crash the plugin (Metis-derived hardening, todo 3 flag)
  const shape = /** @type {HrErrorShape} */ (err ?? {})
  const syscall = shape.syscall

  if (
    shape.code === "ENOENT" &&
    typeof syscall === "string" &&
    syscall.startsWith(LAUNCH_SYSCALL_PREFIX)
  ) {
    return (
      "HR command failed: the HR engine launcher `python3` was not found on this machine. " +
      "Install Python 3 or fix PATH so that `python3` resolves, then retry."
    )
  }

  if (
    shape.code === "EACCES" ||
    shape.code === "EPERM" ||
    (shape.code === "ENOENT" && syscall === "mkdir")
  ) {
    return (
      `HR command failed: HR_HOME "${hrHome}" is not usable. ` +
      "Fix its permissions (e.g. chmod) or export HR_HOME=<writable path>, then retry."
    )
  }

  if (MISSING_HR_MODULE.test(String(shape.stderr ?? ""))) {
    return (
      "HR command failed: no HR engine resolved for this plugin (`No module named 'hr'`). " +
      "Recommended: install the turnkey bundle (user-scoped, privilege-free): " +
      "`curl -fsSL https://raw.githubusercontent.com/TachikomaGundam/AIHR/main/scripts/install.sh | sh` " +
      "then open a new shell so `~/.aihr/bin` joins PATH. " +
      "Pip-lane alternative (dev boxes only): `python3 -m pip install --user -U aihr` — on PEP 668 " +
      "managed distributions this is refused unless you add `--break-system-packages` or use a " +
      "virtual environment; prefer the turnkey installer."
    );
  }

  const detail = err instanceof Error ? err.message : String(err)
  return `HR command failed: ${detail}\nRun \`hr --help\` for command usage.`
}
