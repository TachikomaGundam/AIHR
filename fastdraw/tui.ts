/**
 * FastDraw TUI Plugin — dialog-based model switcher + preset manager
 *
 * Loaded by opencode's plugin resolver via exports: "./tui" → this file.
 * Dual entry: V2 loads the Plugin.define shape ({ id, setup }); the legacy
 * V1 `tui` function (opencode ≥1.18.29) hangs off the same default export.
 */
import { Plugin } from "@opencode/plugin/tui"
import fs from "node:fs/promises"
import { homedir } from "node:os"
import path from "node:path"
import { OMO_ROLES, OVERRIDABLE } from "./roles.js"
import {
  readOmoConfig,
  writeOmoModels,
  isOmoName,
  omoDriftNote,
  omoTargetKind,
  omoPortableFile,
  omoRevertSpec,
  type ModelEntrySpec,
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
  safeWriteFile,
  type RestoreMode,
  type Binding,
  type HarvestCtx,
  type HarvestEnv,
} from "./origins.js"
import { scanHomeReach, homeReachToastLines } from "./home-reach.js"

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
  state.omo[name] = {
    model,
    original: prev?.original ?? omoCfg.targets[name]?.model ?? null,
    original_models: prev?.original_models ?? omoCfg.targets[name]?.models ?? null,
  }
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
  await safeWriteFile(STATE_FILE, JSON.stringify(a, null, 2))
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
  await safeWriteFile(PRESETS_FILE, JSON.stringify(store, null, 2))
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

/** UI facade both plugin generations plug into. V2 (Plugin.setup) wraps the
 *  promise dialogs of @opencode/plugin/tui; V1 wraps the legacy component-
 *  stack dialogs. The flows below only ever talk to this shape. */
type Ui = {
  toast(message: string, variant?: "info" | "success" | "warning" | "error"): void
  select<T>(opts: {
    title: string
    options: { title: string; value: T; description?: string; category?: string }[]
    current?: T
    size?: "medium" | "large"
  }): Promise<T | undefined>
  prompt(opts: { title: string; placeholder?: string; value?: string }): Promise<string | undefined>
  confirm(opts: { title: string; message: string }): Promise<boolean | undefined>
  alert(opts: { title: string; message: string }): Promise<void>
  close(): void
  models(): Promise<{ id: string; provider: string }[]>
}

function toast(ui: Ui, message: string, variant: "info" | "success" | "warning" | "error" = "info") {
  ui.toast(message, variant)
}

function flowError(ui: Ui, err: unknown) {
  toast(ui, `FastDraw: ${err instanceof Error ? err.message : String(err)}`, "error")
}

/** V2 TUI context (@opencode/plugin/tui) → Ui facade over promise dialogs. */
function v2Ui(ctx: any): Ui {
  const collect = async (col: any): Promise<any[]> => {
    if (!col?.list) return []
    let items = col.list(ctx.location)
    if (!items?.length) {
      try {
        await col.sync(ctx.location)
      } catch {
        /* sync is best-effort — an empty list just means "pick manually" */
      }
      items = col.list(ctx.location)
    }
    return items ?? []
  }
  return {
    toast: (message, variant = "info") => ctx.ui.toast.show({ message, variant }),
    select: ({ title, options, current, size }: any) => {
      if (size && size !== "medium") ctx.ui.dialog.set({ size })
      return ctx.ui.dialog.select({ title, options, current })
    },
    prompt: ({ title, placeholder, value }: any) =>
      ctx.ui.dialog.prompt({ title, placeholder, value }),
    confirm: ({ title, message }: any) => ctx.ui.dialog.confirm({ title, message }),
    alert: ({ title, message }: any) => ctx.ui.dialog.alert({ title, message }),
    close: () => {
      try {
        ctx.ui.dialog.clear()
      } catch {
        /* nothing open — clearing is a no-op */
      }
    },
    models: async () => {
      const providers = await collect(ctx.data?.location?.provider)
      const names: Record<string, string> = {}
      for (const p of providers) if (p?.id) names[String(p.id)] = p.name ?? String(p.id)
      const models = await collect(ctx.data?.location?.model)
      const out: { id: string; provider: string }[] = []
      for (const m of models) {
        if (!m?.modelID || !m?.providerID) continue
        out.push({
          id: `${m.providerID}/${m.modelID}`,
          provider: names[String(m.providerID)] ?? String(m.providerID),
        })
      }
      return out
    },
  }
}

