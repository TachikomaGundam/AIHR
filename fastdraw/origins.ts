/**
 * FastDraw — opencode config layering & origin-aware harvest.
 *
 * Single source of truth for WHERE model bindings live (opencode's config
 * precedence order, low → high) and for the portable path scheme used to
 * store binding origins inside presets so they stay shareable across
 * machines. Presets never contain machine-local absolute paths.
 *
 * Precedence (each layer merges onto the previous; later values win):
 *   1. global    ~/.config/opencode/{config.json, opencode.json, opencode.jsonc}
 *   2. env       $OPENCODE_CONFIG                                    (single file)
 *   3. project   opencode.json(c) findUp from cwd → git root, nearest-to-cwd wins
 *   4. .opencode project .opencode/ (farthest first) → ~/.opencode/ →
 *                $OPENCODE_CONFIG_DIR (appended last = wins); within a dir the
 *                json files merge first, then agents/*.md frontmatter (md wins)
 *   5. inline    $OPENCODE_CONFIG_CONTENT  (read-only awareness)
 *   6. managed   /etc/opencode              (read-only awareness)
 *
 * Portable path scheme (stored inside presets / exports, always forward
 * slashes): `${CONFIG_DIR}/...`, `${PROJECT}/...`, `${HOME}/...`. Origin
 * files that cannot be expressed with a placeholder are stored as `null`
 * and fall back to the global config on restore.
 */
import path from "node:path"
import fs from "node:fs/promises"
import { OMO_ROLES } from "./roles.js"

/** Preset schema version. v1 = legacy (flat `agents` map or string-valued
 *  `omo`/`custom` sections); v2 = per-role `{model, origin}` entries. */
export const SCHEMA_VERSION = 2

/* ── Portable paths ───────────────────────────────────────────────── */

export interface PathRoots {
  home: string
  configDir: string
  projectRoot?: string
}

const PLACEHOLDERS: ReadonlyArray<readonly [string, (r: PathRoots) => string | undefined]> = [
  ["${CONFIG_DIR}", (r) => r.configDir],
  ["${PROJECT}", (r) => r.projectRoot],
  ["${HOME}", (r) => r.home],
]

/** Convert an absolute path to its portable placeholder form (`/`-separated),
 *  or `null` when it lives outside every known root. */
export function toPortablePath(abs: string, roots: PathRoots): string | null {
  for (const [ph, get] of PLACEHOLDERS) {
    const root = get(roots)
    if (!root) continue
    const rel = path.relative(root, abs)
    if (rel === "") return ph
    if (rel && !rel.startsWith("..") && !path.isAbsolute(rel)) {
      return `${ph}/${rel.split(path.sep).join("/")}`
    }
  }
  return null
}

/** Resolve a portable (placeholder) path against THIS machine's roots.
 *  Returns `null` when a placeholder cannot be resolved (e.g. `${PROJECT}`
 *  with no project context) — callers fall back to the global config. */
export function resolvePortablePath(
  port: string,
  roots: PathRoots,
  cwd = process.cwd(),
): string | null {
  for (const [ph, get] of PLACEHOLDERS) {
    if (port === ph) return get(roots) ?? null
    if (port.startsWith(`${ph}/`)) {
      const root = get(roots)
      if (!root) return null
      return path.join(root, ...port.slice(ph.length + 1).split("/"))
    }
  }
  // Literal: platform-native absolute (or relative) path — resolve defensively.
  return path.isAbsolute(port) ? port : path.resolve(cwd, port)
}

export function isPortablePath(p: string | null | undefined): p is string {
  return typeof p === "string" && p.startsWith("${")
}

/* ── JSONC parsing (quote-aware comment stripping) ────────────────── */

/** Strip line and block comments from JSONC text (state machine, quote-aware). */
export function stripJsonComments(s: string): string {
  let out = ""
  let inStr = false
  let i = 0
  while (i < s.length) {
    const c = s[i]
    const n = s[i + 1]
    if (inStr) {
      out += c
      if (c === "\\") {
        out += n ?? ""
        i += 2
        continue
      }
      if (c === '"') inStr = false
      i++
      continue
    }
    if (c === '"') {
      inStr = true
      out += c
      i++
      continue
    }
    if (c === "/" && n === "/") {
      while (i < s.length && s[i] !== "\n") i++
      continue
    }
    if (c === "/" && n === "*") {
      i += 2
      while (i < s.length && !(s[i] === "*" && s[i + 1] === "/")) i++
      i += 2
      continue
    }
    out += c
    i++
  }
  return out
}

/** Parse JSONC text as JSON (comments stripped, trailing commas unsupported). */
export function parseJsonc(text: string): unknown {
  return JSON.parse(stripJsonComments(text))
}

/* ── Agent .md frontmatter ────────────────────────────────────────── */

