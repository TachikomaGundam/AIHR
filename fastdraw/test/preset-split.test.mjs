/* FastDraw preset split (omo/custom) + stale-state save bug tests.
   Run via `npm test` (pretest transpiles server.ts with esbuild into
   test/.build/). HOME is redirected to a temp dir so no real opencode state
   is touched. */
import assert from "node:assert"
import fs from "node:fs/promises"
import path from "node:path"

const HOME = "/tmp/fastdraw-test-split"
process.env.HOME = HOME
await fs.rm(HOME, { recursive: true, force: true })
await fs.mkdir(HOME, { recursive: true })
// Isolate the origin harvest: cwd must not be inside the repo.
process.chdir(HOME)

const STATE = path.join(HOME, ".config/opencode/.fastdraw.json")
const PRESETS = path.join(HOME, ".config/opencode/fastdraw-presets.json")

const mod = await import(new URL("./.build/server.mjs", import.meta.url))
const origins = await import(new URL("./.build/origins.mjs", import.meta.url))

let pass = 0
const ok = (name) => { pass++; console.log(`PASS ${name}`) }

/* ── Bug A repro ─────────────────────────────────────────────────────
   The TUI flow (`assignFlow` in tui.ts) and `hr apply --set-state` write
   .fastdraw.json directly to disk. The server process only reads it ONCE at
   boot — so fastdraw_save_preset must not judge emptiness from its stale
   in-memory copy. */
{
  const server = await mod.default.server()
  const t = server.tool

  // server boots; no assignments anywhere yet
  let r = await t.fastdraw_save_preset.execute({ name: "nowhere" })
  assert.match(r, /no assignments to save/)

  // external writer (TUI / hr apply) puts bindings on disk AFTER boot
  await fs.mkdir(path.dirname(STATE), { recursive: true })
  await fs.writeFile(STATE, JSON.stringify({ agents: { oracle: "prov/model-a" } }))

  r = await t.fastdraw_save_preset.execute({ name: "ext" })
  assert.match(r, /preset "ext" saved \(1 agents?\)/, "save_preset must see on-disk assignments")
  const store = JSON.parse(await fs.readFile(PRESETS, "utf-8"))
  assert.equal(store.presets.ext.schemaVersion, 2)
  assert.equal(store.presets.ext.omo.oracle.model, "prov/model-a")
  assert.equal(store.presets.ext.omo.oracle.origin.layer, "state")
  assert.equal(store.presets.ext.omo.oracle.origin.file, "${CONFIG_DIR}/.fastdraw.json")
  assert.deepEqual(store.presets.ext.custom, {})
  ok("bug A: save_preset uses fresh on-disk state, not stale boot-time copy")
}

/* ── Two-section save (omo + custom split) ─────────────────────────── */
{
  const server = await mod.default.server()
  const t = server.tool
  const cfg = { agent: { oracle: { model: "orig/o" }, "pcb-router": { model: "orig/p" } } }
  await server.config(cfg)

  await t.fastdraw_assign.execute({ agent: "oracle", model: "prov/omo-model" })
  await t.fastdraw_assign.execute({ agent: "pcb-router", model: "prov/custom-model" })

  const r = await t.fastdraw_save_preset.execute({ name: "mixed", description: "omo+custom" })
  assert.match(r, /preset "mixed" saved \(2 agents\)/)

  const store = JSON.parse(await fs.readFile(PRESETS, "utf-8"))
  const p = store.presets.mixed
  assert.equal(p.schemaVersion, 2)
  assert.equal(p.omo.oracle.model, "prov/omo-model", "standard roles → omo section")
  assert.equal(p.omo.oracle.origin.layer, "state")
  assert.equal(p.custom["pcb-router"].model, "prov/custom-model", "non-standard roles → custom section")
  assert.equal(p.agents, undefined, "flat legacy key is gone")
  ok("two-section save: omo/custom split persisted")
}