/** Legacy @opencode-ai/plugin TUI api → Ui. Same flows; the callback dialog
 *  stack is wrapped into promises (the pattern the V1 file picker already
 *  used), so V1 keeps its exact dialog widgets. */
function v1Ui(api: any): Ui {
  const ui = api.ui
  const prep = (size?: "medium" | "large") =>
    ui.dialog.setSize(size === "large" ? "large" : "medium")
  return {
    toast: (message, variant = "info") => ui.toast({ message, variant }),
    select: (opts: any): Promise<any> =>
      new Promise((resolve) => {
        prep(opts.size)
        ui.dialog.replace(() =>
          ui.DialogSelect({
            title: opts.title,
            options: opts.options,
            current: opts.current,
            onSelect: (opt: any) => {
              ui.dialog.clear()
              resolve(opt ? opt.value : undefined)
            },
          }),
        )
      }),
    prompt: (opts: any): Promise<string | undefined> =>
      new Promise((resolve) => {
        prep()
        ui.dialog.replace(() =>
          ui.DialogPrompt({
            title: opts.title,
            placeholder: opts.placeholder,
            value: opts.value,
            onCancel: () => {
              ui.dialog.clear()
              resolve(undefined)
            },
            onConfirm: (input: string) => {
              ui.dialog.clear()
              resolve(input)
            },
          }),
        )
      }),
    confirm: (opts: any): Promise<boolean> =>
      new Promise((resolve) => {
        prep()
        ui.dialog.replace(() =>
          ui.DialogConfirm({
            title: opts.title,
            message: opts.message,
            onCancel: () => {
              ui.dialog.clear()
              resolve(false)
            },
            onConfirm: () => {
              ui.dialog.clear()
              resolve(true)
            },
          }),
        )
      }),
    alert: (opts: any): Promise<void> =>
      new Promise((resolve) => {
        prep()
        ui.dialog.replace(() =>
          ui.DialogConfirm({
            title: opts.title,
            message: opts.message,
            onCancel: () => {
              ui.dialog.clear()
              resolve()
            },
            onConfirm: () => {
              ui.dialog.clear()
              resolve()
            },
          }),
        )
      }),
    close: () => ui.dialog.clear(),
    models: async () => buildModelList(api),
  }
}

