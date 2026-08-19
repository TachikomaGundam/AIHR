/**
 * FastDraw Server Plugin — config hook + assignment tools + preset tools
 *
 * Loaded by opencode's plugin resolver via exports: "./server" → this file.
 * Validator sees only { id, server } → passes J="server" check.
 */
import type { Plugin } from "@opencode-ai/plugin"
import { tool } from "@opencode-ai/plugin"
import fs from "node:fs/promises"
import { homedir } from "node:os"
import path from "node:path"
import { OMO_BUILTINS, OVERRIDABLE_LIST } from "./roles.js"
import {
  SCHEMA_VERSION,
  toPortablePath,
  harvest,
  formatConflictReport,
  splitBindings,
  bindingsToFlat,
  sectionToBindings,
  planRestore,
  restoreWrite,
  type RestoreMode,
  type Binding,
  type HarvestCtx,
  type HarvestEnv,
} from "./origins.js"

const CONFIG_DIR = path.join(homedir(), ".config", "opencode")
const STATE_FILE = path.join(CONFIG_DIR, ".fastdraw.json")
const PRESETS_FILE = path.join(CONFIG_DIR, "fastdraw-presets.json")
const CUSTOM_AGENTS_DIR = path.join(CONFIG_DIR, "agents")

/* ── State I/O ────────────────────────────────────────────────────── */

interface Assignments {
  agents: Record<string, string>
}

async function loadState(): Promise<Assignments> {
  try {
    return JSON.parse(await fs.readFile(STATE_FILE, "utf-8")) as Assignments
  } catch {
    return { agents: {} }
  }
}

async function saveState(a: Assignments): Promise<void> {
  await fs.mkdir(CONFIG_DIR, { recursive: true })
  await fs.writeFile(STATE_FILE, JSON.stringify(a, null, 2))
}

/* ── Preset I/O ───────────────────────────────────────────────────── */

/** Preset bindings live in two sections — standard roles ("omo") and custom
 *  roles ("custom"). v2 entries are `{model, origin}`; legacy flat presets
 *  (`agents`) and string-valued sections normalize into v2 entries. */
interface Preset {
  schemaVersion?: number
  description?: string
  createdAt?: string
  omo: Record<string, Binding>
  custom: Record<string, Binding>
}

interface PresetStore {
  presets: Record<string, Preset>
}

function normalizePreset(p: unknown): Preset {
  const obj = (p ?? {}) as Record<string, unknown>
  const description = typeof obj.description === "string" ? obj.description : undefined
  const createdAt = typeof obj.createdAt === "string" ? obj.createdAt : undefined
  const schemaVersion = obj.schemaVersion === SCHEMA_VERSION ? SCHEMA_VERSION : undefined
  let omo: Record<string, Binding> = {}
  let custom: Record<string, Binding> = {}
  if (obj.omo && typeof obj.omo === "object" && !Array.isArray(obj.omo)) {
    omo = sectionToBindings(obj.omo as Record<string, unknown>)
  }
  if (obj.custom && typeof obj.custom === "object" && !Array.isArray(obj.custom)) {
    custom = sectionToBindings(obj.custom as Record<string, unknown>)
  }
  if (
    !Object.keys(omo).length &&
    !Object.keys(custom).length &&
    obj.agents &&
    typeof obj.agents === "object" &&
    !Array.isArray(obj.agents)
  ) {
    const sections = splitBindings(sectionToBindings(obj.agents as Record<string, unknown>))
    omo = sections.omo
    custom = sections.custom
  }
  return { schemaVersion, description, createdAt, omo, custom }
}

function presetAgents(p: Preset): Record<string, string> {
  return { ...bindingsToFlat(p.omo), ...bindingsToFlat(p.custom) }
}