export function parseFrontmatterModel(text: string): string | null {
  const fm = text.match(/^---\s*\n([\s\S]*?)\n---/m)
  const m = fm?.[1].match(/^[ \t]*model[ \t]*:[ \t]*"?([^"#\s]+)"?/m)
  return m && m[1].includes("/") ? m[1] : null
}

/* ── Layer collection & harvest ───────────────────────────────────── */

export type LayerKind = "json" | "jsonc" | "md"

export interface LayerFile {
  /** Precedence group: global | env | project | .opencode | inline | managed */
  layer: string
  /** Absolute path; `null` only for inline env content. */
  file: string | null
  portable: string | null
  kind: LayerKind
  /** Pre-fetched content (inline layer only). */
  text?: string
}

export interface HarvestEnv {
  OPENCODE_CONFIG?: string
  OPENCODE_CONFIG_DIR?: string
  OPENCODE_CONFIG_CONTENT?: string
}

export interface HarvestCtx extends PathRoots {
  cwd: string
  env: HarvestEnv
}

async function exists(p: string): Promise<boolean> {
  try {
    await fs.access(p)
    return true
  } catch {
    return false
  }
}

/** Nearest ancestor of `cwd` containing `.git`; falls back to `cwd` itself
 *  so the project layer still anchors when the repo is not a git checkout. */
export async function findProjectRoot(cwd: string): Promise<string> {
  let dir = path.resolve(cwd)
  for (;;) {
    if (await exists(path.join(dir, ".git"))) return dir
    const parent = path.dirname(dir)
    if (parent === dir) return path.resolve(cwd)
    dir = parent
  }
}

/** Layers in precedence order (low → high). */
export async function collectLayerFiles(ctx: HarvestCtx): Promise<LayerFile[]> {
  const out: LayerFile[] = []
  const add = (layer: string, file: string, kind: LayerKind): void => {
    out.push({ layer, file, portable: toPortablePath(file, ctx), kind })
  }
  /** Within a dir: json files merge first, then agents/*.md frontmatter. */
  const addDir = async (dir: string): Promise<void> => {
    for (const f of ["opencode.json", "opencode.jsonc"] as const) {
      const p = path.join(dir, f)
      if (await exists(p)) add(".opencode", p, f === "opencode.jsonc" ? "jsonc" : "json")
    }
    const agentsDir = path.join(dir, "agents")
    try {
      const names = (await fs.readdir(agentsDir))
        .filter((n) => n.endsWith(".md"))
        .sort()
      for (const n of names) add(".opencode", path.join(agentsDir, n), "md")
    } catch {
      /* no agents dir in this .opencode */
    }
  }

  // 1. Global dir — three files merge in this order (jsonc wins).
  for (const f of ["config.json", "opencode.json", "opencode.jsonc"] as const) {
    const p = path.join(ctx.configDir, f)
    if (await exists(p)) add("global", p, f === "opencode.jsonc" ? "jsonc" : "json")
  }
  // 2. $OPENCODE_CONFIG single file.
  if (ctx.env.OPENCODE_CONFIG) {
    const p = path.resolve(ctx.cwd, ctx.env.OPENCODE_CONFIG)
    if (await exists(p)) add("env", p, p.endsWith(".jsonc") ? "jsonc" : "json")
  }
  // 3. Project files: findUp from cwd to git root; farther-from-cwd first so
  //    the nearest wins. Within a dir opencode.json merges before opencode.jsonc.
  const projectRoot = ctx.projectRoot ?? (await findProjectRoot(ctx.cwd))
  const projDirs: string[] = []
  for (let d = ctx.cwd; ; d = path.dirname(d)) {
    projDirs.push(d)
    if (d === projectRoot) break
    const parent = path.dirname(d)
    if (parent === d) break
  }
  for (const dir of [...projDirs].reverse()) {
    for (const f of ["opencode.json", "opencode.jsonc"] as const) {
      const p = path.join(dir, f)
      if (await exists(p)) add("project", p, f === "opencode.jsonc" ? "jsonc" : "json")
    }
  }
  // 4. .opencode dirs: project walk-up (farthest first) → home → $OPENCODE_CONFIG_DIR.
  for (const dir of projDirs) await addDir(path.join(dir, ".opencode"))
  await addDir(path.join(ctx.home, ".opencode"))
  if (ctx.env.OPENCODE_CONFIG_DIR) {
    await addDir(path.resolve(ctx.cwd, ctx.env.OPENCODE_CONFIG_DIR))
  }
  // 5. Inline content (read-only awareness — no file to write back to).
  if (ctx.env.OPENCODE_CONFIG_CONTENT) {
    out.push({
      layer: "inline",
      file: null,
      portable: null,
      kind: "json",
      text: ctx.env.OPENCODE_CONFIG_CONTENT,
    })
  }
  // 6. Managed dir (read-only awareness).
  if (process.platform !== "win32") {
    for (const f of ["opencode.json", "opencode.jsonc"] as const) {
      const p = path.join("/etc/opencode", f)
      if (await exists(p)) add("managed", p, f === "opencode.jsonc" ? "jsonc" : "json")
    }
  }
  return out
}

/** Read the role→model map a single layer file contributes. Best-effort:
 *  any read/parse failure yields {} for that file. */
export async function readLayerModels(lf: LayerFile): Promise<Record<string, string>> {
  try {
    if (lf.kind === "md") {
      if (lf.file === null) return {}
      const text = await fs.readFile(lf.file, "utf-8")
      const m = parseFrontmatterModel(text)
      return m ? { [path.basename(lf.file, ".md")]: m } : {}
    }
    const text = lf.file === null ? (lf.text ?? "") : await fs.readFile(lf.file, "utf-8")
    const cfg = (lf.kind === "jsonc" ? parseJsonc(text) : JSON.parse(text)) as Record<
      string,
      unknown
    > | null
    const agent = (cfg && typeof cfg === "object" ? (cfg.agent ?? {}) : {}) as Record<
      string,
      unknown
    >
    const out: Record<string, string> = {}
    for (const [name, a] of Object.entries(agent)) {
      const m = typeof a === "string" ? a : (a as { model?: unknown } | null)?.model
      if (typeof m === "string" && m.includes("/")) out[name] = m
    }
    return out
  } catch {
    return {}
  }
}

/* ── Harvest: effective bindings + conflicts ──────────────────────── */

export interface BindingOrigin {
  layer: string
  /** Portable path (placeholder form); null when not expressible. */
  file: string | null
}

export interface Binding {
  model: string
  origin?: BindingOrigin
}

export interface ConflictEntry {
  layer: string
  portable: string | null
  model: string
}

export interface Conflict {
  role: string
  entries: ConflictEntry[]
  winner: string
}

export interface HarvestResult {
  /** Effective binding per role (last-write-wins across the layer order). */
  agents: Record<string, Binding>
  /** Roles bound to DIFFERENT models in multiple locations. */
  conflicts: Conflict[]
}

/** Scan every layer in precedence order and compute the effective binding
 *  landscape together with cross-layer conflicts. Never throws: unreadable
 *  files are skipped. */
export async function harvest(ctx: HarvestCtx): Promise<HarvestResult> {
  const files = await collectLayerFiles(ctx)
  const perRole: Record<string, ConflictEntry[]> = {}
  const effective: Record<string, string> = {}
  const winnerOrigin: Record<string, BindingOrigin> = {}
  for (const lf of files) {
    const models = await readLayerModels(lf)
    for (const [name, model] of Object.entries(models)) {
      const entry: ConflictEntry = {
        layer: lf.layer,
        portable: lf.portable,
        model,
      }
      ;(perRole[name] ??= []).push(entry)
      effective[name] = model
      winnerOrigin[name] = { layer: lf.layer, file: lf.portable }
    }
  }
  const agents: Record<string, Binding> = {}
  for (const [name, model] of Object.entries(effective)) {
    agents[name] = { model, origin: winnerOrigin[name] }
  }
  const conflicts: Conflict[] = []
  for (const [role, entries] of Object.entries(perRole)) {
    if (new Set(entries.map((e) => e.model)).size > 1) {
      conflicts.push({ role, entries, winner: effective[role] })
    }
  }
  conflicts.sort((a, b) => a.role.localeCompare(b.role))
  return { agents, conflicts }
}

/** Human-readable conflict report (portable paths, winner marked). Empty
 *  string when there is nothing to report. */
export function formatConflictReport(conflicts: Conflict[]): string {
  if (!conflicts.length) return ""
  const lines = [
    "⚠ Conflicts — same role bound to different models across config layers:",
  ]
  for (const c of conflicts) {
    lines.push(`  ${c.role}`)
    for (const e of c.entries) {
      const loc = e.portable ?? `(${e.layer}, no portable path)`
      const mark = e.model === c.winner ? "  ← wins (highest precedence)" : ""
      lines.push(`    ${loc} → ${e.model}${mark}`)
    }
  }
  return `\n${lines.join("\n")}`
}

/* ── Section/binding value normalization (v1 ↔ v2) ────────────────── */

export type SectionEntry = string | Binding

/** Normalize one preset section entry: legacy string `"prov/model"` or v2
 *  `{"model": "prov/model", "origin": ...}`. Invalid → null. */
export function entryModel(e: unknown): Binding | null {
  if (typeof e === "string") {
    return e.includes("/") ? { model: e } : null
  }
  if (e && typeof e === "object" && !Array.isArray(e)) {
    const o = e as Record<string, unknown>
    if (typeof o.model === "string" && o.model.includes("/")) {
      let origin: BindingOrigin | undefined
      const g = o.origin as Record<string, unknown> | null | undefined
      if (g && typeof g === "object" && !Array.isArray(g) && typeof g.layer === "string") {
        origin = { layer: g.layer, file: typeof g.file === "string" ? g.file : null }
      }
      return { model: o.model, origin }
    }
  }
  return null
}

export function sectionToBindings(s: Record<string, unknown>): Record<string, Binding> {
  const out: Record<string, Binding> = {}
  for (const [n, e] of Object.entries(s)) {
    const b = entryModel(e)
    if (b) out[n] = b
  }
  return out
}

export function bindingsToFlat(b: Record<string, Binding>): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [n, e] of Object.entries(b)) out[n] = e.model
  return out
}

