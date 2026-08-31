/**
 * FastDraw TUI Plugin — dialog-based model switcher + preset manager
 *
 * Loaded by opencode's plugin resolver via exports: "./tui" → this file.
 * Validator sees only { id, tui } → passes J="tui" check.
 */
import type {
  TuiPlugin,
  TuiDialogStack,
  TuiDialogSelectOption,
} from "@opencode-ai/plugin/tui"
import fs from "node:fs/promises"
import { homedir } from "node:os"
import path from "node:path"
import { OMO_ROLES, OVERRIDABLE } from "./roles.js"
import {
  readOmoConfig,
  writeOmoModels,
  isOmoName,
  omoTargetKind,
  omoPortableFile,
  type OmoBinding,
  type OmoConfig,
  type OmoWriteResult,
} from "./omo.js"
import {
  SCHEMA_VERSION,
  toPortablePath,
  stripJsonComments,
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
  omo?: Record<string, OmoBinding>
}

async function loadState(): Promise<Assignments> {
  try {
    const raw = JSON.parse(await fs.readFile(STATE_FILE, "utf-8")) as Partial<Assignments>
    return { agents: raw.agents ?? {}, ...(raw.omo ? { omo: raw.omo } : {}) }
  } catch {
    return { agents: {} }
  }
}

/** Assign one OMO role/category in the OMO config, recording the pre-fastdraw
 *  model so remove/load-preset can revert. Returns an error string on failure. */
