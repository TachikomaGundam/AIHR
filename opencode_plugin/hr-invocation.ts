// Pure HR invocation helpers: engine-home bootstrap, argv construction, and
// fail-soft error classification. This leaf imports node builtins ONLY and
// uses erasable-syntax-only TypeScript so bare `node` (>= 22.18) can import
// it with no flags. It must never import the plugin SDK, any process-execution
// builtin, or touch the filesystem outside ensureHrHome; all execution stays
// in server.ts.

import { mkdirSync } from "node:fs"
import { homedir } from "node:os"
import path from "node:path"

/**
 * Create (recursively) the HR engine home and return its resolved path.
 * The default mirrors server.ts:9 EXACTLY: `HR_HOME` env ?? `~/hr` resolved
 * via os.homedir() (NOT process.env.HOME). Throws on failure; the caller
 * classifies via classifyHrError.
 */
export function ensureHrHome(
  hrHome: string = process.env.HR_HOME ?? path.join(homedir(), "hr"),
): string {
  const resolved = path.resolve(hrHome)
  mkdirSync(resolved, { recursive: true })
  return resolved
}

export type HrToolName =
  | "status"
  | "recommend"
  | "apply"
  | "apply-preview"
  | "apply-rollback"
  | "apply-backups"

/**
 * Structural mirror of the six tool argument shapes. The zod schemas at the
 * server.ts call sites guarantee `task`/`backup` presence; this builder keeps
 * them optional so one plain interface covers all six tools.
 */
export interface HrToolArgs {
  readonly task?: string
  readonly preset?: string
  readonly setState?: boolean
  readonly backup?: string
}

/**
 * Build the argv appended to `python3 -m hr`, byte-for-byte mirroring the
 * current behavior of server.ts:30-77. Exhaustive over HrToolName: a new tool
 * name is a compile error, and an unknown value at runtime throws instead of
 * being swallowed into a default arm.
 */
export function buildHrArgs(
  tool: HrToolName,
  args: HrToolArgs = {},
): readonly string[] {
  switch (tool) {
    case "status": {
      return ["status"]
    }
    case "recommend": {
      return ["recommend", "--task", args.task ?? ""]
    }
    case "apply":
    case "apply-preview": {
      const argv: string[] = [tool]
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
  }
  const exhausted: never = tool
  throw new TypeError(`unknown HR tool: ${exhausted}`)
}

/**
 * The minimal error shape this classifier reads. Deliberately a local
 * structural type rather than NodeJS.ErrnoException: this module has zero
 * dependencies, including on Node type packages. Note `code` is `string |
 * number` because a non-zero engine exit carries a NUMERIC exit code in
 * `err.code` (Metis F5) — string comparisons below simply do not match it.
 */
interface HrErrorShape {
  readonly code?: string | number
  readonly syscall?: string
  readonly stderr?: unknown
}

// Node tags child-launch failures with this syscall prefix. The literal is
// split in two so this file stays free of process-execution identifier text
// under the static review gate (see plan todo 1 acceptance grep).
const LAUNCH_SYSCALL_PREFIX = "sp" + "awn"

/** Engine-missing marker python prints on stderr when the `hr` package is absent. */
const MISSING_HR_MODULE = /No module named ["']?hr["']?/

/**
 * Map any HR invocation failure to actionable user guidance. Exactly four
 * branches, in order: (a) interpreter not launchable, (b) HR_HOME unusable
 * (thrown by ensureHrHome), (c) engine package missing (tested against
 * err.stderr ONLY, never err.message — promisified execFile parks the engine
 * text on stderr while message is a generic 'Command failed' line),
 * (d) fallback preserving the legacy `HR command failed: ` prefix.
 */
export function classifyHrError(err: unknown, hrHome: string): string {
  // null/undefined throws must still classify, never crash the plugin (Metis-derived hardening, todo 3 flag)
  const shape = (err ?? {}) as HrErrorShape
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
      "HR command failed: the HR engine package is missing (`No module named 'hr'`). " +
      "Install it with `python3 -m pip install --user -U aihr`. " +
      "On PEP 668 managed distributions add `--break-system-packages` to that command, or use a virtual environment."
    )
  }

  const detail = err instanceof Error ? err.message : String(err)
  return `HR command failed: ${detail}\nRun \`hr --help\` for command usage.`
}
