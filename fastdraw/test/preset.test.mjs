/* FastDraw preset logic tests — run via `npm test` (pretest transpiles server.ts
   with esbuild into test/.build/). HOME is redirected to a temp dir so no real
   opencode state is touched. */
import assert from "node:assert"
import fs from "node:fs/promises"
import path from "node:path"

process.env.HOME = "/tmp/fastdraw-test"
await fs.rm("/tmp/fastdraw-test", { recursive: true, force: true })
await fs.mkdir("/tmp/fastdraw-test", { recursive: true })
// Isolate the origin harvest: cwd must not be inside the repo (no repo-level
// opencode configs may leak into the snapshot).
process.chdir("/tmp/fastdraw-test")

const mod = await import(new URL("./.build/server.mjs", import.meta.url))
const server = await mod.default.server()
const t = server.tool

let pass = 0
const ok = (name) => { pass++; console.log(`PASS ${name}`) }

/* 1. save_preset with empty state → refused */
{
  const r = await t.fastdraw_save_preset.execute({ name: "empty" })
  assert.match(r, /no assignments to save/)
  ok("save_preset refuses empty state")
}

/* 2. assign ×2, then save preset */
{
  // fake config for hot-apply + originals snapshot
  const cfg = { agent: { oracle: { model: "orig/oracle-model", mode: "subagent" }, sisyphus: { mode: "core" } } }
  await server.config(cfg)

  await t.fastdraw_assign.execute({ agent: "oracle", model: "prov/model-a" })
  await t.fastdraw_assign.execute({ agent: "explore", model: "prov/model-b" })
  assert.equal(cfg.agent.oracle.model, "prov/model-a")
  assert.equal(cfg.agent.oracle.mode, "subagent", "existing fields preserved")

  const r = await t.fastdraw_save_preset.execute({ name: "p1", description: "test preset" })
  assert.match(r, /preset "p1" saved \(2 agents\)/)
  assert.match(r, /oracle\s+→\s+prov\/model-a/)

  const store = JSON.parse(await fs.readFile("/tmp/fastdraw-test/.config/opencode/fastdraw-presets.json", "utf-8"))
  assert.equal(store.presets.p1.description, "test preset")
  assert.equal(store.presets.p1.schemaVersion, 2, "v2 schema stamped on save")
  assert.equal(store.presets.p1.omo.oracle.model, "prov/model-a", "state binding becomes v2 entry")
  assert.equal(store.presets.p1.omo.oracle.origin.layer, "state")
  assert.equal(store.presets.p1.omo.oracle.origin.file, "${CONFIG_DIR}/.fastdraw.json", "origin stored portably")
  assert.equal(store.presets.p1.omo.explore.model, "prov/model-b")
  assert.deepEqual(store.presets.p1.custom, {})
  ok("assign + save_preset persists with preview")
}

/* 3. hot-swap: load preset that drops one agent → reverts to original */
{
  await t.fastdraw_assign.execute({ agent: "oracle", model: "prov/model-c" })
  // p1 has oracle+explore; current has oracle(c)+explore. Load p1 → oracle back to a.
  const cfg = { agent: { oracle: { model: "orig/oracle-model" }, explore: { model: "orig/explore" } } }
  await server.config(cfg)
  const r = await t.fastdraw_load_preset.execute({ name: "p1" })
  assert.match(r, /preset "p1" loaded \(2 agents\)/)
  assert.equal(cfg.agent.oracle.model, "prov/model-a")
  assert.equal(cfg.agent.explore.model, "prov/model-b")
  ok("load_preset hot-swaps config")
}

/* 4. hot-swap revert: assign extra agent, then load preset without it */
{
  const cfg = { agent: { oracle: { model: "orig/o" }, explore: { model: "orig/e" }, custom1: { model: "orig/c1" } } }
  await server.config(cfg)
  await t.fastdraw_assign.execute({ agent: "custom1", model: "prov/override" })
  assert.equal(cfg.agent.custom1.model, "prov/override")
  await t.fastdraw_load_preset.execute({ name: "p1" })
  assert.equal(cfg.agent.custom1.model, "orig/c1", "agent absent from preset reverted to original")
  ok("load_preset reverts agents not in preset")
}