/** Flow: pick agent → pick model → persist → return to the agent list. */
async function assignFlow(ui: Ui) {
  const allModels = await ui.models()
  if (!allModels.length) {
    toast(ui, "FastDraw: No models configured", "warning")
    return
  }
  const omoCfg = await readOmoConfig()
  const omoRouted = (name: string): boolean =>
    omoCfg.parseable && isOmoName(omoCfg, name)

  let focusName: string | undefined
  for (;;) {
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

    // A hand-edited entry may still carry the legacy shape (category scalar
    // keys, or a dead models[] on an agent) — surface it, mirroring
    // fastdraw_list.
    const shadowNote = (name: string): string => omoDriftNote(omoCfg.targets[name])

    const kindLabel = (name: string): string => {
      const k = omoTargetKind(omoCfg, name)
      if (k === "category") return "OMO Categories"
      if (k === "agent") return "OMO Roles"
      if (OMO_ROLES.has(name)) return "OMO Roles"
      if (OVERRIDABLE.has(name)) return "Overrideable"
      return "Custom"
    }

    const agentOpts: { title: string; value: string; description?: string; category: string }[] =
      [...allNames]
        .sort()
        .map((name) => ({
          title: name,
          value: name,
          description: currentModel(name)
            ? `→ ${currentModel(name)}${shadowNote(name)}`
            : shadowNote(name) || undefined,
          category: kindLabel(name),
        }))

    const agentName = await ui.select<string>({
      title: "FastDraw — Select Agent",
      options: agentOpts,
      current: focusName,
      size: "large",
    })
    if (agentName === undefined) return // ESC closes the flow (both engines)
    focusName = agentName

    const current = currentModel(agentName) ?? ""
    const modelID = await ui.select<string>({
      title: `FastDraw — Model for ${agentName}`,
      options: allModels.map((m) => ({
        title: m.id,
        value: m.id,
        category: m.provider,
      })),
      current: current || undefined,
      size: "large",
    })
    if (modelID === undefined) continue // ESC on the model list → back to agents

    try {
      if (omoRouted(agentName)) {
        const err = await omoAssignToFile(omoCfg, agentName, modelID)
        if (err) {
          toast(ui, `FastDraw: ${agentName} → ${modelID} FAILED — ${err}`, "error")
        } else {
          toast(
            ui,
            `${agentName} → ${modelID} — bound in ${omoCfg.file}, takes effect on next start`,
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
        next.agents[agentName] = modelID
        await saveState(next)
        toast(ui, `${agentName} → ${modelID} (applies on restart)`, "success")
      }
    } catch (e) {
      flowError(ui, e)
    }
  }
}

/** Flow: prompt name → prompt description → save current assignments. */
async function savePresetFlow(ui: Ui) {
  let trimmed = ""
  for (;;) {
    const name = await ui.prompt({
      title: "FastDraw — Save Preset: name",
      placeholder: "e.g. coding-heavy",
    })
    if (name === undefined) return
    trimmed = name.trim()
    if (trimmed) break
    toast(ui, "FastDraw: preset name cannot be empty", "warning")
  }
  const description = await ui.prompt({
    title: `FastDraw — Save Preset "${trimmed}": description (optional)`,
    placeholder: "What is this preset for?",
  })
  if (description === undefined) return
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
      toast(ui, `Saved with conflicts:\n${report}`, "warning")
      await ui.alert({ title: "FastDraw — config conflicts detected", message: report })
    }
  } catch (e) {
    flowError(ui, e)
  }
}

