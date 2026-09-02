/**
 * FastDraw — OMO config (~/.omo/omo.jsonc | omo.json) binding I/O.
 *
 * OMO roles and task categories are defined in OMO's OWN config file, not in
 * opencode's `agent` section: `[opencode].agents.<role>.model` and
 * `[opencode].categories.<cat].model`. OMO's resolution chain reads the
 * top-level `agents`/`categories` keys first, then the `[opencode]` host
 * block on top, so the host block is the highest-precedence, correct write
 * target for opencode-fleet bindings (profiles aside). Writing these names
 * into opencode `cfg.agent` instead just creates PHANTOM ROLES.
 *
 * CAUTION (production incident 2026-09): for CATEGORIES the `model` scalar is
 * NOT the whole truth — when the definition also carries a `models` array,
 * OMO's delegate-task resolution treats `models[0]` as the primary and
 * `models.slice(1)` as the fallback chain, ignoring `model` entirely. Binding
 * only `model` therefore silently no-ops. writeOmoModels normalizes the array
 * (`models: [model]`) on every category write and the postcondition verifies
 * the DOMINANT value, not just the scalar.
 *
 * File detection mirrors OMO's own order (omo.jsonc wins, omo.json fallback);
 * read and write always go through the SAME resolved file. Project-level
 * `<dir>/.omo/omo.json{c}` ancestors shadow the user config in OMO's merge —
 * FastDraw never writes those, but reports them so callers can warn that a
 * binding may not take effect.
 */
import fs from "node:fs/promises"
import { homedir } from "node:os"
import path from "node:path"
import {
  backupIfExists,
  writeFileAtomic,
  stripJsonComments,
  setNestedModelsJsonc,
  specModel,
  toPortablePath,
  type ModelEntrySpec,
} from "./origins.js"

/** Static fallback when the OMO config file is absent (OMO 4.19 builtins). */
export const OMO_STATIC_CATEGORIES = [
  "visual-engineering",
  "artistry",
  "ultrabrain",
  "deep",
  "quick",
  "unspecified-low",
  "unspecified-high",
  "writing",
] as const

/** On a machine without any user OMO config, category names would not be
 *  recognized and would be mis-routed into opencode's `cfg.agent` as PHANTOM
 *  roles. Seeding the OMO builtin categories as known targets makes the first
 *  category bind create `[opencode].categories.<cat>` in a fresh omo.jsonc. */
function staticCategoryTargets(): Record<string, OmoTarget> {
  const out: Record<string, OmoTarget> = {}
  for (const c of OMO_STATIC_CATEGORIES) out[c] = { kind: "category", model: null, models: null }
  return out
}

const AGENT_CHAIN = ["[opencode]", "agents"]
const CATEGORY_CHAIN = ["[opencode]", "categories"]

export type { ModelEntrySpec } from "./origins.js"

export type OmoTargetKind = "agent" | "category"

/** One FastDraw-recorded binding into the OMO config. */
export interface OmoBinding {
  model: string
  /** Model the OMO config held before FastDraw first touched this name;
   *  null = the name had no model there (or none was recorded). */
  original: string | null
  /** Category `models` array as found before FastDraw's first write (the
   *  dominant field — see module header); recorded so revert restores the
   *  exact pre-fastdraw entry, not just its primary model. */
  original_models?: unknown[] | null
}

/** Write-spec restoring a binding's pre-fastdraw state: the original model
 *  plus, for categories, the recorded dominant `models` array so the exact
 *  entry shape comes back. Null when no original model is recorded. */
export function omoRevertSpec(
  kind: OmoTargetKind | null,
  rec: OmoBinding,
): ModelEntrySpec | null {
  if (!rec.original) return null
  if (kind === "category" && rec.original_models)
    return { model: rec.original, models: rec.original_models }
  return rec.original
}

export interface OmoTarget {
  kind: OmoTargetKind
  /** `model` scalar as written in the config. For categories this is NOT
   *  authoritative when `models` is non-null — OMO's delegate-task path uses
   *  `models[0]` as the effective primary. */
  model: string | null
  /** Category `models` array when the definition carries one; null
   *  otherwise. Non-null = this entry's `models[0]` dominates `model`. */
  models: unknown[] | null
}

export interface OmoConfig {
  /** User config file FastDraw reads AND writes; null when neither jsonc nor
   *  json exists yet (first write creates omo.jsonc). */
  file: string | null
  exists: boolean
  /** false = file exists but could not be parsed — never write blindly. */
  parseable: boolean
  /** name → target. On an agent/category name collision the agent wins. */
  targets: Record<string, OmoTarget>
  /** Project .omo configs between cwd and its git root that shadow the user
   *  file (OMO merges them on top). */
  shadowFiles: string[]
}

