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
 * CAUTION (semantics verified against oh-my-openagent 4.19.4 dist/index.js):
 * CATEGORIES and AGENTS resolve through different fields. A category's model
 * CHAIN is canonical — `resolveCategoryExecution` (:132535) takes `models[0]`
 * as the primary and `models.slice(1)` as the fallback chain, IGNORING the
 * legacy `model` scalar whenever `models` exists; `fallback_models` is
 * deprecated there (scalar + array coexisting = drift). writeOmoModels
 * therefore canonicalizes category writes chain-preservingly: the existing
 * chain (or the `[model, ...fallback_models]` fold) keeps its tail, only the
 * primary is swapped, and the superseded scalar keys are dropped — the
 * postcondition verifies the whole chain plus the ABSENCE of those keys.
 * Agents are the mirror image: no consumer reads agent-level `.models`
 * (userModel registration :161170, call_omo_agent :128763, and
 * resolveSubagentModel :132741 all read `.model` + `.fallback_models` only),
 * so agent writes touch ONLY `.model` and never create a chain.
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
  canonicalCategoryChain,
  specModel,
  toPortablePath,
  type ModelEntrySpec,
} from "./origins.js"
import { OMO_BUILTINS } from "./roles.js"

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

/** OMO's builtin agent roles (single source of truth: roles.ts). They are
 *  fleet-defined in OMO's own config, exactly like the builtin categories. */
export const OMO_STATIC_AGENTS: readonly string[] = OMO_BUILTINS

/** On a machine without any user OMO config, builtin agent and category
 *  names would not be recognized and would be mis-routed into opencode's
 *  `cfg.agent` as PHANTOM roles (production incident 2026-09-07: a preset
 *  load on a fresh target wrote all 11 builtin bindings into
 *  opencode.jsonc). Seeding every OMO builtin as a known target makes the
 *  first bind of any of them create `[opencode].agents|categories.<name>`
 *  in a fresh omo.jsonc instead. */
function staticTargets(): Record<string, OmoTarget> {
  const out: Record<string, OmoTarget> = {}
  for (const c of OMO_STATIC_CATEGORIES) out[c] = { kind: "category", model: null, models: null }
  for (const a of OMO_STATIC_AGENTS) out[a] = { kind: "agent", model: null, models: null }
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
  /** Category `models` chain as found before FastDraw's first write (see
   *  module header); recorded so revert restores the exact pre-fastdraw
   *  chain, not just its primary model. */
  original_models?: unknown[] | null
}

/** Write-spec restoring a binding's pre-fastdraw state: the original model
 *  plus, for categories, the recorded `models` chain — written back verbatim
 *  as the canonical array, legacy scalar keys dropped. Null when no original
 *  model is recorded. */
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
  /** EFFECTIVE primary model. Category: `models[0]` (string head or object
   *  head's `.model`) — what OMO's delegate path actually binds — falling
   *  back to the legacy scalar only when no chain exists. Agent: the
   *  `.model` scalar (the only field its runtime paths read), chain head
   *  shown for display when an entry oddly carries one. */
  model: string | null
  /** Raw `models` array as written in the definition; null when absent. On
   *  agents this is DEAD WEIGHT (no consumer) — the UI flags it. */
  models: unknown[] | null
  /** Raw `model` scalar (or bare-string entry) as written, undefined when
   *  absent. On categories superseded by the chain — presence = drift the
   *  next apply cleans up. */
  legacyScalar?: string
  /** Raw `fallback_models` array as written, undefined when absent.
   *  Deprecated on categories (drift); still live config on agents. */
  legacyFallbacks?: unknown[]
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

/** Primary model of a `models` chain (string head or object head's `.model`);
 *  null when absent/unusable. */
function chainPrimary(models: readonly unknown[]): string | null {
  return entryModel(models[0])
}

/** Raw superseded keys one definition carries, for drift reporting and the
 *  category fold. Included only when actually present so targets stay
 *  deep-comparable. */