/* 5. export → import round-trip */
{
  const out = "/tmp/fastdraw-test/export-p1.json"
  await t.fastdraw_export_preset.execute({ name: "p1", path: out })
  const payload = JSON.parse(await fs.readFile(out, "utf-8"))
  assert.equal(payload.fastdraw, 1)
  assert.equal(payload.schemaVersion, 2)
  assert.equal(payload.name, "p1")
  assert.equal(payload.omo.oracle.model, "prov/model-a")
  assert.equal(payload.omo.oracle.origin.file, "${CONFIG_DIR}/.fastdraw.json")
  assert.equal(payload.omo.explore.model, "prov/model-b")
  assert.deepEqual(payload.custom, {})
  assert.equal(payload.agents, undefined, "export carries two-part structure")

  await t.fastdraw_delete_preset.execute({ name: "p1" })
  let r = await t.fastdraw_load_preset.execute({ name: "p1" })
  assert.match(r, /not found/)

  r = await t.fastdraw_import_preset.execute({ path: out })
  assert.match(r, /preset "p1" imported/)
  r = await t.fastdraw_load_preset.execute({ name: "p1" })
  assert.match(r, /loaded \(2 agents\)/)
  ok("export → delete → import → load round-trip")
}

/* 6. import with name override */
{
  const r = await t.fastdraw_import_preset.execute({ path: "/tmp/fastdraw-test/export-p1.json", name: "p1-copy" })
  assert.match(r, /preset "p1-copy" imported/)
  ok("import name override")
}

/* 7. bulk import: presets store file itself */
{
  const r = await t.fastdraw_import_preset.execute({ path: "/tmp/fastdraw-test/.config/opencode/fastdraw-presets.json" })
  assert.match(r, /imported 2 presets/)
  assert.match(r, /p1-copy/)
  ok("bulk import of presets store")
}

/* 8. invalid imports → graceful errors */
{
  await fs.writeFile("/tmp/fastdraw-test/bad1.json", JSON.stringify({ hello: "world" }))
  let r = await t.fastdraw_import_preset.execute({ path: "/tmp/fastdraw-test/bad1.json" })
  assert.match(r, /invalid preset file/)

  await fs.writeFile("/tmp/fastdraw-test/bad2.json", JSON.stringify({ agents: { oracle: "no-slash-model" } }))
  r = await t.fastdraw_import_preset.execute({ path: "/tmp/fastdraw-test/bad2.json" })
  assert.match(r, /invalid preset file/)

  r = await t.fastdraw_import_preset.execute({ path: "/tmp/fastdraw-test/nonexistent.json" })
  assert.match(r, /cannot read/)
  ok("invalid imports rejected gracefully")
}

/* 9. remove reverts via originals (fresh agent name — originals snapshot is first-write-wins) */
{
  const cfg = { agent: { momus: { model: "orig/m" } } }
  await server.config(cfg)
  await t.fastdraw_assign.execute({ agent: "momus", model: "prov/x" })
  const r = await t.fastdraw_remove.execute({ agent: "momus" })
  assert.match(r, /reverted to default/)
  assert.equal(cfg.agent.momus.model, "orig/m")
  ok("remove reverts to original model")
}

/* 10. list_presets shows bindings */
{
  const r = await t.fastdraw_list_presets.execute({})
  assert.match(r, /### p1/)
  assert.match(r, /oracle\s+→\s+prov\/model-a/)
  assert.match(r, /2 agents/)
  ok("list_presets with preview")
}

await fs.rm("/tmp/fastdraw-test", { recursive: true, force: true })
console.log(`\n${pass}/10 tests passed`)
