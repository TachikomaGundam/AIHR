// Typed façade for the pure HR invocation helpers. The implementation lives
// in hr-invocation-core.mjs (single source of truth — see that file for the
// behavior contracts); this module only re-exports it under TypeScript types
// so server.ts and any typed consumer keep full checking. Plain .mjs was
// chosen for the implementation because distro node builds without the
// TypeScript translator (e.g. Ubuntu v22.22.1, ERR_NO_TYPESCRIPT) cannot load
// .ts even though official node >= 22.18 strips types natively — the test
// suite imports the core directly and thus runs on any engines-satisfying
// node from a clean checkout. This leaf imports node builtins ONLY; it must
// never import the plugin SDK or any process-execution builtin (all
// execution stays in server.ts).

export {
  ensureHrHome,
  buildHrArgs,
  resolveHrLaunch,
  classifyHrError,
} from "./hr-invocation-core.mjs"

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
 * A fully-resolved way to launch the HR engine, produced by resolveHrLaunch.
 * See hr-invocation-core.mjs resolveHrLaunch for the precedence order.
 */
export interface HrLaunch {
  /** Executable to spawn (frozen `hr`, `python3`, or an operator override). */
  readonly command: string
  /** argv placed BEFORE the tool args (pip lane: `["-m", "hr"]`; else empty). */
  readonly prefixArgs: readonly string[]
  /** Working directory for the spawn. */
  readonly cwd: string
  /** PYTHONPATH to inject, or null to inherit the environment untouched. */
  readonly pythonpath: string | null
  /** True only for the pip lane: only there may the home dir be created. */
  readonly ensureHome: boolean
}