/** Split a binding map into standard OMO roles vs custom roles. */
export function splitBindings(
  b: Record<string, Binding>,
): { omo: Record<string, Binding>; custom: Record<string, Binding> } {
  const omo: Record<string, Binding> = {}
  const custom: Record<string, Binding> = {}
  for (const [n, e] of Object.entries(b)) {
    if (OMO_ROLES.has(n)) omo[n] = e
    else custom[n] = e
  }
  return { omo, custom }
}

/* ── Write side: backups, atomic writes, surgical JSONC editing ───── */
/* Restores must never corrupt user config: every existing file gets a
 * timestamped `<file>.bak-YYYYMMDD-HHMMSS` sibling before it is touched
 * (name is Windows-safe — no colons), and replaced content is written to
 * a temp file first and renamed into place. JSONC edits are token-aware
 * so comments, whitespace and untouched keys survive. */

function pad2(n: number): string {
  return n < 10 ? `0${n}` : String(n)
}

/** Timestamped backup path for `file` (the file may or may not exist). */
export function backupName(file: string, now = new Date()): string {
  const stamp =
    `${now.getFullYear()}${pad2(now.getMonth() + 1)}${pad2(now.getDate())}-` +
    `${pad2(now.getHours())}${pad2(now.getMinutes())}${pad2(now.getSeconds())}`
  return `${file}.bak-${stamp}`
}