function legacyKeys(def: unknown): Pick<OmoTarget, "legacyScalar" | "legacyFallbacks"> {
  const out: Pick<OmoTarget, "legacyScalar" | "legacyFallbacks"> = {}
  const raw = typeof def === "string" ? def : asRecord(def)?.model
  if (typeof raw === "string" && raw) out.legacyScalar = raw
  const fb = asRecord(def)?.fallback_models
  if (Array.isArray(fb)) out.legacyFallbacks = fb
  return out
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
      const models = entryModelsArray(def)
      // Agent runtime paths read `.model` (+ `.fallback_models`) only — a
      // models[] here is dead weight, surfaced for display and flagged.
      const model = entryModel(def) ?? (models ? chainPrimary(models) : null)
      out[name] = { kind: "agent", model, models, ...legacyKeys(def) }
    }
  }
  const categories = asRecord(cfg.categories)
  if (categories) {
    for (const [name, def] of Object.entries(categories)) {
      if (name in out) continue
      const models = entryModelsArray(def)
      out[name] = {
        kind: "category",
        // `models[0]` is the effective primary whenever the chain exists;
        // the scalar only answers alone (see module header).
        model: (models ? chainPrimary(models) : null) ?? entryModel(def),
        models,
        ...legacyKeys(def),
      }
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
      targets: staticTargets(),
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
  // Builtins stay known even when the user file omits them; file entries
  // always override the seed (empty model means "unbound" in the seed).
  const targets: Record<string, OmoTarget> = staticTargets()
  const fromFile: Record<string, OmoTarget> = {}
  if (root) {
    // Top-level base keys first, then the [opencode] host block wins.
    foldSection(fromFile, root)
    const block = asRecord(root["[opencode]"])
    if (block) foldSection(fromFile, { agents: block.agents, categories: block.categories })
  }
  Object.assign(targets, fromFile)
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

/** Terse drift flag shared by fastdraw_list and the TUI. A CATEGORY still
 *  carrying the superseded `model` scalar or `fallback_models` keys (next to
 *  or instead of the canonical chain) is legacy drift — the next apply
 *  cleans it. An AGENT carrying a `models` array holds dead weight: no
 *  agent-path consumer reads it. */
export function omoDriftNote(t: OmoTarget | undefined): string {
  if (!t) return ""
  if (t.kind === "category")
    return t.legacyScalar !== undefined || t.legacyFallbacks !== undefined
      ? "  ⚠ legacy scalar/fallback_models — canonicalized to the chain on next apply"
      : ""
  return t.models !== null ? "  ⚠ .models array on an agent — ignored at runtime" : ""
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
 *  Category updates CANONICALIZE the model chain: the existing `models`
 *  array (or the legacy `[model, ...fallback_models]` fold) keeps its tail
 *  and only the primary is replaced; the superseded `model` /
 *  `fallback_models` keys are dropped. A spec carrying an explicit `models`
 *  array is written verbatim (revert path). Agent updates touch ONLY
 *  `.model`. Backs the file up first; on ANY failure the filesystem is
 *  untouched. */
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
    const categoryChains: Record<string, unknown[]> = {}
    if (Object.keys(categoryUpdates).length) {
      const oldCats = asRecord(
        asRecord(asRecord(JSON.parse(stripJsonComments(text)))?.["[opencode]"])?.categories,
      )
      for (const [name, spec] of Object.entries(categoryUpdates))
        categoryChains[name] = canonicalCategoryChain(oldCats?.[name], spec)
      text = setNestedModelsJsonc(text, CATEGORY_CHAIN, categoryUpdates, {
        categoryChain: true,
      })
    }
    verifyWritten(text, { agents: agentUpdates, categories: categoryChains })
    await writeFileAtomic(file, text)
    return { file, backup, written: true }
  } catch (e) {
    if (backup !== null) {
      await fs.rm(backup, { force: true }).catch(() => undefined)
    }
    return { file, backup: null, written: false, error: e instanceof Error ? e.message : String(e) }
  }
}

/** Postcondition for every routed update. AGENTS: `.model` reads back the
 *  requested string — the only field the agent runtime paths consume.
 *  CATEGORIES: the entry must hold EXACTLY the expected canonical chain in
 *  `models` and must NOT carry the superseded `model` / `fallback_models`
 *  keys — a residual scalar is precisely the drift (two sources of truth
 *  across merge layers) this writer exists to prevent. Exported for tests.
 *  Throws otherwise; writeOmoModels then leaves the filesystem untouched. */
export function verifyWritten(
  text: string,
  sections: { agents: Record<string, ModelEntrySpec>; categories: Record<string, unknown[]> },
): void {
  const root = asRecord(JSON.parse(stripJsonComments(text)))
  const block = asRecord(root?.["[opencode]"])
  for (const [name, spec] of Object.entries(sections.agents)) {
    const model = specModel(spec)
    const entry = asRecord(block?.agents)?.[name]
    if (entry === undefined || entryModel(entry) !== model) {
      throw new Error(`postcondition failed: [opencode].agents.${name}.model != ${model}`)
    }
  }
  for (const [name, chain] of Object.entries(sections.categories)) {
    const e = asRecord(asRecord(block?.categories)?.[name])
    if (!e || "model" in e || "fallback_models" in e) {
      throw new Error(
        `postcondition failed: [opencode].categories.${name} must not keep legacy model/fallback_models keys`,
      )
    }
    if (JSON.stringify(e.models) !== JSON.stringify(chain)) {
      throw new Error(
        `postcondition failed: [opencode].categories.${name}.models != ${JSON.stringify(chain)} (holds ${JSON.stringify(e.models)})`,
      )
    }
  }
}