/* ── Two-section export + import round-trip ────────────────────────── */
{
  const server = await mod.default.server()
  const t = server.tool
  const out = path.join(HOME, "export-mixed.json")
  await t.fastdraw_export_preset.execute({ name: "mixed", path: out })
  const payload = JSON.parse(await fs.readFile(out, "utf-8"))
  assert.equal(payload.fastdraw, 1)
  assert.equal(payload.schemaVersion, 2)
  assert.equal(payload.omo.oracle.model, "prov/omo-model")
  assert.equal(payload.custom["pcb-router"].model, "prov/custom-model")
  assert.equal(payload.agents, undefined, "export carries two-part structure")

  await t.fastdraw_delete_preset.execute({ name: "mixed" })
  const r = await t.fastdraw_import_preset.execute({ path: out })
  assert.match(r, /preset "mixed" imported/)
  const store = JSON.parse(await fs.readFile(PRESETS, "utf-8"))
  assert.equal(store.presets.mixed.omo.oracle.model, "prov/omo-model")
  assert.equal(store.presets.mixed.custom["pcb-router"].model, "prov/custom-model")
  ok("two-section export/import round-trip")
}

/* ── Load skips missing custom roles, warns, keeps the rest ────────── */
// preset referencing a custom role that does NOT exist on this machine
// (no agents dir in the test HOME) plus one that does exist in config —
// written to disk BEFORE the server boots (presets are read once at boot)
{
  const store = JSON.parse(await fs.readFile(PRESETS, "utf-8"))
  store.presets["has-ghost"] = {
    description: "ghost test",
    createdAt: new Date().toISOString(),
    omo: { oracle: "prov/ghost-omo" },
    custom: { "ghost-agent": "prov/ghost-custom" },
  }
  await fs.writeFile(PRESETS, JSON.stringify(store, null, 2))

  const server = await mod.default.server()
  const t = server.tool
  const cfg = { agent: { oracle: { model: "orig/o" }, sisyphus: { mode: "core" } } }
  await server.config(cfg)

  const r = await t.fastdraw_load_preset.execute({ name: "has-ghost" })
  assert.match(r, /loaded \(1 agents?\)/)
  assert.match(r, /Skipped 1 custom role not present on this machine: ghost-agent/)
  assert.equal(cfg.agent.oracle.model, "prov/ghost-omo", "present omo role applied")
  assert.equal(cfg.agent["ghost-agent"], undefined, "missing custom role NOT written to config")

  const state = JSON.parse(await fs.readFile(STATE, "utf-8"))
  assert.deepEqual(state.agents, { oracle: "prov/ghost-omo" }, "state keeps only present roles")
  ok("load skips missing custom role with warning, applies the rest")
}

/* ── Old flat-format presets still load (backward compat) ──────────── */
{
  // write a legacy flat store BEFORE booting a fresh server instance
  await fs.rm(HOME, { recursive: true, force: true })
  await fs.mkdir(path.dirname(PRESETS), { recursive: true })
  await fs.writeFile(
    PRESETS,
    JSON.stringify({
      presets: {
        legacy: {
          description: "old shape",
          createdAt: "2026-01-01T00:00:00Z",
          agents: { oracle: "prov/legacy-omo", "pcb-router": "prov/legacy-custom" },
        },
      },
    }, null, 2),
  )

  const server = await mod.default.server()
  const t = server.tool
  await server.config({ agent: { oracle: { model: "orig/o" }, "pcb-router": { model: "orig/p" } } })

  let r = await t.fastdraw_load_preset.execute({ name: "legacy" })
  assert.match(r, /preset "legacy" loaded \(2 agents\)/)

  // export round-trips the legacy data as the NEW two-part structure
  const out = path.join(HOME, "export-legacy.json")
  await t.fastdraw_export_preset.execute({ name: "legacy", path: out })
  const payload = JSON.parse(await fs.readFile(out, "utf-8"))
  assert.equal(payload.omo.oracle.model, "prov/legacy-omo", "flat entry classified as omo")
  assert.equal(payload.custom["pcb-router"].model, "prov/legacy-custom", "flat entry classified as custom")
  ok("old flat preset loads and normalizes to omo/custom")

  // assignment + save migrates the store file to the new shape
  r = await t.fastdraw_save_preset.execute({ name: "legacy" })
  assert.match(r, /preset "legacy" saved \(2 agents\)/)
  const migrated = JSON.parse(await fs.readFile(PRESETS, "utf-8"))
  assert.equal(migrated.presets.legacy.omo.oracle.model, "prov/legacy-omo")
  assert.equal(migrated.presets.legacy.custom["pcb-router"].model, "prov/legacy-custom")
  ok("store file migrated to two-part shape on next save")
}