/** Copy `file` to its timestamped backup; returns the backup path, or
 *  `null` when the file does not exist (nothing to protect). */
export async function backupIfExists(file: string, now = new Date()): Promise<string | null> {
  try {
    await fs.access(file)
  } catch {
    return null
  }
  const bak = backupName(file, now)
  await fs.copyFile(file, bak)
  return bak
}

/** Write `content` to `file` atomically (same-dir temp file + rename),
 *  creating parent directories as needed. */
export async function writeFileAtomic(file: string, content: string): Promise<void> {
  const dir = path.dirname(file)
  await fs.mkdir(dir, { recursive: true })
  const tmp = path.join(
    dir,
    `.fastdraw-tmp-${process.pid}-${Math.random().toString(36).slice(2)}`,
  )
  await fs.writeFile(tmp, content)
  try {
    await fs.rename(tmp, file)
  } catch (e) {
    await fs.rm(tmp, { force: true })
    throw e
  }
}

/** stripJsonComments variant that also records, per cleaned char, the
 *  original index it came from — used to map edit spans back onto the
 *  raw file text. */
function stripWithMap(s: string): { clean: string; orig: Int32Array } {
  let out = ""
  const orig: number[] = []
  let inStr = false
  let i = 0
  while (i < s.length) {
    const c = s[i]
    const n = s[i + 1]
    if (inStr) {
      out += c
      orig.push(i)
      if (c === "\\") {
        if (n !== undefined) {
          out += n
          orig.push(i + 1)
        }
        i += 2
        continue
      }
      if (c === '"') inStr = false
      i++
      continue
    }
    if (c === '"') {
      inStr = true
      out += c
      orig.push(i)
      i++
      continue
    }
    if (c === "/" && n === "/") {
      while (i < s.length && s[i] !== "\n") i++
      continue
    }
    if (c === "/" && n === "*") {
      i += 2
      while (i < s.length && !(s[i] === "*" && s[i + 1] === "/")) i++
      i += 2
      continue
    }
    out += c
    orig.push(i)
    i++
  }
  return { clean: out, orig: new Int32Array(orig) }
}

/** Drop trailing commas (outside strings) so the editor tolerates them. */
function stripTrailingCommas(s: string): string {
  let out = ""
  let inStr = false
  let i = 0
  while (i < s.length) {
    const c = s[i]
    if (inStr) {
      out += c
      if (c === "\\") {
        out += s[i + 1] ?? ""
        i += 2
        continue
      }
      if (c === '"') inStr = false
      i++
      continue
    }
    if (c === '"') {
      inStr = true
      out += c
      i++
      continue
    }
    if (c === ",") {
      let j = i + 1
      while (j < s.length && /[ \t\r\n]/.test(s[j])) j++
      if (s[j] === "}" || s[j] === "]") {
        i++
        continue
      }
    }
    out += c
    i++
  }
  return out
}

function parseLenient(text: string): unknown {
  return JSON.parse(stripTrailingCommas(stripJsonComments(text)))
}

interface KeyHit {
  key: string
  colon: number
  depth: number
}

/** Match the container (`{}`/`[]`) opened at `openIdx`; returns its end
 *  (exclusive). Strings inside are skipped. */