async function loadPresets(): Promise<PresetStore> {
  try {
    const raw = JSON.parse(await fs.readFile(PRESETS_FILE, "utf-8"))
    if (raw && typeof raw === "object" && raw.presets && typeof raw.presets === "object") {
      const presets: Record<string, Preset> = {}
      for (const [name, p] of Object.entries(raw.presets as Record<string, unknown>)) {
        presets[name] = normalizePreset(p)
      }
      return { presets }
    }
    return { presets: {} }
  } catch {
    return { presets: {} }
  }
}

async function savePresets(store: PresetStore): Promise<void> {
  await fs.mkdir(CONFIG_DIR, { recursive: true })
  await fs.writeFile(PRESETS_FILE, JSON.stringify(store, null, 2))
}

/** A valid model binding map: every value is a "provider/model" string. */
function isModelMap(v: unknown): v is Record<string, string> {
  if (!v || typeof v !== "object" || Array.isArray(v)) return false
  const entries = Object.entries(v as Record<string, unknown>)
  return (
    entries.length > 0 &&
    entries.every(
      ([k, x]) => k.length > 0 && typeof x === "string" && x.includes("/"),
    )
  )
}

/** Strict entry parse: section values are legacy `"provider/model"` strings
 *  or v2 `{model, origin?}` objects; any malformed entry rejects the preset. */
function parsePresetEntry(p: unknown, label: string): Preset {
  const obj = (p ?? {}) as Record<string, unknown>
  const description = typeof obj.description === "string" ? obj.description : undefined
  const createdAt = typeof obj.createdAt === "string" ? obj.createdAt : undefined
  const schemaVersion = obj.schemaVersion === SCHEMA_VERSION ? SCHEMA_VERSION : undefined
  const hasOmo = "omo" in obj
  const hasCustom = "custom" in obj
  if (hasOmo || hasCustom) {
    const section = (key: string): Record<string, Binding> => {
      const s = obj[key]
      if (s === undefined) return {}
      if (s === null || typeof s !== "object" || Array.isArray(s)) {
        throw new Error(`"${key}" section is not a valid model map`)
      }
      const map = sectionToBindings(s as Record<string, unknown>)
      if (Object.keys(map).length !== Object.keys(s as object).length) {
        throw new Error(`"${key}" section is not a valid model map`)
      }
      return map
    }
    const omo = section("omo")
    const custom = section("custom")
    if (!Object.keys(omo).length && !Object.keys(custom).length) {
      throw new Error(`preset "${label}" has no bindings (both sections empty)`)
    }
    return { schemaVersion, description, createdAt, omo, custom }
  }
  if (obj.agents && typeof obj.agents === "object" && !Array.isArray(obj.agents)) {
    const flat = sectionToBindings(obj.agents as Record<string, unknown>)
    if (Object.keys(flat).length !== Object.keys(obj.agents as object).length) {
      throw new Error(`"agents" section is not a valid model map`)
    }
    if (!Object.keys(flat).length) {
      throw new Error(`preset "${label}" has no valid "omo"/"custom" sections or legacy "agents" map`)
    }
    const sections = splitBindings(flat)
    return { schemaVersion, description, createdAt, ...sections }
  }
  throw new Error(`preset "${label}" has no valid "omo"/"custom" sections or legacy "agents" map`)
}

/** Parse an import file: single exported preset, or a full presets store. */
function parseImport(raw: unknown):
  | { kind: "single"; name?: string; description?: string; omo: Record<string, Binding>; custom: Record<string, Binding> }
  | { kind: "bulk"; presets: Record<string, Preset> } {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error("not a JSON object")
  }
  const obj = raw as Record<string, unknown>
  // Bulk: { presets: { name: { omo, custom } } } or legacy flat {agents}
  if (obj.presets && typeof obj.presets === "object" && !Array.isArray(obj.presets)) {
    const out: Record<string, Preset> = {}
    for (const [name, p] of Object.entries(obj.presets as Record<string, unknown>)) {
      out[name] = parsePresetEntry(p, name)
    }
    if (!Object.keys(out).length) throw new Error("presets map is empty")
    return { kind: "bulk", presets: out }
  }
  // Single: { fastdraw: 1, name?, description?, omo?, custom? } or legacy
  // { name?, description?, agents }
  try {
    const p = parsePresetEntry(obj, "file")
    return {
      kind: "single",
      name: typeof obj.name === "string" ? obj.name : undefined,
      description: p.description,
      omo: p.omo,
      custom: p.custom,
    }
  } catch (e) {
    throw new Error(e instanceof Error ? e.message : String(e))
  }
}