/* ── Import of a legacy flat export file ───────────────────────────── */
{
  const server = await mod.default.server()
  const t = server.tool
  const file = path.join(HOME, "flat-export.json")
  await fs.writeFile(
    file,
    JSON.stringify({
      fastdraw: 1,
      name: "flatx",
      description: "legacy export",
      exportedAt: "2026-01-01T00:00:00Z",
      agents: { explore: "prov/flat-e", "session-mover": "prov/flat-s" },
    }),
  )
  const r = await t.fastdraw_import_preset.execute({ path: file })
  assert.match(r, /preset "flatx" imported/)
  const store = JSON.parse(await fs.readFile(PRESETS, "utf-8"))
  assert.equal(store.presets.flatx.omo.explore.model, "prov/flat-e")
  assert.equal(store.presets.flatx.custom["session-mover"].model, "prov/flat-s")
  const lr = await t.fastdraw_load_preset.execute({ name: "flatx" })
  assert.match(lr, /loaded \(1 agents?\)/)
  assert.match(lr, /Skipped 1 custom role not present on this machine: session-mover/)
  ok("legacy flat export file imports → classified omo/custom")
  // the global restore wrote a config file — drop it so later tests (which
  // harvest config layers at save time) start from a clean slate
  await fs.rm(path.join(HOME, ".config/opencode/opencode.jsonc"), { force: true })
}

/* 8. models bound via opencode config (never touched by FastDraw) count as
   assignments for save_preset — snapshot merges config-declared models */
{
  await fs.rm(path.join(HOME, ".config/opencode/.fastdraw.json"), { force: true })
  const server = await mod.default.server()
  const t = server.tool
  const cfg = {
    agent: {
      oracle: { model: "prov/cfg-omo", mode: "subagent" },
      "pcb-router": { model: "prov/cfg-custom" },
      bare: { model: "no-slash-model" },
    },
  }
  await server.config(cfg)
  await assert.rejects(
    () => fs.readFile(path.join(HOME, ".config/opencode/.fastdraw.json")),
    /ENOENT/,
  )
  const r = await t.fastdraw_save_preset.execute({ name: "cfgonly" })
  assert.match(r, /preset "cfgonly" saved \(2 agents\)/, "config-level bindings count as assignments")
  assert.match(r, /no recorded config origin and will restore to the global config: oracle, pcb-router/, "origin-less config bindings reported")
  const store = JSON.parse(await fs.readFile(PRESETS, "utf-8"))
  assert.equal(store.presets.cfgonly.omo.oracle.model, "prov/cfg-omo")
  assert.equal(store.presets.cfgonly.omo.oracle.origin, undefined, "programmatic-only bindings carry no origin")
  assert.equal(store.presets.cfgonly.custom["pcb-router"].model, "prov/cfg-custom")
  await assert.rejects(
    () => fs.readFile(path.join(HOME, ".config/opencode/.fastdraw.json")),
    /ENOENT/,
    "saving a preset must not fabricate FastDraw state for config bindings",
  )
  ok("config-only bindings saveable; snapshot merges config models")
}