function matchContainer(clean: string, openIdx: number): { s: number; e: number } {
  const closers: Record<string, string> = { "{": "}", "[": "]", "(": ")" }
  const open = clean[openIdx]
  const closer = closers[open] ?? "}"
  let depth = 0
  for (let i = openIdx; i < clean.length; i++) {
    const c = clean[i]
    if (c === '"') {
      i++
      while (i < clean.length) {
        const ch = clean[i]
        if (ch === "\\") {
          i += 2
          continue
        }
        if (ch === '"') break
        i++
      }
      continue
    }
    if (c === open) depth++
    else if (c === closer) {
      depth--
      if (depth === 0) return { s: openIdx, e: i + 1 }
    }
  }
  throw new Error(`unbalanced "${open}" in config`)
}

/** Scan cleaned text for every `"key":` hit with its brace depth. */
function scanKeys(clean: string): { hits: KeyHit[]; rootRbrace: number } {
  const hits: KeyHit[] = []
  let rootRbrace = -1
  let depth = 0
  let i = 0
  while (i < clean.length) {
    const c = clean[i]
    if (c === '"') {
      i++
      let k = ""
      while (i < clean.length) {
        const ch = clean[i]
        if (ch === "\\") {
          k += clean[i + 1] ?? ""
          i += 2
          continue
        }
        if (ch === '"') {
          i++
          break
        }
        k += ch
        i++
      }
      let j = i
      while (j < clean.length && /[ \t\r\n]/.test(clean[j])) j++
      if (clean[j] === ":") {
        hits.push({ key: k, colon: j, depth })
        i = j + 1
        continue
      }
      continue
    }
    if (c === "{" || c === "[" || c === "(") {
      if (depth === 0 && c === "{") rootRbrace = -1
      depth++
      i++
      continue
    }
    if (c === "}" || c === "]" || c === ")") {
      depth--
      if (depth === 0 && c === "}") rootRbrace = i
      i++
      continue
    }
    i++
  }
  return { hits, rootRbrace }
}

/** Span (in clean space) of the value after `colon`. */
function valueSpan(clean: string, colon: number): { s: number; e: number } {
  let i = colon + 1
  while (i < clean.length && /[ \t\r\n]/.test(clean[i])) i++
  if (i >= clean.length) throw new Error("missing value in config")
  const c = clean[i]
  if (c === "{" || c === "[" || c === "(") return matchContainer(clean, i)
  if (c === '"') {
    let j = i + 1
    while (j < clean.length) {
      const ch = clean[j]
      if (ch === "\\") {
        j += 2
        continue
      }
      if (ch === '"') return { s: i, e: j + 1 }
      j++
    }
    throw new Error("unterminated string in config")
  }
  let j = i + 1
  while (j < clean.length && !",}]".includes(clean[j])) j++
  return { s: i, e: j }
}

/** One entry write: a bare model string, or a spec also carrying the FULL
 *  desired `models` array (OMO's dominant field for categories — when the
 *  array exists, `models[0]` wins over `model` at runtime, so callers that
 *  rebind a category must keep the array consistent). `models` = explicit
 *  replacement; leave undefined to keep/normalize whatever the file holds. */
export type ModelEntrySpec = string | { model: string; models?: unknown[] }

export function specModel(spec: ModelEntrySpec): string {
  return typeof spec === "string" ? spec : spec.model
}

function specModels(spec: ModelEntrySpec): unknown[] | undefined {
  return typeof spec === "string" ? undefined : spec.models
}

/** Serialize one role entry, preserving the legacy shape (bare model
 *  string) when the pre-existing value was a string. `models` (whole-entry
 *  rewrite path): explicit array replacement, else — with
 *  `normalizeModelsArray` — collapse an existing `models[]` to `[model]` so
 *  the dominant field can never shadow the freshly written `model`. */
function entryJson(
  oldVal: unknown,
  model: string,
  models?: unknown[],
  normalizeModelsArray?: boolean,
): string {
  if (typeof oldVal === "string") return JSON.stringify(model)
  const base =
    oldVal && typeof oldVal === "object" && !Array.isArray(oldVal)
      ? { ...(oldVal as Record<string, unknown>) }
      : {}
  if (models !== undefined) {
    if ("models" in base || models.length > 0) base.models = models
  } else if (normalizeModelsArray && Array.isArray(base.models)) {
    base.models = [model]
  }
  return JSON.stringify({ ...base, model }, null, 2)
}

const WS_RE = /[ \t\r\n]/

/** Index in `clean` right before `braceIdx` (the closing brace of a
 *  container) skipping any trailing whitespace — i.e. the insertion point
 *  for new entries and their comma. */
function insertBeforeClose(clean: string, braceIdx: number): number {
  let i = braceIdx - 1
  while (i > 0 && WS_RE.test(clean[i])) i--
  return i + 1
}

const sp2 = (n: number): string => " ".repeat(n)

/** One new leaf entry member: `"name": { "model": "…" }` at `indent`. */
function entryBodyText(name: string, spec: ModelEntrySpec, indent: number): string {
  const model = specModel(spec)
  const models = specModels(spec)
  const inner = models
    ? `${sp2(indent + 2)}"model": ${JSON.stringify(model)},\n${sp2(indent + 2)}"models": ${JSON.stringify(models)}\n`
    : `${sp2(indent + 2)}"model": ${JSON.stringify(model)}\n`
  return `${sp2(indent)}${JSON.stringify(name)}: {\n${inner}${sp2(indent)}}`
}