async function tryExists(p: string): Promise<boolean> {
  try {
    await fs.access(p)
    return true
  } catch {
    return false
  }
}

function homeDir(env: NodeJS.ProcessEnv): string {
  return env.HOME || env.USERPROFILE || homedir()
}

function omoDir(env: NodeJS.ProcessEnv): string {
  return path.join(homeDir(env), ".omo")
}

/** Resolve the user OMO config with OMO's own detection order: omo.jsonc
 *  wins over omo.json; null when neither exists. */
export async function resolveOmoUserConfigFile(env = process.env): Promise<string | null> {
  const dir = omoDir(env)
  for (const f of ["omo.jsonc", "omo.json"] as const) {
    const p = path.join(dir, f)
    if (await tryExists(p)) return p
  }
  return null
}

function asRecord(v: unknown): Record<string, unknown> | null {
  return v !== null && typeof v === "object" && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : null
}

/** Model of one agents/categories definition entry (object with `model`, or
 *  legacy bare string); null when absent/unusable. */
function entryModel(v: unknown): string | null {
  if (typeof v === "string") return v.includes("/") ? v : null
  const r = asRecord(v)
  const m = r?.model
  return typeof m === "string" && m.includes("/") ? m : null
}

function entryModelsArray(v: unknown): unknown[] | null {
  const m = asRecord(v)?.models
  return Array.isArray(m) ? m : null
}

/** Fold one layer's agents+categories definitions into `out` (later calls
 *  win — callers pass base keys first, then the [opencode] host block). */
function foldSection(
  out: Record<string, OmoTarget>,
  cfg: Record<string, unknown>,
): void {
  const agents = asRecord(cfg.agents)
  if (agents) {
    for (const [name, def] of Object.entries(agents)) {
      out[name] = { kind: "agent", model: entryModel(def), models: entryModelsArray(def) }
    }
  }
  const categories = asRecord(cfg.categories)
  if (categories) {
    for (const [name, def] of Object.entries(categories)) {
      if (!(name in out))
        out[name] = { kind: "category", model: entryModel(def), models: entryModelsArray(def) }
    }
  }
}

/** Walk cwd up to the git root collecting project .omo configs that shadow
 *  the user file. `findProjectRoot`-style walk, jsonc before json per dir. */
async function findShadowFiles(cwd: string, userFile: string | null): Promise<string[]> {
  const out: string[] = []
  let root = path.resolve(cwd)
  for (;;) {
    if (await tryExists(path.join(root, ".git"))) break
    const parent = path.dirname(root)
    if (parent === root) {
      root = path.resolve(cwd)
      break
    }
    root = parent
  }
  for (let d = path.resolve(cwd); ; d = path.dirname(d)) {
    for (const f of ["omo.jsonc", "omo.json"] as const) {
      const p = path.join(d, ".omo", f)
      if (p !== userFile && (await tryExists(p))) out.push(p)
    }
    if (d === root || path.dirname(d) === d) break
  }
  return out
}

export async function readOmoConfig(
  env = process.env,
  cwd = process.cwd(),
): Promise<OmoConfig> {
  const file = await resolveOmoUserConfigFile(env)
  const empty: OmoConfig = {
    file,
    exists: file !== null,
    parseable: true,
    targets: {},
    shadowFiles: [],
  }
  if (!file) {
    return {
      ...empty,
      targets: staticCategoryTargets(),
      shadowFiles: await findShadowFiles(cwd, file),
    }
  }
  let cfg: unknown
  try {
    let text = await fs.readFile(file, "utf-8")
    if (text.charCodeAt(0) === 0xfeff) text = text.slice(1)
    cfg = JSON.parse(stripJsonComments(text))
  } catch {
    return { ...empty, parseable: false }
  }
  const root = asRecord(cfg)
  const targets: Record<string, OmoTarget> = {}
  if (root) {
    // Top-level base keys first, then the [opencode] host block wins.
    foldSection(targets, root)
    const block = asRecord(root["[opencode]"])
    if (block) foldSection(targets, { agents: block.agents, categories: block.categories })
  }
  return {
    file,
    exists: true,
    parseable: root !== null,
    targets,
    shadowFiles: await findShadowFiles(cwd, file),
  }
}

