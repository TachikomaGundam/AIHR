/**
 * FastDraw — home-reachability & hardlink-partner checks.
 *
 * Incident (2026-09-09, wiki: dev/tools/omo-home-pollution-cadence): a
 * Cadence installer set the USER-level HOME to Documents\Cadence\SPB_Data.
 * OMO (oh-my-openagent 4.19.4, resolveHomeDir) resolves its config home as
 * `env.HOME ?? env.USERPROFILE ?? cwd` — so it silently stopped reading the
 * ~/.omo/omo.jsonc that the user (and FastDraw) edit. A second-order bug:
 * the user's hardlink mirror of that file was broken by editors' atomic
 * save (write temp + rename = new inode), leaving two diverged configs.
 *
 * This module is pure + DI (env / cwd / realHome / fs injectable) so it is
 * hermetically testable; the fs default is node:fs. It WARNS ONLY — callers
 * must never block writes on these findings.
 */
import fsSync from "node:fs"
import { homedir } from "node:os"
import path from "node:path"
import { createHash } from "node:crypto"

/** The wiki page every warning cites so operators land on the incident. */
export const HOME_REACH_WIKI = "dev/tools/omo-home-pollution-cadence"

/* ── fs seam (sync subset; node:fs satisfies it structurally) ─────── */

export interface LinkStat {
  dev: number
  ino: number
  nlink: number
}

export interface HomeReachFs {
  existsSync(p: string): boolean
  statSync(p: string): LinkStat
  readFileSync(p: string, enc: "utf8"): string
  writeFileSync(p: string, data: string): void
  mkdirSync(p: string, opts: { recursive: true }): void
}

/* ── Home resolution (OMO's exact rule — order matters, HOME wins) ── */

/** OMO 4.19.4 resolveHomeDir: `env.HOME ?? env.USERPROFILE ?? cwd`. */
export function resolveOmoHomeDir(env: NodeJS.ProcessEnv, cwd: string): string {
  return env.HOME ?? env.USERPROFILE ?? cwd
}

/* ── Warnings ─────────────────────────────────────────────────────── */

export type HomeReachWarning =
  | { kind: "home-hijack-suspect"; detail: string }
  | { kind: "config-outside-resolved-home"; detail: string }
  | { kind: "link-partner-broken"; detail: string }

export interface HomeReachReport {
  ok: boolean
  omoHome: string
  warnings: HomeReachWarning[]
}

export interface HomeReachDeps {
  env?: NodeJS.ProcessEnv
  cwd?: string
  /** The genuine user home (os.homedir() / USERPROFILE base). */
  realHome?: string
  fs?: HomeReachFs
  registryPath?: string
}

const OMO_MANAGED_FILES = ["omo.jsonc", "omo.json"] as const

function hasManagedOmoConfig(home: string, fs: HomeReachFs): boolean {
  return OMO_MANAGED_FILES.some((f) => fs.existsSync(path.join(home, ".omo", f)))
}

/** HOME-hijack + stale-config-outside-resolved-home checks (warn-only). */
export function checkHomeReachability(deps: HomeReachDeps = {}): HomeReachReport {
  const env = deps.env ?? process.env
  const cwd = deps.cwd ?? process.cwd()
  const realHome = deps.realHome ?? homedir()
  const fs = deps.fs ?? fsSync
  const omoHome = resolveOmoHomeDir(env, cwd)
  const warnings: HomeReachWarning[] = []

  if (env.HOME && env.HOME !== realHome && env.HOME !== env.USERPROFILE) {
    warnings.push({
      kind: "home-hijack-suspect",
      detail:
        `HOME="${env.HOME}" is neither the OS home ("${realHome}") nor USERPROFILE — ` +
        `OMO resolves its config as HOME ?? USERPROFILE ?? cwd, so it reads ` +
        `${path.join(omoHome, ".omo")}; an installer may have hijacked HOME ` +
        `(wiki: ${HOME_REACH_WIKI})`,
    })
  }
  if (
    omoHome !== realHome &&
    !hasManagedOmoConfig(omoHome, fs) &&
    hasManagedOmoConfig(realHome, fs)
  ) {
    warnings.push({
      kind: "config-outside-resolved-home",
      detail:
        `a managed OMO config exists under ${path.join(realHome, ".omo")} but ` +
        `${path.join(omoHome, ".omo")} has none — OMO (resolved home "${omoHome}") ` +
        `reads nothing/stale there; edits to the real-home file never take effect ` +
        `(wiki: ${HOME_REACH_WIKI})`,
    })
  }
  return { ok: warnings.length === 0, omoHome, warnings }
}

/* ── Link-partner registry (sidecar written by safeWriteFile) ─────── */

export interface LinkPartnerRecord {
  path: string
  dev: number
  ino: number
  updatedAt: string
}

interface LinkRegistryDoc {
  version: 1
  partners: LinkPartnerRecord[]
}

/** Next to the other FastDraw state (~/.config/opencode/.fastdraw.json). */
export function defaultLinkRegistryPath(realHome: string = homedir()): string {
  return path.join(realHome, ".config", "opencode", "fastdraw-links.json")
}

function isPartnerRecord(v: unknown): v is LinkPartnerRecord {
  return (
    typeof v === "object" &&
    v !== null &&
    "path" in v &&
    typeof v.path === "string" &&
    "dev" in v &&
    typeof v.dev === "number" &&
    "ino" in v &&
    typeof v.ino === "number" &&
    "updatedAt" in v &&
    typeof v.updatedAt === "string"
  )
}