/** New-entries member block (no braces), newline-prefixed, comma-joined. */
function entriesBody(updates: Record<string, ModelEntrySpec>, indent: number): string {
  return Object.entries(updates)
    .map(([name, spec]) => `\n${entryBodyText(name, spec, indent)}`)
    .join(",")
}

/** Nested member text for `keys` (outermost first) wrapping new model
 *  entries, formatted for insertion into an existing container. Starts with
 *  a newline; single-key output matches the legacy `setAgentModelsJsonc`
 *  creation format byte for byte. */
function chainMembersText(keys: string[], updates: Record<string, ModelEntrySpec>): string {
  let inner = entriesBody(updates, 2 * (keys.length + 1))
  for (let i = keys.length - 1; i >= 0; i--) {
    const sp = sp2(2 * (i + 1))
    inner = `\n${sp}${JSON.stringify(keys[i])}: {${inner}\n${sp}}`
  }
  return inner
}

/** Set role models inside the container object addressed by a chain of
 *  string keys from the root (e.g. `["[opencode]", "agents"]`). ONLY the
 *  touched nodes are rewritten — comments, whitespace and untouched keys
 *  are preserved verbatim. Missing chain levels are created (a level that
 *  exists but holds a non-object value is replaced by an object); names
 *  absent from the container are appended. Throws when the text is not a
 *  JSON object or the result would not parse.
 *
 *  `normalizeModelsArray`: an entry's existing `"models"` array is OMO's
 *  dominant field for categories (`models[0]` beats `model` at runtime), so
 *  rebinding without touching it silently fails. When set, every updated
 *  entry whose `models` value is an array gets that array collapsed to
 *  `[model]` — unless the spec itself carries an explicit `models` array,
 *  which is written verbatim (restore path). */
export function setNestedModelsJsonc(
  text: string,
  containerKeys: string[],
  updates: Record<string, ModelEntrySpec>,
  opts?: { normalizeModelsArray?: boolean },
): string {
  if (!Object.keys(updates).length) return text
  const pre = stripTrailingCommas(text)
  const root = parseLenient(pre)
  if (!root || typeof root !== "object" || Array.isArray(root)) {
    throw new Error("config root is not a JSON object")
  }
  const { clean, orig } = stripWithMap(pre)
  const { hits, rootRbrace } = scanKeys(clean)
  if (rootRbrace < 0) throw new Error("config has no root '{'...'}' pair")
  let rootOpen = 0
  while (rootOpen < clean.length && WS_RE.test(clean[rootOpen])) rootOpen++
  if (clean[rootOpen] !== "{") throw new Error("config root is not a JSON object")

  // Walk the chain down to the deepest existing container object.
  let scope = { s: rootOpen, e: rootRbrace + 1 }
  let missingAt = -1
  let replaceColon = -1
  for (let i = 0; i < containerKeys.length; i++) {
    const hit = [...hits]
      .reverse()
      .find(
        (h) =>
          h.key === containerKeys[i] && h.depth === i + 1 && h.colon > scope.s && h.colon < scope.e,
      )
    if (!hit) {
      missingAt = i
      break
    }
    const vs = valueSpan(clean, hit.colon)
    if (clean[vs.s] !== "{") {
      missingAt = i
      replaceColon = hit.colon
      break
    }
    scope = vs
  }

  const edits: { s: number; e: number; text: string }[] = []
  if (missingAt === -1) {
    // Container exists: rewrite known entries, append unknown ones.
    const entryDepth = containerKeys.length + 1
    const entryColons = new Map(
      hits
        .filter((h) => h.depth === entryDepth && h.colon > scope.s && h.colon < scope.e)
        .map((h) => [h.key, h.colon]),
    )
    const missing: [string, ModelEntrySpec][] = []
    for (const [name, spec] of Object.entries(updates)) {
      const model = specModel(spec)
      const models = specModels(spec)
      const colon = entryColons.get(name)
      if (colon === undefined) {
        missing.push([name, spec])
        continue
      }
      const vs = valueSpan(clean, colon)
      // Entry object with a model field → replace ONLY that value span so
      // inner comments survive; bare strings / objects lacking the field
      // fall back to a whole-entry rewrite.
      const entryKeyHit = (key: string) =>
        clean[vs.s] === "{"
          ? hits
              .filter(
                (h) =>
                  h.key === key &&
                  h.depth === entryDepth + 1 &&
                  h.colon > vs.s &&
                  h.colon < vs.e,
              )
              .pop()
          : undefined
      const modelHit = entryKeyHit("model")
      if (modelHit) {
        const mvs = valueSpan(clean, modelHit.colon)
        edits.push({ s: mvs.s, e: mvs.e, text: JSON.stringify(model) })
        const modelsHit = entryKeyHit("models")
        if (modelsHit) {
          const avs = valueSpan(clean, modelsHit.colon)
          if (Array.isArray(models)) {
            edits.push({ s: avs.s, e: avs.e, text: JSON.stringify(models) })
          } else if (opts?.normalizeModelsArray && clean[avs.s] === "[") {
            edits.push({ s: avs.s, e: avs.e, text: `[${JSON.stringify(model)}]` })
          }
        } else if (Array.isArray(models)) {
          const at = insertBeforeClose(clean, vs.e - 1)
          const pad = sp2(2 * (entryDepth + 1))
          edits.push({ s: at, e: at, text: `,\n${pad}"models": ${JSON.stringify(models)}` })
        }
      } else {
        const oldVal: unknown = parseLenient(clean.slice(vs.s, vs.e))
        edits.push({
          s: vs.s,
          e: vs.e,
          text: entryJson(
            oldVal,
            model,
            Array.isArray(models) ? models : undefined,
            opts?.normalizeModelsArray,
          ),
        })
      }
    }
    if (missing.length) {
      const hasAny = clean.slice(scope.s + 1, scope.e - 1).trim().length > 0
      const body = `${hasAny ? "," : ""}${missing
        .map(([name, s]) => `\n${entryBodyText(name, s, 2 * entryDepth)}`)
        .join(",")}`
      const at = insertBeforeClose(clean, scope.e - 1)
      edits.push({ s: at, e: at, text: body })
    }
  } else {
    // Chain broken: create the remaining levels (or replace the non-object
    // value found there) with all updates inside.
    const rest = containerKeys.slice(missingAt)
    if (replaceColon >= 0) {
      const vs = valueSpan(clean, replaceColon)
      const inner =
        rest.length > 1 ? chainMembersText(rest.slice(1), updates) : entriesBody(updates, 2)
      edits.push({ s: vs.s, e: vs.e, text: `{${inner}\n}` })
    } else {
      const hasAny = clean.slice(scope.s + 1, scope.e - 1).trim().length > 0
      const at = insertBeforeClose(clean, scope.e - 1)
      edits.push({
        s: at,
        e: at,
        text: `${hasAny ? "," : ""}${chainMembersText(rest, updates)}`,
      })
    }
  }

  edits.sort((a, b) => b.s - a.s)
  let out = pre
  for (const ed of edits) {
    const os = ed.s < clean.length ? orig[ed.s] : orig[clean.length - 1] + 1
    const oe = ed.e > ed.s ? orig[ed.e - 1] + 1 : os
    out = out.slice(0, os) + ed.text + out.slice(oe)
  }
  const chk = parseLenient(out)
  if (!chk || typeof chk !== "object" || Array.isArray(chk)) {
    throw new Error("config would not be a JSON object after edit")
  }
  return out
}