export function isOmoName(cfg: OmoConfig, name: string): boolean {
  return name in cfg.targets
}

export function omoTargetKind(cfg: OmoConfig, name: string): OmoTargetKind | null {
  return cfg.targets[name]?.kind ?? null
}

/** Portable form of the OMO file for preset origins (`${HOME}/.omo/...`). */
export function omoPortableFile(cfg: OmoConfig, home: string, configDir: string): string | null {
  return cfg.file ? toPortablePath(cfg.file, { home, configDir }) : null
}

export interface OmoWriteResult {
  /** File written (or intended, on failure). */
  file: string
  /** Backup path of the pre-write file; null when none/failed-clean. */
  backup: string | null
  written: boolean
  error?: string
}

/** Apply role/category model updates to the USER OMO config: names known as
 *  categories land in `[opencode].categories`, everything else in
 *  `[opencode].agents` (unknown names = builtin-role overrides there).
 *  Category updates ALSO normalize the dominant `models` array to
 *  `[model]` (or write back a spec-provided array verbatim) so the binding
 *  can never be shadowed by a legacy list — see module header. Backs the
 *  file up first; on ANY failure the filesystem is untouched. */
export async function writeOmoModels(
  updates: Record<string, ModelEntrySpec>,
  env = process.env,
  cwd = process.cwd(),
): Promise<OmoWriteResult> {
  const cfg = await readOmoConfig(env, cwd)
  const file = cfg.file ?? path.join(omoDir(env), "omo.jsonc")
  if (Object.keys(updates).length === 0) {
    return { file, backup: null, written: false }
  }
  if (cfg.exists && !cfg.parseable) {
    return {
      file,
      backup: null,
      written: false,
      error: `cannot edit unparseable OMO config: ${file}`,
    }
  }
  const agentUpdates: Record<string, ModelEntrySpec> = {}
  const categoryUpdates: Record<string, ModelEntrySpec> = {}
  for (const [name, spec] of Object.entries(updates)) {
    if (cfg.targets[name]?.kind === "category") categoryUpdates[name] = spec
    else agentUpdates[name] = spec
  }

  let backup: string | null = null
  try {
    let text = cfg.exists ? await fs.readFile(file, "utf-8") : "{}\n"
    if (cfg.exists) backup = await backupIfExists(file)
    if (Object.keys(agentUpdates).length) {
      text = setNestedModelsJsonc(text, AGENT_CHAIN, agentUpdates)
    }
    if (Object.keys(categoryUpdates).length) {
      text = setNestedModelsJsonc(text, CATEGORY_CHAIN, categoryUpdates, {
        normalizeModelsArray: true,
      })
    }
    verifyWritten(text, { agents: agentUpdates, categories: categoryUpdates })
    await writeFileAtomic(file, text)
    return { file, backup, written: true }
  } catch (e) {
    if (backup !== null) {
      await fs.rm(backup, { force: true }).catch(() => undefined)
    }
    return { file, backup: null, written: false, error: e instanceof Error ? e.message : String(e) }
  }
}

/** Postcondition: every updated name reads back with the requested model in
 *  the [opencode] section it was routed to. For categories the DOMINANT
 *  `models` array is verified too: when the entry carries one it must equal
 *  the write's effective chain (explicit spec array, else `[model]`) — a
 *  matching `model` scalar with a stale array is EXACTLY the silent-no-op
 *  this guards against. Throws otherwise. */
function verifyWritten(
  text: string,
  sections: { agents: Record<string, ModelEntrySpec>; categories: Record<string, ModelEntrySpec> },
): void {
  const root = asRecord(JSON.parse(stripJsonComments(text)))
  const block = asRecord(root?.["[opencode]"])
  for (const key of ["agents", "categories"] as const) {
    const updates = sections[key]
    if (!Object.keys(updates).length) continue
    const section = asRecord(block?.[key])
    for (const [name, spec] of Object.entries(updates)) {
      const model = specModel(spec)
      if (!section || !(name in section) || entryModel(section[name]) !== model) {
        throw new Error(`postcondition failed: [opencode].${key}.${name}.model != ${model}`)
      }
      if (key !== "categories") continue
      const actual = entryModelsArray(section[name])
      if (actual === null) continue
      const expected = typeof spec === "string" ? [model] : (spec.models ?? [model])
      if (JSON.stringify(actual) !== JSON.stringify(expected)) {
        throw new Error(
          `postcondition failed: [opencode].categories.${name}.models dominates model=${model} but holds ${JSON.stringify(actual)}`,
        )
      }
    }
  }
}
