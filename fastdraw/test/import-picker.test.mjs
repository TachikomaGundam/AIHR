/* FastDraw TUI import-picker tests — run via `npm test` (pretest bundles tui.ts
   into test/.build/tui.mjs). HOME is redirected to a temp dir so the live
   presets store is never touched; import round-trips land in the temp store. */
import assert from "node:assert"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"

process.env.HOME = "/tmp/fastdraw-import-test"
await fs.rm("/tmp/fastdraw-import-test", { recursive: true, force: true })
await fs.mkdir("/tmp/fastdraw-import-test", { recursive: true })

const tmp = process.env.HOME
const ws = path.join(tmp, "ws")
const cfg = path.join(tmp, ".config", "opencode")
await fs.mkdir(ws, { recursive: true })
await fs.mkdir(cfg, { recursive: true })

const mod = await import(new URL("./.build/tui.mjs", import.meta.url))
const { findImportCandidates, describeImport, applyImportFile } = mod

const exportPayload = (name) => ({
  fastdraw: 1,
  schemaVersion: 2,
  name,
  omo: { oracle: { model: "local-qwen/qwen3.8-flash-next" } },
  custom: { "pcb-architect": { model: "bailian/qwen3.7-plus" } },
})

await fs.writeFile(path.join(ws, "fastdraw-preset-Alpha.json"), JSON.stringify(exportPayload("Alpha")))
await fs.writeFile(
  path.join(ws, "fastdraw-preset-Bulk.json"),
  JSON.stringify({ presets: { Beta: exportPayload("Beta"), Gamma: exportPayload("Gamma") } }),
)
await fs.writeFile(path.join(ws, "fastdraw-preset-NoName.json"), JSON.stringify({
  fastdraw: 1, omo: { atlas: { model: "p/m" } }, custom: {},
}))
await fs.writeFile(path.join(cfg, "fastdraw-presets.json"), JSON.stringify({ presets: {} }))
await fs.writeFile(path.join(cfg, ".fastdraw.json"), JSON.stringify({ agents: { oracle: "a/b" } }))
await fs.writeFile(path.join(ws, "package.json"), JSON.stringify({ name: "unrelated" }))
await fs.writeFile(path.join(ws, "random-preset-notes.json"), "not json {{{")
await fs.writeFile(path.join(tmp, "legacy-preset.json"), JSON.stringify({ agents: { oracle: "a/b" } }))

const toasts = []
const ui = { toast: (o) => toasts.push(o) }

const candidates = await findImportCandidates([ws, tmp, cfg])
const names = candidates.map((c) => path.basename(c.file)).sort()

assert.deepEqual(names, [
  "fastdraw-preset-Alpha.json",
  "fastdraw-preset-Bulk.json",
  "fastdraw-preset-NoName.json",
], `unexpected candidates: ${names.join(",")}`)

assert.ok(!candidates.some((c) => c.file.endsWith("fastdraw-presets.json")), "live store must not be offered")
assert.ok(!candidates.some((c) => c.file.endsWith(".fastdraw.json")), "state file must not be offered")
assert.ok(!candidates.some((c) => c.file.endsWith("legacy-preset.json")), "bare {agents} dump must not be auto-listed")
assert.ok(!candidates.some((c) => c.file.includes("random")), "unparseable file must not be listed")

const alpha = candidates.find((c) => c.file.endsWith("Alpha.json"))
assert.match(alpha.label, /preset "Alpha" — 2 agents/)
const bulk = candidates.find((c) => c.file.endsWith("Bulk.json"))
assert.match(bulk.label, /preset store — 2 presets, 4 bindings: Beta, Gamma/)
const noname = candidates.find((c) => c.file.endsWith("NoName.json"))
assert.match(noname.label, /preset "NoName" — 1 agent$/)

const future = new Date(Date.now() + 60_000)
await fs.utimes(path.join(ws, "fastdraw-preset-Alpha.json"), future, future)
const again = await findImportCandidates([ws, tmp, cfg])
assert.equal(path.basename(again[0].file), "fastdraw-preset-Alpha.json", "newest must sort first")

const dedup = await findImportCandidates([ws, ws, tmp, cfg])
assert.equal(dedup.length, again.length, "roots overlap must dedupe")

await applyImportFile(ui, path.join(tmp, "does-not-exist.json"))
assert.ok(toasts.some((t) => t.variant === "error"), "missing file must surface an error toast")
assert.equal(await fs.readFile(path.join(cfg, "fastdraw-presets.json"), "utf-8"), JSON.stringify({ presets: {} }),
  "failed import must not write the store")

toasts.length = 0
await applyImportFile(ui, path.join(ws, "fastdraw-preset-Alpha.json"))
const store = JSON.parse(await fs.readFile(path.join(cfg, "fastdraw-presets.json"), "utf-8"))
assert.ok(store.presets.Alpha, "single import must land in the store")
assert.equal(store.presets.Alpha.omo.oracle.model, "local-qwen/qwen3.8-flash-next")
assert.ok(toasts.some((t) => t.variant === "success" && /"Alpha" imported \(2 agents\)/.test(t.message)))

toasts.length = 0
await applyImportFile(ui, path.join(ws, "fastdraw-preset-Bulk.json"))
const store2 = JSON.parse(await fs.readFile(path.join(cfg, "fastdraw-presets.json"), "utf-8"))
assert.deepEqual(Object.keys(store2.presets).sort(), ["Alpha", "Beta", "Gamma"], "bulk import must merge")
assert.ok(toasts.some((t) => t.variant === "success" && /Imported 2 presets: Beta, Gamma/.test(t.message)))

console.log("import-picker: all assertions passed")
await fs.rm(tmp, { recursive: true, force: true })