/** Set role models inside the root-level `agent` object. Wrapper around
 *  `setNestedModelsJsonc` kept for opencode config editing. */
export function setAgentModelsJsonc(text: string, updates: Record<string, string>): string {
  return setNestedModelsJsonc(text, ["agent"], updates)
}

/** Set (or insert) the `model:` line in an agent .md frontmatter block. */
export function setFrontmatterModel(text: string, model: string): string {
  const m = text.match(/^---\s*\n([\s\S]*?)\n---/m)
  if (!m) return `---\nmodel: ${model}\n---\n${text}`
  const lines = m[1].split("\n")
  const idx = lines.findIndex((l) => /^[ \t]*model[ \t]*:/.test(l))
  if (idx >= 0) {
    lines[idx] = lines[idx].replace(/^([ \t]*)model[ \t]*:.*$/, `$1model: ${model}`)
    const start = m.index
    return text.slice(0, start) + "---\n" + lines.join("\n") + "\n---" + text.slice(start + m[0].length)
  }
  const nl = text.indexOf("\n", m.index)
  const at = nl < 0 ? m.index : nl + 1
  return text.slice(0, at) + `model: ${model}\n` + text.slice(at)
}

/** Fresh config content for a file being created. */
export function newConfigContent(entries: Record<string, string>, kind: LayerKind): string {
  if (kind === "md") {
    const m = Object.entries(entries)[0]
    return m ? `---\nmodel: ${m[1]}\n---\n` : ""
  }
  const body = Object.entries(entries)
    .map(([n, m]) => `\n    "${n}": {\n      "model": "${m}"\n    }`)
    .join(",")
  return `{\n  "agent": {${body}\n  }\n}\n`
}

export function kindFromExt(file: string): LayerKind {
  if (file.endsWith(".md")) return "md"
  if (file.endsWith(".jsonc")) return "jsonc"
  return "json"
}

export type RestoreMode = "global" | "original" | "path"

export interface RestorePlanFile {
  file: string
  kind: LayerKind
  entries: Record<string, string>
  /** Portable form of the file (origin of the source binding), if any. */
  origin: string | null
  create: boolean
}

export interface RestorePlan {
  files: RestorePlanFile[]
  /** Roles with no resolvable origin → written to the global config. */
  fallback: string[]
  /** Roles whose binding lives in FastDraw state (.fastdraw.json) — no
   *  config file write needed. */
  fromState: string[]
  /** Chosen global config target (absolute). */
  globalTarget: string
}