/* 9. restore mode "path": preview makes no changes; real load writes only
   the touched roles into the target JSONC, keeps comments & other keys,
   and backs the file up first */
{
  await fs.rm(HOME, { recursive: true, force: true })
  await fs.mkdir(HOME, { recursive: true })
  const CONFIG_DIR = path.join(HOME, ".config", "opencode")
  const target = path.join(CONFIG_DIR, "project-settings.jsonc")
  await fs.mkdir(CONFIG_DIR, { recursive: true })
  await fs.writeFile(
    target,
    `{\n  // local note\n  "agent": {\n    "builder": { "model": "prov/builder" }\n  }\n}\n`,
  )
  await fs.writeFile(
    PRESETS,
    JSON.stringify({
      presets: {
        "has-path": {
          description: "path mode",
          createdAt: "2026-01-01T00:00:00Z",
          omo: { oracle: { model: "prov/p-omo", origin: { layer: "state", file: "${CONFIG_DIR}/.fastdraw.json" } } },
          custom: { "pcb-router": { model: "prov/p-custom", origin: { layer: "state", file: "${CONFIG_DIR}/.fastdraw.json" } } },
        },
      },
    }, null, 2),
  )

  const server = await mod.default.server()
  const t = server.tool
  const cfg = { agent: { oracle: { model: "orig/o" }, "pcb-router": { model: "orig/p" } } }
  await server.config(cfg)

  const pr = await t.fastdraw_load_preset.execute({
    name: "has-path",
    mode: "path",
    targetPath: target,
    preview: true,
  })
  assert.match(pr, /preview of preset "has-path" \(mode: path\)/)
  assert.match(pr, /project-settings\.jsonc → 2 binding\(s\)/)
  assert.equal(
    await fs.readFile(target, "utf-8"),
    `{\n  // local note\n  "agent": {\n    "builder": { "model": "prov/builder" }\n  }\n}\n`,
    "preview touches nothing",
  )
  await assert.rejects(() => fs.readFile(STATE), /ENOENT/, "preview does not apply the preset")

  const r = await t.fastdraw_load_preset.execute({
    name: "has-path",
    mode: "path",
    targetPath: target,
  })
  assert.match(r, /preset "has-path" loaded \(2 agents\)/, "live apply unchanged")
  assert.match(r, /Restored \(mode: path\)/)
  assert.match(r, /project-settings\.jsonc → 2 binding\(s\) \(backup: .*\.bak-\d{8}-\d{6}\)/)
  const jtxt = await fs.readFile(target, "utf-8")
  assert.match(jtxt, /\/\/ local note/, "unrelated comment survives")
  const jj = JSON.parse(origins.stripJsonComments(jtxt))
  assert.equal(jj.agent.oracle.model, "prov/p-omo", "omo role written into target")
  assert.equal(jj.agent["pcb-router"].model, "prov/p-custom", "custom role written into target")
  assert.equal(jj.agent.builder.model, "prov/builder", "untouched role kept verbatim")
  const state = JSON.parse(await fs.readFile(STATE, "utf-8"))
  assert.deepEqual(Object.keys(state.agents).sort(), ["oracle", "pcb-router"])
  const bak = (await fs.readdir(CONFIG_DIR)).find((n) => n.startsWith("project-settings.jsonc.bak-"))
  assert.ok(bak, "backup file created")
  assert.equal(
    await fs.readFile(path.join(CONFIG_DIR, bak), "utf-8"),
    `{\n  // local note\n  "agent": {\n    "builder": { "model": "prov/builder" }\n  }\n}\n`,
    "backup holds the pre-restore bytes",
  )
  assert.ok(
    !(await fs.readdir(CONFIG_DIR)).some((n) => n.includes("fastdraw-tmp")),
    "no temp leftovers",
  )
  ok("path restore: preview inert, surgical write, backup with old bytes")
}

/* 10. restore mode "original": a binding harvested from the global config
   file is written BACK to that file when the preset is loaded */
{
  await fs.rm(HOME, { recursive: true, force: true })
  await fs.mkdir(path.join(HOME, ".config", "opencode"), { recursive: true })
  const cfgFile = path.join(HOME, ".config", "opencode", "opencode.jsonc")
  await fs.writeFile(cfgFile, JSON.stringify({ agent: { oracle: { model: "prov/orig-source" } } }, null, 2))
  const server = await mod.default.server()
  const t = server.tool

  const sr = await t.fastdraw_save_preset.execute({ name: "ri" })
  assert.match(sr, /preset "ri" saved \(1 agents?\)/, "harvested binding saveable")
  const store = JSON.parse(await fs.readFile(PRESETS, "utf-8"))
  assert.equal(store.presets.ri.omo.oracle.origin.file, "${CONFIG_DIR}/opencode.jsonc", "origin recorded portably")

  // the config file loses the role (e.g. hand-edit)
  await fs.writeFile(cfgFile, `{\n  "agent": {}\n}\n`)
  const r = await t.fastdraw_load_preset.execute({ name: "ri", mode: "original" })
  assert.match(r, /preset "ri" loaded \(1 agents?\)/)
  assert.match(r, /Restored \(mode: original\)/)
  assert.match(r, /opencode\.jsonc → 1 binding\(s\) \(backup: .*\.bak-\d{8}-\d{6}\)/)
  const j = JSON.parse(origins.stripJsonComments(await fs.readFile(cfgFile, "utf-8")))
  assert.equal(j.agent.oracle.model, "prov/orig-source", "role written back to its origin file")
  const bak = (await fs.readdir(path.join(HOME, ".config", "opencode"))).find(
    (n) => n.startsWith("opencode.jsonc.bak-"),
  )
  assert.ok(bak)
  assert.equal(
    await fs.readFile(path.join(HOME, ".config", "opencode", bak), "utf-8"),
    `{\n  "agent": {}\n}\n`,
    "backup = the file as it was before restore",
  )
  ok("original restore: writes back to harvested origin file with backup")
}