/* ── Agent Discovery ──────────────────────────────────────────────── */

async function readCustomAgentNames(): Promise<string[]> {
  try {
    const entries = await fs.readdir(CUSTOM_AGENTS_DIR, { withFileTypes: true })
    return entries
      .filter((e) => e.isFile() && e.name.endsWith(".md"))
      .map((e) => e.name.replace(/\.md$/, ""))
  } catch {
    return []
  }
}

type AgentGroups = { omo: string[]; overrideable: string[]; custom: string[] }

function categorize(all: string[]): AgentGroups {
  const omo = new Set(OMO_BUILTINS)
  const ovr = new Set(OVERRIDABLE_LIST)
  const out: AgentGroups = { omo: [], overrideable: [], custom: [] }
  const seen = new Set<string>()
  for (const n of all) {
    if (seen.has(n)) continue
    seen.add(n)
    if (omo.has(n)) out.omo.push(n)
    else if (ovr.has(n)) out.overrideable.push(n)
    else out.custom.push(n)
  }
  return out
}

/* ── Config Mutation ──────────────────────────────────────────────── */

type Cfg = Record<string, unknown>
type AgentCfg = Record<string, Record<string, unknown>>

function applyAssignments(cfg: Cfg, agents: Record<string, string>): void {
  const agentCfg = ((cfg as any).agent ?? {}) as AgentCfg
  for (const [name, model] of Object.entries(agents)) {
    if (!agentCfg[name]) agentCfg[name] = {}
    agentCfg[name].model = model
  }
}

/* ── Display ──────────────────────────────────────────────────────── */

function formatList(
  groups: AgentGroups,
  agents: Record<string, string>,
  cfgAgent: AgentCfg,
): string {
  const line = (n: string): string => {
    const override = agents[n]
    const def = cfgAgent[n]?.model
    if (override) return `  ${n}: ${override}  [overridden]`
    if (def) return `  ${n}: ${def}`
    return `  ${n}: [not set]`
  }
  const blocks: string[] = []
  if (groups.omo.length)
    blocks.push(`OMO Roles:\n${groups.omo.sort().map(line).join("\n")}`)
  if (groups.overrideable.length)
    blocks.push(`Overrideable:\n${groups.overrideable.sort().map(line).join("\n")}`)
  if (groups.custom.length)
    blocks.push(`Custom:\n${groups.custom.sort().map(line).join("\n")}`)
  return blocks.join("\n\n")
}

function presetPreview(agents: Record<string, string>): string {
  const entries = Object.entries(agents).sort(([a], [b]) => a.localeCompare(b))
  const w = Math.max(4, ...entries.map(([n]) => n.length))
  return entries.map(([n, m]) => `  ${n.padEnd(w)}  →  ${m}`).join("\n")
}

function expandPath(p: string): string {
  if (p === "~") return homedir()
  if (p.startsWith("~/")) return path.join(homedir(), p.slice(2))
  return path.resolve(process.cwd(), p)
}

/* ── Plugin ───────────────────────────────────────────────────────── */