export function loadLinkRegistry(
  fs: HomeReachFs = fsSync,
  registryPath: string = defaultLinkRegistryPath(),
): LinkPartnerRecord[] {
  let text: string
  try {
    text = fs.readFileSync(registryPath, "utf8")
  } catch {
    return []
  }
  try {
    const doc: unknown = JSON.parse(text)
    if (
      typeof doc !== "object" ||
      doc === null ||
      !("partners" in doc) ||
      !Array.isArray(doc.partners)
    ) {
      return []
    }
    return doc.partners.filter(isPartnerRecord)
  } catch {
    return []
  }
}

/** Record one hardlinked config path under its (dev,ino) identity.
 *  Best-effort: registry I/O must never fail the config write itself. */
export function upsertLinkPartner(
  record: LinkPartnerRecord,
  deps: { fs?: HomeReachFs; registryPath?: string } = {},
): void {
  const fs = deps.fs ?? fsSync
  const registryPath = deps.registryPath ?? defaultLinkRegistryPath()
  const partners = loadLinkRegistry(fs, registryPath).filter((r) => r.path !== record.path)
  partners.push(record)
  partners.sort((a, b) => a.path.localeCompare(b.path))
  const doc: LinkRegistryDoc = { version: 1, partners }
  try {
    fs.mkdirSync(path.dirname(registryPath), { recursive: true })
    fs.writeFileSync(registryPath, JSON.stringify(doc, null, 2))
  } catch {
    // telemetry only — swallow (the loud surface is verifyLinkIntegrity)
  }
}

function partnerBroken(detail: string): HomeReachWarning {
  return { kind: "link-partner-broken", detail: `${detail} — see wiki ${HOME_REACH_WIKI}` }
}

/** Re-check every registered path: missing file, replaced inode
 *  (editor atomic save broke the mirror) or diverged content between
 *  partners that still share one identity. */
export function verifyLinkIntegrity(deps: HomeReachDeps = {}): HomeReachWarning[] {
  const fs = deps.fs ?? fsSync
  const registryPath = deps.registryPath ?? defaultLinkRegistryPath()
  const warnings: HomeReachWarning[] = []
  const byIdentity = new Map<string, string[]>()
  for (const rec of loadLinkRegistry(fs, registryPath)) {
    let st: LinkStat | null = null
    try {
      st = fs.statSync(rec.path)
    } catch {
      // handled below via the null check
    }
    if (!st) {
      warnings.push(
        partnerBroken(`registered link partner ${rec.path} no longer exists (recorded ino ${rec.ino})`),
      )
      continue
    }
    if (st.ino !== rec.ino || st.dev !== rec.dev) {
      warnings.push(
        partnerBroken(
          `${rec.path} lost its link: inode ${rec.ino} → ${st.ino} ` +
            `(an atomic-save replace detached the mirror — partners silently diverge)`,
        ),
      )
      continue
    }
    try {
      const hash = createHash("sha256").update(fs.readFileSync(rec.path, "utf8"), "utf8").digest("hex")
      const key = `${st.dev}:${st.ino}`
      const bucket = byIdentity.get(key) ?? []
      bucket.push(rec.path, hash)
      byIdentity.set(key, bucket)
    } catch {
      // unreadable mid-check: the next scan re-stats it anyway
    }
  }
  for (const [key, flat] of byIdentity) {
    const hashes = new Set<string>()
    const paths: string[] = []
    for (let i = 0; i + 1 < flat.length; i += 2) {
      paths.push(flat[i] ?? "")
      hashes.add(flat[i + 1] ?? "")
    }
    if (hashes.size > 1) {
      warnings.push(
        partnerBroken(`partners sharing identity ${key} (${paths.join(", ")}) hold different content`),
      )
    }
  }
  return warnings
}

/* ── Combined scan + rendering ────────────────────────────────────── */

/** checkHomeReachability + verifyLinkIntegrity in one additive report. */
export function scanHomeReach(deps: HomeReachDeps = {}): HomeReachReport {
  const base = checkHomeReachability(deps)
  const warnings = [...base.warnings, ...verifyLinkIntegrity(deps)]
  return { ok: warnings.length === 0, omoHome: base.omoHome, warnings }
}

function describeWarning(w: HomeReachWarning): string {
  switch (w.kind) {
    case "home-hijack-suspect":
      return `HOME hijack suspect — ${w.detail}`
    case "config-outside-resolved-home":
      return `OMO config outside resolved home — ${w.detail}`
    case "link-partner-broken":
      return `hardlink partner broken — ${w.detail}`
    default:
      return assertNever(w)
  }
}

function assertNever(v: never): never {
  throw new Error(`unhandled HomeReachWarning: ${JSON.stringify(v)}`)
}

/** One `⚠ home-reach: ...` line per warning (server list + TUI toasts). */
export function homeReachToastLines(warnings: readonly HomeReachWarning[]): string[] {
  return warnings.map((w) => `⚠ home-reach: ${describeWarning(w)}`)
}

export function renderHomeReachWarnings(warnings: readonly HomeReachWarning[]): string {
  return homeReachToastLines(warnings).join("\n")
}