/** Choose the global config target: the highest-precedence EXISTING file in
 *  the config dir (jsonc wins), else a fresh `opencode.jsonc`. */
export async function pickGlobalTarget(configDir: string): Promise<string> {
  for (const f of ["opencode.jsonc", "opencode.json", "config.json"] as const) {
    const p = path.join(configDir, f)
    if (await exists(p)) return p
  }
  return path.join(configDir, "opencode.jsonc")
}

function addEntry(
  files: Map<string, RestorePlanFile>,
  file: string,
  kind: LayerKind,
  origin: string | null,
  exists: boolean,
  role: string,
  model: string,
): void {
  const prev = files.get(file)
  if (prev) {
    prev.entries[role] = model
    return
  }
  files.set(file, { file, kind, entries: { [role]: model }, origin, create: !exists })
}

/** Compute the write plan for restoring a preset's bindings into config
 *  files, WITHOUT writing anything (so callers can preview/confirm).
 *
 *  mode:
 *   - "global"   every binding → the global config file (single target)
 *   - "original" each binding → the file recorded as its origin (portable
 *                path resolved for this machine)
 *   - "path"     every binding → `targetPath` (absolute or cwd-relative)
 *
 *  Roles whose origin is missing/unresolvable fall back to the global
 *  target; roles that came from FastDraw state need no file write. */
export async function planRestore(
  bindings: Record<string, Binding>,
  mode: RestoreMode,
  ctx: HarvestCtx,
  targetPath?: string,
): Promise<RestorePlan> {
  if (mode === "path" && !targetPath) {
    throw new Error("targetPath is required for path restore mode")
  }
  const globalTarget = await pickGlobalTarget(ctx.configDir)
  const roots: PathRoots = {
    home: ctx.home,
    configDir: ctx.configDir,
    projectRoot: ctx.projectRoot ?? (await findProjectRoot(ctx.cwd)),
  }
  const files = new Map<string, RestorePlanFile>()
  const fallback: string[] = []
  const fromState: string[] = []

  for (const [role, b] of Object.entries(bindings)) {
    if (mode === "original" && b.origin?.layer === "state") {
      fromState.push(role)
      continue
    }
    let file: string | null
    let origin: string | null = b.origin?.file ?? null
    if (mode === "global") {
      file = globalTarget
    } else if (mode === "path") {
      file = path.isAbsolute(targetPath as string)
        ? (targetPath as string)
        : path.resolve(ctx.cwd, targetPath as string)
      origin = null
    } else if (b.origin?.file) {
      const port = b.origin.file
      file = isPortablePath(port)
        ? resolvePortablePath(port, roots)
        : path.isAbsolute(port)
          ? port
          : path.resolve(ctx.cwd, port)
    } else {
      file = null
    }
    if (!file) {
      fallback.push(role)
      continue
    }
    const kind = kindFromExt(file)
    if (kind === "md" && path.basename(file, ".md") !== role) {
      fallback.push(role)
      continue
    }
    addEntry(files, file, kind, origin, await exists(file), role, b.model)
  }

  if (fallback.length) {
    const kind = kindFromExt(globalTarget)
    const f = files.get(globalTarget)
    if (f) {
      for (const r of fallback) f.entries[r] = bindings[r].model
    } else {
      files.set(globalTarget, {
        file: globalTarget,
        kind,
        entries: Object.fromEntries(fallback.map((r) => [r, bindings[r].model])),
        origin: null,
        create: !(await exists(globalTarget)),
      })
    }
  }
  return { files: [...files.values()], fallback, fromState, globalTarget }
}

export interface RestoreWriteOutcome {
  file: string
  kind: LayerKind
  backup: string | null
  written: boolean
  /** Set when the write failed (file left untouched; backup kept). */
  error?: string
}

/** Execute a restore plan: back up each target file, then surgically apply
 *  the role models. Failures are per-file and do not abort the batch. */
export async function restoreWrite(
  plan: RestorePlan,
  now = new Date(),
): Promise<RestoreWriteOutcome[]> {
  const out: RestoreWriteOutcome[] = []
  for (const f of plan.files) {
    const oc: RestoreWriteOutcome = { file: f.file, kind: f.kind, backup: null, written: false }
    try {
      const bak = await backupIfExists(f.file, now)
      oc.backup = bak
      let content: string
      if (bak !== null) {
        const existing = await fs.readFile(f.file, "utf-8")
        content =
          f.kind === "md"
            ? setFrontmatterModel(existing, Object.values(f.entries)[0] ?? "")
            : setAgentModelsJsonc(existing, f.entries)
      } else {
        content = newConfigContent(f.entries, f.kind)
      }
      await writeFileAtomic(f.file, content)
      oc.written = true
    } catch (e) {
      oc.error = e instanceof Error ? e.message : String(e)
    }
    out.push(oc)
  }
  return out
}