async function fastdrawServer() {
  const state = await loadState()
  const presets = await loadPresets()
  const customNames = await readCustomAgentNames()

  let configRef: Cfg | null = null
  /** Original cfg.agent[name].model values captured before our first mutation,
   *  so preset hot-swaps can revert agents that are no longer overridden. */
  const originals = new Map<string, string | undefined>()

  function snapshotOriginals(cfg: Cfg, names: Iterable<string>): void {
    const agentCfg = ((cfg as any).agent ?? {}) as AgentCfg
    for (const name of names) {
      if (!originals.has(name)) {
        originals.set(name, agentCfg[name]?.model as string | undefined)
      }
    }
  }

  /** Hot-swap: revert overrides absent from the new set, then apply the new set. */
  function hotSwap(cfg: Cfg, nextAgents: Record<string, string>): void {
    const agentCfg = ((cfg as any).agent ?? {}) as AgentCfg
    for (const name of Object.keys(state.agents)) {
      if (name in nextAgents) continue
      const orig = originals.get(name)
      if (orig === undefined) {
        if (agentCfg[name]) delete agentCfg[name].model
      } else if (agentCfg[name]) {
        agentCfg[name].model = orig
      }
    }
    applyAssignments(cfg, nextAgents)
  }

  return {
    async config(cfg: any) {
      configRef = cfg as Cfg
      snapshotOriginals(configRef, Object.keys(state.agents))
      applyAssignments(configRef, state.agents)
    },

    tool: {
      fastdraw_assign: tool({
        description: "FastDraw: assign a model to an agent (effective immediately)",
        args: {
          agent: tool.schema.string().describe("Agent name (e.g. oracle, my-custom-agent)"),
          model: tool.schema
            .string()
            .describe("Model ID with provider prefix (e.g. provider/model)"),
        },
        async execute(args) {
          if (configRef) snapshotOriginals(configRef, [args.agent])
          state.agents[args.agent] = args.model
          await saveState(state)

          if (configRef) applyAssignments(configRef, { [args.agent]: args.model })

          const cfgAgent = ((configRef as any)?.agent ?? {}) as AgentCfg
          const all = [
            ...new Set([
              ...OMO_BUILTINS,
              ...OVERRIDABLE_LIST,
              ...customNames,
              ...Object.keys(state.agents),
            ]),
          ]
          const groups = categorize(all.filter((n) => cfgAgent[n] || state.agents[n]))
          return `**FastDraw**: ${args.agent} → ${args.model}\n\n${formatList(groups, state.agents, cfgAgent)}`
        },
      }),

      fastdraw_remove: tool({
        description: "FastDraw: remove model override for an agent",
        args: {
          agent: tool.schema.string().describe("Agent name"),
        },
        async execute(args) {
          if (args.agent in state.agents) {
            delete state.agents[args.agent]
            await saveState(state)
            if (configRef) {
              const agentCfg = ((configRef as any).agent ?? {}) as AgentCfg
              const orig = originals.get(args.agent)
              if (orig === undefined) {
                if (agentCfg[args.agent]) delete agentCfg[args.agent].model
              } else if (agentCfg[args.agent]) {
                agentCfg[args.agent].model = orig
              }
            }
            return `**FastDraw**: removed override for ${args.agent} (reverted to default).`
          }
          return `**FastDraw**: no override for ${args.agent}.`
        },
      }),

      fastdraw_list: tool({
        description: "FastDraw: show current model assignments for all agents",
        args: {},
        async execute() {
          const cfgAgent = ((configRef as any)?.agent ?? {}) as AgentCfg
          const all = [
            ...new Set([
              ...OMO_BUILTINS,
              ...OVERRIDABLE_LIST,
              ...customNames,
              ...Object.keys(state.agents),
              ...Object.keys(cfgAgent),
            ]),
          ]
          const groups = categorize(all)
          return (
            `**FastDraw** (${STATE_FILE})\n\n${formatList(groups, state.agents, cfgAgent)}` +
            `\n\nUse \`fastdraw_assign\` or TUI (\`/fastdraw\` or \`<leader>m\`) to change.`
          )
        },
      }),

      /* ── Preset tools ─────────────────────────────────────────── */

      fastdraw_save_preset: tool({
        description: "FastDraw: save the current model assignments as a named preset",
        args: {
          name: tool.schema.string().describe("Preset name (e.g. coding-heavy)"),
          description: tool.schema.string().optional().describe("Optional description"),
        },
        async execute(args) {
          // Re-read the on-disk state: the TUI flow and external tools that
          // write .fastdraw.json directly may leave the boot-time in-memory
          // copy stale (false "no assignments to save").
          const fresh = await loadState()
          state.agents = fresh.agents
          // Origin-aware harvest: scan every config layer in opencode's
          // precedence order, record where each binding lives (portable
          // paths), and detect cross-layer conflicts. FastDraw state is the
          // top practical layer — it overrides harvested bindings.
          const ctx: HarvestCtx = {
            home: homedir(),
            configDir: CONFIG_DIR,
            cwd: process.cwd(),
            env: process.env as HarvestEnv,
          }
          const h = await harvest(ctx)
          const snapshot: Record<string, Binding> = { ...h.agents }
          for (const [name, model] of Object.entries(state.agents)) {
            snapshot[name] = {
              model,
              origin: { layer: "state", file: toPortablePath(STATE_FILE, ctx) },
            }
          }
          // Bindings opencode surfaced programmatically (config hook) that no
          // layer file records get no origin — they restore to the global
          // config and are reported as such.
          const cfgAgent = ((configRef as any)?.agent ?? {}) as AgentCfg
          const noOrigin: string[] = []
          for (const [name, a] of Object.entries(cfgAgent)) {
            if (name in snapshot) continue
            if (typeof a?.model === "string" && a.model.includes("/")) {
              snapshot[name] = { model: a.model }
              noOrigin.push(name)
            }
          }
          if (!Object.keys(snapshot).length) {
            return "**FastDraw**: no assignments to save — assign models first."
          }
          const note =
            noOrigin.length === 0
              ? ""
              : `\nNote: ${noOrigin.length} binding${noOrigin.length === 1 ? "" : "s"} have no recorded config origin and will restore to the global config: ${noOrigin.join(", ")}.`
          const sections = splitBindings(snapshot)
          presets.presets[args.name] = {
            schemaVersion: SCHEMA_VERSION,
            description: args.description,
            createdAt: new Date().toISOString(),
            ...sections,
          }
          await savePresets(presets)
          const p = presets.presets[args.name]
          const confBlock = formatConflictReport(h.conflicts)
          return `**FastDraw**: preset "${args.name}" saved (${Object.keys(presetAgents(p)).length} agents)\n\n${presetPreview(presetAgents(p))}${note}${confBlock}`
        },
      }),

      fastdraw_load_preset: tool({
        description:
          "FastDraw: load a preset — replaces ALL current assignments with the preset's bindings (effective immediately), and persists them into config files",
        args: {
          name: tool.schema.string().describe("Preset name"),
          mode: tool.schema
            .string()
            .optional()
            .describe(
              "Restore mode: 'global' (all bindings → global config, default), 'original' (each binding → the file its origin came from, unresolved origins fall back to global), 'path' (all bindings → targetPath)",
            ),
          targetPath: tool.schema
            .string()
            .optional()
            .describe("Target config file for mode 'path' (absolute or cwd-relative)"),
          preview: tool.schema
            .boolean()
            .optional()
            .describe("Only show the write plan — make no changes"),
        },
        async execute(args) {
          const mode: RestoreMode = (args.mode as RestoreMode) || "global"
          if (!["global", "original", "path"].includes(mode)) {
            return `**FastDraw**: unknown restore mode "${args.mode}" (expected global | original | path).`
          }
          if (mode === "path" && !args.targetPath) {
            return "**FastDraw**: targetPath is required for restore mode 'path'."
          }
          const p = presets.presets[args.name]
          if (!p) {
            const names = Object.keys(presets.presets)
            return `**FastDraw**: preset "${args.name}" not found.` +
              (names.length ? `\nAvailable: ${names.join(", ")}` : "\nNo presets saved yet.")
          }
          // Custom roles only apply when the agent exists on this machine
          // (agents dir, or a config agent entry). Missing ones are skipped
          // with a warning — a preset from another machine never fails.
          const present = new Set<string>([...customNames])
          if (configRef) {
            const agentCfg = ((configRef as any).agent ?? {}) as AgentCfg
            for (const n of Object.keys(agentCfg)) present.add(n)
          }
          const skipped: string[] = []
          const applyAgents: Record<string, string> = {}
          const flat = presetAgents(p)
          for (const n of Object.keys(p.custom)) {
            if (present.has(n)) applyAgents[n] = flat[n]
            else skipped.push(n)
          }
          for (const n of Object.keys(p.omo)) applyAgents[n] = flat[n]
          const ctx: HarvestCtx = {
            home: homedir(),
            configDir: CONFIG_DIR,
            cwd: process.cwd(),
            env: process.env as HarvestEnv,
          }
          const presentBindings: Record<string, Binding> = {}
          for (const [n, b] of Object.entries({ ...p.omo, ...p.custom })) {
            if (n in applyAgents) presentBindings[n] = b
          }
          const plan = await planRestore(presentBindings, mode, ctx, args.targetPath)
          if (args.preview) {
            const lines = plan.files.map(
              (f) => `  ${f.file}${f.create ? " (new file)" : ""} → ${Object.keys(f.entries).length} binding(s)`,
            )
            const fallbackBlock = plan.fallback.length
              ? `\n⚠ ${plan.fallback.length} role(s) have no resolvable origin on this machine and would land in the global config: ${plan.fallback.join(", ")}.`
              : ""
            return (
              `**FastDraw**: preview of preset "${args.name}" (mode: ${mode})${skipped.length ? `\nSkipped ${skipped.length} custom role${skipped.length === 1 ? "" : "s"} not present on this machine: ${skipped.join(", ")}.` : ""}\n` +
              `${lines.join("\n")}${fallbackBlock}`
            )
          }
          if (configRef) {
            snapshotOriginals(configRef, Object.keys(applyAgents))
            hotSwap(configRef, applyAgents)
          }
          state.agents = applyAgents
          await saveState(state)
          const outcomes = await restoreWrite(plan)
          const planLines = outcomes.map((o) => {
            const bindings = plan.files.find((f) => f.file === o.file)?.entries ?? {}
            const n = Object.keys(bindings).length
            return o.error
              ? `  ✗ ${o.file}: ${o.error}${o.backup ? ` (backup: ${o.backup})` : ""}`
              : `  ${o.file} → ${n} binding(s)${o.backup ? ` (backup: ${o.backup})` : ""}`
          })
          const fallbackBlock =
            plan.fallback.length === 0
              ? ""
              : `\n⚠ ${plan.fallback.length} role(s) had no resolvable origin on this machine and were written to the global config: ${plan.fallback.join(", ")}.`
          const stateBlock =
            plan.fromState.length === 0
              ? ""
              : `\nNote: ${plan.fromState.length} binding(s) came from FastDraw state (.fastdraw.json) and stay there: ${plan.fromState.join(", ")}.`
          const warn = skipped.length
            ? `\nSkipped ${skipped.length} custom role${skipped.length === 1 ? "" : "s"} not present on this machine: ${skipped.join(", ")}.`
            : ""
          const errBlock = outcomes.some((o) => o.error)
            ? "\n⚠ Restore partially failed — see ✗ lines above."
            : ""
          return (
            `**FastDraw**: preset "${args.name}" loaded (${Object.keys(applyAgents).length} agents)${warn}\n\n` +
            `${presetPreview(applyAgents)}\n\nAgents not in this preset reverted to their defaults.` +
            `\n\nRestored (mode: ${mode}):\n${planLines.join("\n")}${fallbackBlock}${stateBlock}${errBlock}`
          )
        },
      }),

      fastdraw_list_presets: tool({
        description: "FastDraw: list all saved presets with their full agent→model bindings",
        args: {},
        async execute() {
          const names = Object.keys(presets.presets).sort()
          if (!names.length) {
            return `**FastDraw**: no presets saved yet (${PRESETS_FILE}). Use fastdraw_save_preset.`
          }
          const blocks = names.map((n) => {
            const p = presets.presets[n]
            const meta = [
              `${Object.keys(presetAgents(p)).length} agents`,
              p.createdAt ? `saved ${p.createdAt.slice(0, 10)}` : null,
              p.description ?? null,
            ].filter(Boolean).join(" · ")
            return `### ${n}\n${meta}\n${presetPreview(presetAgents(p))}`
          })
          return `**FastDraw presets** (${PRESETS_FILE})\n\n${blocks.join("\n\n")}`
        },
      }),

      fastdraw_delete_preset: tool({
        description: "FastDraw: delete a saved preset",
        args: {
          name: tool.schema.string().describe("Preset name"),
        },
        async execute(args) {
          if (!(args.name in presets.presets)) {
            return `**FastDraw**: preset "${args.name}" not found.`
          }
          delete presets.presets[args.name]
          await savePresets(presets)
          return `**FastDraw**: preset "${args.name}" deleted.`
        },
      }),

      fastdraw_export_preset: tool({
        description: "FastDraw: export a preset to a portable JSON file for sharing",
        args: {
          name: tool.schema.string().describe("Preset name"),
          path: tool.schema
            .string()
            .optional()
            .describe("Output file path (default: ./fastdraw-preset-<name>.json)"),
        },
        async execute(args) {
          const p = presets.presets[args.name]
          if (!p) return `**FastDraw**: preset "${args.name}" not found.`
          const out = expandPath(args.path ?? `fastdraw-preset-${args.name}.json`)
          const payload = {
            fastdraw: 1,
            schemaVersion: SCHEMA_VERSION,
            name: args.name,
            description: p.description,
            exportedAt: new Date().toISOString(),
            omo: p.omo,
            custom: p.custom,
          }
          await fs.mkdir(path.dirname(out), { recursive: true })
          await fs.writeFile(out, JSON.stringify(payload, null, 2))
          return `**FastDraw**: preset "${args.name}" exported → ${out}\n\n${presetPreview(presetAgents(p))}`
        },
      }),

      fastdraw_import_preset: tool({
        description:
          "FastDraw: import preset(s) from a JSON file — accepts a single exported preset or a full presets store",
        args: {
          path: tool.schema.string().describe("File path to import (supports ~)"),
          name: tool.schema
            .string()
            .optional()
            .describe("Override the preset name (single-preset files only)"),
        },
        async execute(args) {
          const file = expandPath(args.path)
          let raw: unknown
          try {
            raw = JSON.parse(await fs.readFile(file, "utf-8"))
          } catch (e) {
            return `**FastDraw**: cannot read ${file} — ${e instanceof Error ? e.message : String(e)}`
          }
          let parsed: ReturnType<typeof parseImport>
          try {
            parsed = parseImport(raw)
          } catch (e) {
            return `**FastDraw**: invalid preset file — ${e instanceof Error ? e.message : String(e)}`
          }
          if (parsed.kind === "bulk") {
            for (const [n, p] of Object.entries(parsed.presets)) {
              presets.presets[n] = p
            }
            await savePresets(presets)
            const names = Object.keys(parsed.presets).sort()
            return `**FastDraw**: imported ${names.length} presets from ${file}:\n${names.map((n) => `  - ${n}`).join("\n")}`
          }
          const name = args.name ?? parsed.name ?? path.basename(file, ".json").replace(/^fastdraw-preset-/, "")
          presets.presets[name] = {
            schemaVersion: SCHEMA_VERSION,
            description: parsed.description,
            createdAt: new Date().toISOString(),
            omo: parsed.omo,
            custom: parsed.custom,
          }
          await savePresets(presets)
          return `**FastDraw**: preset "${name}" imported from ${file}\n\n${presetPreview(presetAgents(presets.presets[name]))}`
        },
      }),
    },
  }
}

export default { id: "fastdraw", server: fastdrawServer }