async function omoAssignToFile(
  omoCfg: OmoConfig,
  name: string,
  model: string,
): Promise<string | null> {
  const state = await loadState()
  state.omo ??= {}
  const prev = state.omo[name]
  state.omo[name] = { model, original: prev?.original ?? omoCfg.targets[name]?.model ?? null }
  const res = await writeOmoModels({ [name]: model })
  if (!res.written) {
    if (prev) state.omo[name] = prev
    else delete state.omo[name]
    return res.error ?? "unknown OMO write error"
  }
  delete state.agents[name]
  await saveState(state)
  return null
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

function presetPreview(agents: Record<string, string>): string {
  const entries = Object.entries(agents).sort(([a], [b]) => a.localeCompare(b))
  const w = Math.max(4, ...entries.map(([n]) => n.length))
  return entries.map(([n, m]) => `${n.padEnd(w)}  →  ${m}`).join("\n")
}

function expandPath(p: string): string {
  if (p === "~") return homedir()
  if (p.startsWith("~/")) return path.join(homedir(), p.slice(2))
  return path.resolve(process.cwd(), p)
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

/* ── Build model list from providers ──────────────────────────────── */

function buildModelList(api: any): { id: string; provider: string }[] {
  const providers = api.state?.provider ?? []
  const allModels: { id: string; provider: string }[] = []
  for (const p of providers) {
    if (p.models) {
      for (const m of Object.keys(p.models)) {
        allModels.push({ id: `${p.id}/${m}`, provider: p.name ?? p.id })
      }
    }
  }
  return allModels
}

/* ── Dialog Flows ─────────────────────────────────────────────────── */

type Ui = any

function toast(ui: Ui, message: string, variant: "info" | "success" | "warning" | "error" = "info") {
  ui.toast({ message, variant })
}

function flowError(ui: Ui, err: unknown) {
  toast(ui, `FastDraw: ${err instanceof Error ? err.message : String(err)}`, "error")
}

/** Flow: pick agent → pick model → persist → return to the agent list. */
async function assignFlow(ui: Ui, allModels: { id: string; provider: string }[]) {
  if (!allModels.length) {
    toast(ui, "FastDraw: No models configured", "warning")
    return
  }
  const omoCfg = await readOmoConfig()
  const omoRouted = (name: string): boolean =>
    omoCfg.exists && omoCfg.parseable && isOmoName(omoCfg, name)

  const showAgentList = async (focusName?: string): Promise<void> => {
    const assignments = await loadState()
    const customFromDir = await readCustomAgentNames()

    const allNames = new Set<string>([
      ...Object.keys(omoCfg.targets),
      ...OMO_ROLES,
      ...OVERRIDABLE,
      ...customFromDir,
      ...Object.keys(assignments.agents),
      ...Object.keys(assignments.omo ?? {}),
    ])

    const currentModel = (name: string): string | undefined =>
      assignments.omo?.[name]?.model ??
      (omoRouted(name) ? (omoCfg.targets[name]?.model ?? undefined) : undefined) ??
      assignments.agents[name]

    const kindLabel = (name: string): string => {
      const k = omoTargetKind(omoCfg, name)
      if (k === "category") return "OMO Categories"
      if (k === "agent") return "OMO Roles"
      if (OMO_ROLES.has(name)) return "OMO Roles"
      if (OVERRIDABLE.has(name)) return "Overrideable"
      return "Custom"
    }

    const agentOpts: TuiDialogSelectOption<string>[] = [...allNames]
      .sort()
      .map((name) => ({
        title: name,
        value: name,
        description: currentModel(name) ? `→ ${currentModel(name)}` : undefined,
        category: kindLabel(name),
      }))

    ui.dialog.setSize("large")
    ui.dialog.replace(() =>
      ui.DialogSelect<string>({
        title: "FastDraw — Select Agent",
        options: agentOpts,
        current: focusName,
        onSelect: async (opt: any) => {
          if (!opt) return
          const agentName = opt.value
          const current = currentModel(agentName) ?? ""

          ui.dialog.clear()
          ui.dialog.replace(() =>
            ui.DialogSelect<string>({
              title: `FastDraw — Model for ${agentName}`,
              options: allModels.map((m) => ({
                title: m.id,
                value: m.id,
                category: m.provider,
              })),
              current: current || undefined,
              onSelect: async (modelOpt: any) => {
                if (!modelOpt) return
                ui.dialog.clear()
                try {
                  if (omoRouted(agentName)) {
                    const err = await omoAssignToFile(omoCfg, agentName, modelOpt.value)
                    if (err) {
                      toast(ui, `FastDraw: ${agentName} → ${modelOpt.value} FAILED — ${err}`, "error")
                    } else {
                      toast(
                        ui,
                        `${agentName} → ${modelOpt.value} — bound in ${omoCfg.file}, takes effect on next start`,
                        "success",
                      )
                      if (omoCfg.shadowFiles.length) {
                        toast(
                          ui,
                          `⚠ Project OMO config shadows this file: ${omoCfg.shadowFiles.join(", ")}`,
                          "warning",
                        )
                      }
                    }
                  } else {
                    const next = await loadState()
                    next.agents[agentName] = modelOpt.value
                    await saveState(next)
                    toast(ui, `${agentName} → ${modelOpt.value} (applies on restart)`, "success")
                  }
                } catch (e) {
                  flowError(ui, e)
                }
                await showAgentList(agentName)
              },
            }),
          )
        },
      }),
    )
  }

  await showAgentList()
}

/** Flow: prompt name → prompt description → save current assignments. */
function savePresetFlow(ui: Ui) {
  ui.dialog.setSize("medium")
  ui.dialog.replace(() =>
    ui.DialogPrompt({
      title: "FastDraw — Save Preset: name",
      placeholder: "e.g. coding-heavy",
      onCancel: () => ui.dialog.clear(),
      onConfirm: (name: string) => {
        const trimmed = name.trim()
        if (!trimmed) {
          toast(ui, "FastDraw: preset name cannot be empty", "warning")
          return
        }
        ui.dialog.clear()
        ui.dialog.replace(() =>
          ui.DialogPrompt({
            title: `FastDraw — Save Preset "${trimmed}": description (optional)`,
            placeholder: "What is this preset for?",
            onCancel: () => ui.dialog.clear(),
            onConfirm: async (description: string) => {
              ui.dialog.clear()
              try {
                const state = await loadState()
                const omoCfg = await readOmoConfig()
                const ctx: HarvestCtx = {
                  home: homedir(),
                  configDir: CONFIG_DIR,
                  cwd: process.cwd(),
                  env: process.env as HarvestEnv,
                }
                const h = await harvest(ctx)
                const snapshot: Record<string, Binding> = { ...h.agents }
                if (omoCfg.exists && omoCfg.parseable) {
                  const port = omoPortableFile(omoCfg, homedir(), CONFIG_DIR)
                  for (const [name, t] of Object.entries(omoCfg.targets)) {
                    if (t.model) {
                      snapshot[name] = { model: t.model, origin: port ? { layer: "omo", file: port } : undefined }
                    }
                  }
                }
                for (const [name, model] of Object.entries(state.agents)) {
                  snapshot[name] = {
                    model,
                    origin: { layer: "state", file: toPortablePath(STATE_FILE, ctx) },
                  }
                }
                for (const [name, rec] of Object.entries(state.omo ?? {})) {
                  const port = omoPortableFile(omoCfg, homedir(), CONFIG_DIR)
                  snapshot[name] = {
                    model: rec.model,
                    origin: omoCfg.file ? (port ? { layer: "omo", file: port } : undefined) : { layer: "state", file: toPortablePath(STATE_FILE, ctx) },
                  }
                }
                if (!Object.keys(snapshot).length) {
                  toast(ui, "FastDraw: no assignments to save — assign models first", "warning")
                  return
                }
                const store = await loadPresets()
                store.presets[trimmed] = {
                  schemaVersion: SCHEMA_VERSION,
                  description: description.trim() || undefined,
                  createdAt: new Date().toISOString(),
                  ...splitBindings(snapshot),
                }
                await savePresets(store)
                toast(
                  ui,
                  `Preset "${trimmed}" saved (${Object.keys(presetAgents(store.presets[trimmed])).length} agents)`,
                  "success",
                )
                if (h.conflicts.length) {
                  const report = formatConflictReport(h.conflicts)
                  ui.dialog.replace(() =>
                    ui.DialogPrompt({
                      title: "FastDraw — config conflicts detected",
                      placeholder: "Press Enter to dismiss",
                      value: "",
                      onCancel: () => ui.dialog.clear(),
                      onConfirm: () => ui.dialog.clear(),
                    }),
                  )
                  toast(ui, `Saved with conflicts:\n${report}`, "warning")
                }
              } catch (e) {
                flowError(ui, e)
              }
            },
          }),
        )
      },
    }),
  )
}

/** Pick a preset, then run `action` with (name, preset). */
async function pickPreset(ui: Ui, title: string, action: (name: string, p: Preset) => void) {
  const store = await loadPresets()
  const names = Object.keys(store.presets).sort()
  if (!names.length) {
    toast(ui, "FastDraw: no presets saved yet", "warning")
    return
  }
  ui.dialog.setSize("large")
  ui.dialog.replace(() =>
    ui.DialogSelect<string>({
      title,
      options: names.map((n) => {
        const p = store.presets[n]
        return {
          title: n,
          value: n,
          description:
            `${Object.keys(presetAgents(p)).length} agents` + (p.description ? ` — ${p.description}` : ""),
        }
      }),
      onSelect: (opt: any) => {
        if (!opt) return
        action(opt.value, store.presets[opt.value])
      },
    }),
  )
}

/** Browse directories, pick an existing file, or create a new one.
 *  Returns the chosen path, or null when cancelled. */
async function pickTargetFile(ui: Ui, startDir: string): Promise<string | null> {
  let dir = startDir
  for (;;) {
    let entries: { name: string; isDir: boolean }[] = []
    try {
      const raw = await fs.readdir(dir, { withFileTypes: true })
      entries = raw
        .sort((a, b) =>
          a.isDirectory() === b.isDirectory()
            ? a.name.localeCompare(b.name)
            : a.isDirectory()
              ? -1
              : 1,
        )
        .map((d) => ({ name: d.name, isDir: d.isDirectory() }))
    } catch {
      toast(ui, `Cannot read directory: ${dir}`, "warning")
      return null
    }
    const opts: TuiDialogSelectOption<string>[] = entries.map((e) => ({
      title: e.isDir ? `${e.name}/` : e.name,
      value: e.isDir ? `d:${path.join(dir, e.name)}` : `f:${path.join(dir, e.name)}`,
      description: e.isDir ? "directory" : "file",
    }))
    if (path.dirname(dir) !== dir) {
      opts.unshift({ title: "..", value: `d:${path.dirname(dir)}`, description: "parent" })
    }
    opts.push({ title: "＋ Create new file…", value: "__new__", description: "type a filename" })
    const choice = await new Promise<string | null>((resolve) => {
      ui.dialog.setSize("large")
      ui.dialog.replace(() =>
        ui.DialogSelect<string>({
          title: `FastDraw — pick config file (${dir})`,
          options: opts,
          onSelect: (opt: any) => (opt ? resolve(opt.value) : resolve(null)),
        }),
      )
    })
    if (choice === null) return null
    if (choice === "__new__") {
      const name = await new Promise<string | null>((resolve) => {
        ui.dialog.setSize("medium")
        ui.dialog.replace(() =>
          ui.DialogPrompt({
            title: `FastDraw — new file in ${dir}`,
            placeholder: "opencode.jsonc",
            onCancel: () => resolve(null),
            onConfirm: (input: string) => {
              const trimmed = input.trim()
              resolve(trimmed ? path.join(dir, trimmed) : null)
            },
          }),
        )
      })
      if (!name) continue
      return name
    }
    if (choice.startsWith("d:")) {
      dir = choice.slice(2)
      continue
    }
    return choice.slice(2)
  }
}

/** Flow: pick preset → restore mode → (optionally pick a target file) →
 *  confirm with write plan → apply live + persist with backup. */
function loadPresetFlow(ui: Ui) {
  pickPreset(ui, "FastDraw — Load Preset", async (name, p) => {
    ui.dialog.clear()
    try {
      // Custom roles only apply when the agent exists on this machine
      // (agents dir). Missing ones are skipped with a warning — a preset
      // from another machine never fails.
      const present = new Set<string>(await readCustomAgentNames())
      const skipped: string[] = []
      const applyAgents: Record<string, string> = {}
      const flat = presetAgents(p)
      for (const n of Object.keys(p.custom)) {
        if (present.has(n)) applyAgents[n] = flat[n]
        else skipped.push(n)
      }
      for (const n of Object.keys(p.omo)) applyAgents[n] = flat[n]
      const presentBindings: Record<string, Binding> = {}
      for (const [n, b] of Object.entries({ ...p.omo, ...p.custom })) {
        if (n in applyAgents) presentBindings[n] = b
      }
      // OMO-routed names bind in the OMO config file — never into opencode
      // config layers (that creates phantom roles). Split before planning.
      const omoCfg = await readOmoConfig()
      const omoRouted = (n: string): boolean =>
        omoCfg.exists && omoCfg.parseable && isOmoName(omoCfg, n)
      const omoSide: Record<string, string> = {}
      for (const n of Object.keys(presentBindings)) {
        if (omoRouted(n)) omoSide[n] = presentBindings[n].model
      }
      const cfgApplyAgents: Record<string, string> = {}
      for (const [n, m] of Object.entries(applyAgents)) {
        if (!(n in omoSide)) cfgApplyAgents[n] = m
      }
      const cfgSideBindings: Record<string, Binding> = {}
      for (const [n, b] of Object.entries(presentBindings)) {
        if (!(n in omoSide)) cfgSideBindings[n] = b
      }
      const mode = await new Promise<RestoreMode | null>((resolve) => {
        ui.dialog.setSize("small")
        ui.dialog.replace(() =>
          ui.DialogSelect<RestoreMode>({
            title: `FastDraw — restore preset "${name}" where?`,
            options: [
              {
                title: "Global config",
                value: "global",
                description: "all bindings → ~/.config/opencode config file",
              },
              {
                title: "Original locations",
                value: "original",
                description: "each binding back to the file it was recorded from",
              },
              { title: "Choose a file…", value: "path", description: "pick or create a config file" },
            ],
            onSelect: (opt: any) => (opt ? resolve(opt.value) : resolve(null)),
          }),
        )
      })
      if (mode === null) return
      const ctx: HarvestCtx = {
        home: homedir(),
        configDir: CONFIG_DIR,
        cwd: process.cwd(),
        env: process.env as HarvestEnv,
      }
      let targetPath: string | undefined
      if (mode === "path") {
        targetPath = (await pickTargetFile(ui, CONFIG_DIR)) ?? undefined
        if (!targetPath) return
      }
      const plan = await planRestore(cfgSideBindings, mode, ctx, targetPath)
      const planLines = plan.files.map(
        (f) => `  ${f.file}${f.create ? " (new file)" : ""} → ${Object.keys(f.entries).length} binding(s)`,
      )
      const fallbackBlock = plan.fallback.length
        ? `\n⚠ ${plan.fallback.length} role(s) have no resolvable origin on this machine and would land in the global config: ${plan.fallback.join(", ")}.`
        : ""
      const skippedBlock = skipped.length
        ? `\nSkipped ${skipped.length} custom role${skipped.length === 1 ? "" : "s"} not present on this machine: ${skipped.join(", ")}.`
        : ""
      const omoBlock = Object.keys(omoSide).length
        ? `\nOMO config bindings (${omoCfg.file}):\n${presetPreview(omoSide)}\n`
        : ""
      const modeLabel =
        mode === "path" ? `path → ${targetPath}` : mode
      ui.dialog.setSize("large")
      ui.dialog.replace(() =>
        ui.DialogConfirm({
          title: `Load preset "${name}"? (mode: ${modeLabel})`,
          message:
            `${presetPreview(applyAgents)}\n\n` +
            `This replaces ALL current assignments. Agents not listed revert to defaults.\n\n` +
            `Files to write:\n${planLines.join("\n")}${fallbackBlock}${skippedBlock}${omoBlock}\n\n` +
            `Existing files are backed up as <file>.bak-<timestamp> before writing.`,
          onCancel: () => ui.dialog.clear(),
          onConfirm: async () => {
            ui.dialog.clear()
            try {
              const state = await loadState()
              const omoWrite: Record<string, string> = { ...omoSide }
              const omoReverted: string[] = []
              for (const [n, rec] of Object.entries(state.omo ?? {})) {
                if (n in omoSide) continue
                if (rec.original) omoWrite[n] = rec.original
                omoReverted.push(n)
              }
              let omoRes: OmoWriteResult | null = null
              if (Object.keys(omoWrite).length || omoReverted.length) {
                if (Object.keys(omoWrite).length) {
                  omoRes = await writeOmoModels(omoWrite)
                  if (!omoRes.written) {
                    toast(ui, `Load aborted — OMO config write failed: ${omoRes.error}`, "error")
                    return
                  }
                }
                for (const [n, m] of Object.entries(omoSide)) {
                  state.omo ??= {}
                  state.omo[n] = {
                    model: m,
                    original: state.omo[n]?.original ?? omoCfg.targets[n]?.model ?? null,
                  }
                }
                for (const n of omoReverted) delete state.omo![n]
              }
              await saveState({
                agents: cfgApplyAgents,
                ...(Object.keys(state.omo ?? {}).length ? { omo: state.omo } : {}),
              })
              const outcomes = await restoreWrite(plan)
              const failed = outcomes.filter((o) => o.error)
              if (failed.length) {
                toast(ui, `Restore failed for ${failed.length} file(s) — backups kept`, "warning")
              }
              toast(
                ui,
                `Preset "${name}" loaded${omoRes ? ` (OMO config: ${omoRes.file})` : ""} — restart to apply`,
                "success",
              )
              if (omoCfg.shadowFiles.length) {
                toast(
                  ui,
                  `⚠ Project OMO config shadows the user file: ${omoCfg.shadowFiles.join(", ")}`,
                  "warning",
                )
              }
              if (plan.fallback.length) {
                toast(
                  ui,
                  `${plan.fallback.length} role(s) had no origin → written to the global config: ${plan.fallback.join(", ")}`,
                  "warning",
                )
              }
              if (skipped.length) {
                toast(
                  ui,
                  `Skipped ${skipped.length} custom role${skipped.length === 1 ? "" : "s"} not present on this machine: ${skipped.join(", ")}.`,
                  "warning",
                )
              }
            } catch (e) {
              flowError(ui, e)
            }
          },
        }),
      )
    } catch (e) {
      flowError(ui, e)
    }
  }).catch((e) => flowError(ui, e))
}

/** Flow: prompt path → import preset(s). */
function importPresetFlow(ui: Ui) {
  ui.dialog.setSize("medium")
  ui.dialog.replace(() =>
    ui.DialogPrompt({
      title: "FastDraw — Import Preset: file path",
      placeholder: "~/preset.json or ./preset.json",
      onCancel: () => ui.dialog.clear(),
      onConfirm: async (input: string) => {
        ui.dialog.clear()
        const file = expandPath(input.trim())
        try {
          const raw = JSON.parse(await fs.readFile(file, "utf-8"))
          const parsed = parseImport(raw)
          const store = await loadPresets()
          if (parsed.kind === "bulk") {
            for (const [n, p] of Object.entries(parsed.presets)) {
              store.presets[n] = p
            }
            await savePresets(store)
            toast(
              ui,
              `Imported ${Object.keys(parsed.presets).length} presets: ${Object.keys(parsed.presets).sort().join(", ")}`,
              "success",
            )
          } else {
            const name =
              parsed.name ?? path.basename(file, ".json").replace(/^fastdraw-preset-/, "")
            store.presets[name] = {
              schemaVersion: SCHEMA_VERSION,
              description: parsed.description,
              createdAt: new Date().toISOString(),
              omo: parsed.omo,
              custom: parsed.custom,
            }
            await savePresets(store)
            toast(
              ui,
              `Preset "${name}" imported (${Object.keys(presetAgents(store.presets[name])).length} agents)`,
              "success",
            )
          }
        } catch (e) {
          flowError(ui, e)
        }
      },
    }),
  )
}

/** Flow: pick preset → prompt output path → write file. */
function exportPresetFlow(ui: Ui) {
  pickPreset(ui, "FastDraw — Export Preset", (name, p) => {
    ui.dialog.clear()
    ui.dialog.replace(() =>
      ui.DialogPrompt({
        title: `FastDraw — Export "${name}" to file`,
        value: `fastdraw-preset-${name}.json`,
        onCancel: () => ui.dialog.clear(),
        onConfirm: async (input: string) => {
          ui.dialog.clear()
          const out = expandPath(input.trim() || `fastdraw-preset-${name}.json`)
          try {
            const payload = {
              fastdraw: 1,
              schemaVersion: SCHEMA_VERSION,
              name,
              description: p.description,
              exportedAt: new Date().toISOString(),
              omo: p.omo,
              custom: p.custom,
            }
            await fs.mkdir(path.dirname(out), { recursive: true })
            await fs.writeFile(out, JSON.stringify(payload, null, 2))
            toast(ui, `Preset "${name}" exported → ${out}`, "success")
          } catch (e) {
            flowError(ui, e)
          }
        },
      }),
    )
  }).catch((e) => flowError(ui, e))
}

/** Flow: pick preset → confirm → delete. */
function deletePresetFlow(ui: Ui) {
  pickPreset(ui, "FastDraw — Delete Preset", (name, p) => {
    ui.dialog.clear()
    ui.dialog.replace(() =>
      ui.DialogConfirm({
        title: `Delete preset "${name}"?`,
        message: `${Object.keys(presetAgents(p)).length} agent bindings will be removed from the preset store.\nCurrent assignments are NOT affected.`,
        onCancel: () => ui.dialog.clear(),
        onConfirm: async () => {
          ui.dialog.clear()
          try {
            const store = await loadPresets()
            delete store.presets[name]
            await savePresets(store)
            toast(ui, `Preset "${name}" deleted`, "success")
          } catch (e) {
            flowError(ui, e)
          }
        },
      }),
    )
  }).catch((e) => flowError(ui, e))
}

/* ── Main Menu ────────────────────────────────────────────────────── */

type MenuAction = "assign" | "save" | "load" | "import" | "export" | "delete"

function buildDialogHandler(
  api: any,
  allModels: { id: string; provider: string }[],
): (_dialog?: TuiDialogStack) => Promise<void> {
  const ui = api.ui
  return async (_dialog?: TuiDialogStack) => {
    try {
      ui.dialog.setSize("medium")
      ui.dialog.replace(() =>
        ui.DialogSelect<MenuAction>({
          title: "FastDraw — Model Assignments & Presets",
          options: [
            { title: "Assign Model", value: "assign" as const, description: "Bind a model to an agent" },
            { title: "Save Current as Preset", value: "save" as const, description: "Snapshot all current assignments" },
            { title: "Load Preset", value: "load" as const, description: "Preview bindings, then apply" },
            { title: "Import Preset from File", value: "import" as const, description: "Load preset(s) from a JSON file" },
            { title: "Export Preset to File", value: "export" as const, description: "Share a preset as JSON" },
            { title: "Delete Preset", value: "delete" as const, description: "Remove a saved preset" },
          ],
          onSelect: (opt: any) => {
            if (!opt) return
            ui.dialog.clear()
            switch (opt.value as MenuAction) {
              case "assign":
                assignFlow(ui, allModels).catch((e) => flowError(ui, e))
                break
              case "save":
                savePresetFlow(ui)
                break
              case "load":
                loadPresetFlow(ui)
                break
              case "import":
                importPresetFlow(ui)
                break
              case "export":
                exportPresetFlow(ui)
                break
              case "delete":
                deletePresetFlow(ui)
                break
            }
          },
        }),
      )
    } catch (err) {
      flowError(api.ui, err)
    }
  }
}

/* ── TUI Plugin ───────────────────────────────────────────────────── */

const tui: TuiPlugin = async function fastdrawTui(api) {
  const allModels = buildModelList(api)
  const openFastDraw = buildDialogHandler(api, allModels)

  /* Modern API: keymap.registerLayer */
  const keymap = (api as any).keymap
  if (keymap?.registerLayer) {
    try {
      keymap.registerLayer({
        commands: [
          {
            name: "fastdraw.open",
            title: "FastDraw",
            desc: "Model switching & presets for agents",
            category: "FastDraw",
            namespace: "palette",
            slashName: "fastdraw",
            run: openFastDraw,
          },
        ],
        bindings: [
          { key: "<leader>m", cmd: "fastdraw.open" },
        ],
      })
      return
    } catch {
      // fall through to legacy API
    }
  }

  /* Legacy API: command.register */
  const cmdApi = (api as any).command
  if (cmdApi?.register) {
    cmdApi.register(() => [
      {
        title: "FastDraw",
        value: "fastdraw",
        description: "Model switching & presets for agents",
        category: "FastDraw",
        slash: { name: "fastdraw" },
        suggested: true,
        onSelect: openFastDraw,
      },
    ])
  }
}

export default { id: "fastdraw", tui }