/** Pick a preset by name; undefined when the store is empty or on cancel. */
async function pickPreset(ui: Ui, title: string): Promise<string | undefined> {
  const store = await loadPresets()
  const names = Object.keys(store.presets).sort()
  if (!names.length) {
    toast(ui, "FastDraw: no presets saved yet", "warning")
    return undefined
  }
  return ui.select<string>({
    title,
    size: "large",
    options: names.map((n) => {
      const p = store.presets[n]
      return {
        title: n,
        value: n,
        description:
          `${Object.keys(presetAgents(p)).length} agents` + (p.description ? ` — ${p.description}` : ""),
      }
    }),
  })
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
    const opts: { title: string; value: string; description: string }[] = entries.map((e) => ({
      title: e.isDir ? `${e.name}/` : e.name,
      value: e.isDir ? `d:${path.join(dir, e.name)}` : `f:${path.join(dir, e.name)}`,
      description: e.isDir ? "directory" : "file",
    }))
    if (path.dirname(dir) !== dir) {
      opts.unshift({ title: "..", value: `d:${path.dirname(dir)}`, description: "parent" })
    }
    opts.push({ title: "＋ Create new file…", value: "__new__", description: "type a filename" })
    const choice = await ui.select<string>({
      title: `FastDraw — pick config file (${dir})`,
      options: opts,
      size: "large",
    })
    if (choice === undefined) return null
    if (choice === "__new__") {
      const input = await ui.prompt({
        title: `FastDraw — new file in ${dir}`,
        placeholder: "opencode.jsonc",
      })
      const trimmed = input?.trim()
      if (!trimmed) continue // cancel or empty name → back to the list
      return path.join(dir, trimmed)
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
async function loadPresetFlow(ui: Ui) {
  const name = await pickPreset(ui, "FastDraw — Load Preset")
  if (name === undefined) return
  try {
    const store = await loadPresets()
    const p = store.presets[name]
    if (!p) return
    try {
      // Custom roles only apply when the agent exists on this machine
      // (agents dir, or an OMO target — builtin or user-defined). Missing
      // ones are skipped with a warning — a preset from another machine
      // never fails.
      const omoCfg = await readOmoConfig()
      const present = new Set<string>(await readCustomAgentNames())
      for (const n of Object.keys(omoCfg.targets)) present.add(n)
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
      const omoRouted = (n: string): boolean =>
        omoCfg.parseable && isOmoName(omoCfg, n)
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
      // V2 has no "small" dialog size — medium is the closest match.
      const mode = await ui.select<RestoreMode>({
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
      })
      if (mode === undefined) return
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
      const ok = await ui.confirm({
        title: `Load preset "${name}"? (mode: ${modeLabel})`,
        message:
          `${presetPreview(applyAgents)}\n\n` +
          `This replaces ALL current assignments. Agents not listed revert to defaults.\n\n` +
          `Files to write:\n${planLines.join("\n")}${fallbackBlock}${skippedBlock}${omoBlock}\n\n` +
          `Existing files are backed up as <file>.bak-<timestamp> before writing.`,
      })
      if (!ok) return
      try {
              const state = await loadState()
              const omoWrite: Record<string, ModelEntrySpec> = { ...omoSide }
              const omoReverted: string[] = []
              for (const [n, rec] of Object.entries(state.omo ?? {})) {
                if (n in omoSide) continue
                const upd = omoRevertSpec(omoTargetKind(omoCfg, n), rec)
                if (upd) omoWrite[n] = upd
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
                    original_models:
                      state.omo[n]?.original_models ?? omoCfg.targets[n]?.models ?? null,
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
    } catch (e) {
      flowError(ui, e)
    }
  } catch (e) {
    flowError(ui, e)
  }
}

/* ── Import candidate discovery ───────────────────────────────────── */

/** Filename hint for portable exports / store dumps worth probing. */
const IMPORT_NAME_HINT = /(fastdraw|preset)[^/\\]*\.json$/i

export interface ImportCandidate {
  file: string
  label: string
  mtimeMs: number
}

/** True only for content that is unmistakably fastdraw-shaped: a portable
 *  export (`fastdraw: 1`), a whole store (`presets`), or v2 sections
 *  (`omo`/`custom`). Bare legacy `{agents}` dumps are NOT auto-listed —
 *  they stay reachable via the manual-path fallback. */
function looksLikeFastdrawFile(obj: unknown): boolean {
  if (!obj || typeof obj !== "object" || Array.isArray(obj)) return false
  const o = obj as Record<string, unknown>
  return o.fastdraw === 1 || "presets" in o || "omo" in o || "custom" in o
}

/** One-line preview shown beside each candidate in the picker. */
export function describeImport(parsed: ReturnType<typeof parseImport>, file: string): string {
  if (parsed.kind === "bulk") {
    const names = Object.keys(parsed.presets).sort()
    const total = names.reduce(
      (s, n) => s + Object.keys(presetAgents(parsed.presets[n])).length,
      0,
    )
    return `preset store — ${names.length} preset${names.length === 1 ? "" : "s"}, ${total} bindings: ${names.join(", ")}`
  }
  const name =
    parsed.name ?? path.basename(file, ".json").replace(/^fastdraw-preset-/, "")
  const count = Object.keys(parsed.omo).length + Object.keys(parsed.custom).length
  return `preset "${name}" — ${count} agent${count === 1 ? "" : "s"}`
}

/** Scan the given roots (depth 1) for fastdraw-preset-*.json / preset-ish
 *  JSON files that parse cleanly, newest first. The live presets store and
 *  state file are never offered as import candidates. */
export async function findImportCandidates(
  roots: string[] = [process.cwd(), homedir(), CONFIG_DIR],
  cap = 60,
): Promise<ImportCandidate[]> {
  const seen = new Set<string>()
  const skip = new Set([path.resolve(PRESETS_FILE), path.resolve(STATE_FILE)])
  const out: ImportCandidate[] = []
  for (const root of roots) {
    let names: string[] = []
    try {
      names = await fs.readdir(root)
    } catch {
      continue // root missing/unreadable — not an error, just no candidates there
    }
    for (const name of names.sort()) {
      if (!IMPORT_NAME_HINT.test(name)) continue
      const file = path.resolve(root, name)
      if (skip.has(file) || seen.has(file)) continue
      seen.add(file)
      try {
        const st = await fs.stat(file)
        if (!st.isFile()) continue
        const raw = JSON.parse(await fs.readFile(file, "utf-8"))
        if (!looksLikeFastdrawFile(raw)) continue
        out.push({ file, label: describeImport(parseImport(raw), file), mtimeMs: st.mtimeMs })
      } catch {
        // unreadable or not importable — leave it to the manual path prompt
      }
    }
  }
  out.sort((a, b) => b.mtimeMs - a.mtimeMs)
  return out.slice(0, cap)
}

/** Import one file into the live store. Shared by picker and manual prompt. */
export async function applyImportFile(ui: Ui, file: string): Promise<void> {
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
}

/** Fallback flow: type any path (~ and relative supported). */
async function promptManualImport(ui: Ui) {
  const input = await ui.prompt({
    title: "FastDraw — Import Preset: file path",
    placeholder: "~/preset.json or ./preset.json",
  })
  if (input === undefined) return
  await applyImportFile(ui, expandPath(input.trim()))
}

/** Flow: scan nearby preset files → pick from list → import.
 *  Manual path entry stays as an explicit option (and as the direct
 *  fallback when nothing decodable is found nearby). */
async function importPresetFlow(ui: Ui) {
  try {
    const candidates = await findImportCandidates()
    if (!candidates.length) {
      toast(ui, "FastDraw: no preset files found in workspace/home — enter a path", "info")
      await promptManualImport(ui)
      return
    }
    const choice = await ui.select<string>({
      title: "FastDraw — Import Preset: choose file",
      size: "large",
      options: [
        ...candidates.map((c) => ({
          title: path.basename(c.file),
          value: c.file,
          description: `${c.label}  ·  ${path.dirname(c.file)}`,
        })),
        {
          title: "Other file path…",
          value: "__manual__",
          description: "type an absolute or ~/ path",
        },
      ],
    })
    if (choice === undefined) return
    if (choice === "__manual__") await promptManualImport(ui)
    else await applyImportFile(ui, choice)
  } catch (e) {
    flowError(ui, e)
  }
}

/** Flow: pick preset → prompt output path → write file. */
async function exportPresetFlow(ui: Ui) {
  const name = await pickPreset(ui, "FastDraw — Export Preset")
  if (name === undefined) return
  const store = await loadPresets()
  const p = store.presets[name]
  if (!p) return
  const input = await ui.prompt({
    title: `FastDraw — Export "${name}" to file`,
    value: `fastdraw-preset-${name}.json`,
  })
  if (input === undefined) return
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
    await safeWriteFile(out, JSON.stringify(payload, null, 2))
    toast(ui, `Preset "${name}" exported → ${out}`, "success")
  } catch (e) {
    flowError(ui, e)
  }
}

/** Flow: pick preset → confirm → delete. */
async function deletePresetFlow(ui: Ui) {
  const name = await pickPreset(ui, "FastDraw — Delete Preset")
  if (name === undefined) return
  const probe = await loadPresets()
  const p = probe.presets[name]
  if (!p) return
  const ok = await ui.confirm({
    title: `Delete preset "${name}"?`,
    message: `${Object.keys(presetAgents(p)).length} agent bindings will be removed from the preset store.\nCurrent assignments are NOT affected.`,
  })
  if (!ok) return
  try {
    const store = await loadPresets()
    delete store.presets[name]
    await savePresets(store)
    toast(ui, `Preset "${name}" deleted`, "success")
  } catch (e) {
    flowError(ui, e)
  }
}

/* ── Main Menu ────────────────────────────────────────────────────── */

type MenuAction = "assign" | "save" | "load" | "import" | "export" | "delete"

/** Menu entry for both engines: home-reach advisories → action menu →
 *  dispatch into the flows (shared logic, engine behind the Ui facade). */
async function openFastDraw(ui: Ui): Promise<void> {
  try {
    // Advisory only (home-pollution incident, wiki dev/tools/omo-home-pollution-cadence):
    // never let a check failure block the menu, so it gets its own guard.
    try {
      for (const line of homeReachToastLines(scanHomeReach().warnings)) toast(ui, line, "warning")
    } catch {
      /* home-reach warnings are optional */
    }
    const action = await ui.select<MenuAction>({
      title: "FastDraw — Model Assignments & Presets",
      options: [
        { title: "Assign Model", value: "assign", description: "Bind a model to an agent" },
        { title: "Save Current as Preset", value: "save", description: "Snapshot all current assignments" },
        { title: "Load Preset", value: "load", description: "Preview bindings, then apply" },
        { title: "Import Preset from File", value: "import", description: "Pick a nearby preset file or enter a path" },
        { title: "Export Preset to File", value: "export", description: "Share a preset as JSON" },
        { title: "Delete Preset", value: "delete", description: "Remove a saved preset" },
      ],
    })
    if (action === undefined) return
    ui.close()
    switch (action) {
      case "assign":
        await assignFlow(ui)
        break
      case "save":
        await savePresetFlow(ui)
        break
      case "load":
        await loadPresetFlow(ui)
        break
      case "import":
        await importPresetFlow(ui)
        break
      case "export":
        await exportPresetFlow(ui)
        break
      case "delete":
        await deletePresetFlow(ui)
        break
    }
  } catch (e) {
    flowError(ui, e)
  }
}

/* ── Plugin entry — V2 definition + V1 `tui` bridge ───────────────── */

/** V1 entry (opencode ≥1.18.29 <2): legacy plugin function, called by the
 *  old loader via the `tui` key on the default export. */
const tui = async function fastdrawTui(api: any): Promise<void> {
  const ui = v1Ui(api)
  const open = () => {
    void openFastDraw(ui).catch((e) => flowError(ui, e))
  }

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
            run: open,
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
        onSelect: open,
      },
    ])
  }
}

/** V2 entry (opencode ≥2): definition consumed by @opencode/plugin/tui.
 *  keymap.layer supplies the default keybind, the palette entry, and the
 *  /fastdraw slash command in one layer. Default key is <leader>f, NOT the
 *  V1-era <leader>m: 2.x ships <leader>m as the built-in model.list
 *  ("List available models", verified in the 2.3.0 binary keybind table) —
 *  claiming it would shadow the engine. Users rebind via config keybinds id
 *  "fastdraw.open". */
const v2Definition = Plugin.define({
  id: "fastdraw",
  setup(ctx: any) {
    const ui = v2Ui(ctx)
    ctx.keymap.layer(() => ({
      mode: "global",
      commands: [
        {
          id: "fastdraw.open",
          title: "FastDraw",
          description: "Model switching & presets for agents",
          group: "FastDraw",
          bind: "<leader>f",
          palette: true,
          slash: { name: "fastdraw" },
          run: () => openFastDraw(ui),
        },
      ],
      bindings: ["fastdraw.open"],
    }))
  },
})

/* Dual exit: V2 loads { id, setup } off the default export; V1 ≥1.18.29
 * finds the extra `tui` key next to it (same bridge shape the server half
 * uses with `server`). */
export default { ...v2Definition, tui }