/* 11. origin without any portable path (env-layer file outside known
   roots) falls back to the global config on restore — with warning */
{
  await fs.rm(HOME, { recursive: true, force: true })
  await fs.mkdir(path.join(HOME, ".config", "opencode"), { recursive: true })
  const envFile = path.join("/tmp", "fastdraw-test-split-env", "opencode.json")
  await fs.mkdir(path.dirname(envFile), { recursive: true })
  await fs.writeFile(envFile, JSON.stringify({ agent: { oracle: { model: "prov/env-model" } } }))
  process.env.OPENCODE_CONFIG = envFile
  const server = await mod.default.server()
  const t = server.tool

  const sr = await t.fastdraw_save_preset.execute({ name: "envcfg" })
  assert.match(sr, /preset "envcfg" saved \(1 agents?\)/)
  const store = JSON.parse(await fs.readFile(PRESETS, "utf-8"))
  assert.equal(store.presets.envcfg.omo.oracle.origin.file, null, "non-portable origin stored as null")
  delete process.env.OPENCODE_CONFIG

  const r = await t.fastdraw_load_preset.execute({ name: "envcfg", mode: "original" })
  assert.match(r, /had no resolvable origin on this machine and were written to the global config: oracle/)
  const cfgFile = path.join(HOME, ".config", "opencode", "opencode.jsonc")
  const j = JSON.parse(origins.stripJsonComments(await fs.readFile(cfgFile, "utf-8")))
  assert.equal(j.agent.oracle.model, "prov/env-model", "fell back to the global config file")
  ok("original restore: origin-less bindings fall back to global config, warned")
}

/* 12. bindings that came from FastDraw state (.fastdraw.json) never get
   written into config files on restore — they already live in state */
{
  await fs.rm(HOME, { recursive: true, force: true })
  await fs.mkdir(path.join(HOME, ".config", "opencode"), { recursive: true })
  const server = await mod.default.server()
  const t = server.tool
  await server.config({ agent: { oracle: { model: "orig/o" } } })
  await t.fastdraw_assign.execute({ agent: "oracle", model: "prov/state-model" })
  await t.fastdraw_save_preset.execute({ name: "st" })
  const r = await t.fastdraw_load_preset.execute({ name: "st", mode: "original" })
  assert.match(r, /Restored \(mode: original\)/)
  assert.match(r, /came from FastDraw state \(\.fastdraw\.json\) and stay there: oracle/)
  // no config file was created: the state binding needs no file write
  const cfgFile = path.join(HOME, ".config", "opencode", "opencode.jsonc")
  await assert.rejects(() => fs.readFile(cfgFile), /ENOENT/, "nothing written for state-layer bindings")
  ok("original restore: state-layer bindings stay in .fastdraw.json")
}

/* 13. restore arg validation: unknown mode rejected; path mode requires
   targetPath — both fail before any preset lookup */
{
  const server = await mod.default.server()
  const t = server.tool
  let r = await t.fastdraw_load_preset.execute({ name: "x", mode: "bogus" })
  assert.match(r, /unknown restore mode "bogus"/)
  r = await t.fastdraw_load_preset.execute({ name: "x", mode: "path" })
  assert.match(r, /targetPath is required for restore mode 'path'/)
  ok("restore args validated before preset lookup")
}

await fs.rm(HOME, { recursive: true, force: true })
console.log(`\n${pass}/13 tests passed`)