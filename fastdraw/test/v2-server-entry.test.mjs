/* V2 entry-shape tests — server half. HOME is redirected to a temp dir; a
   fake V2 context exercises setup(ctx): agent transform registration and the
   9-tool JSON Schema registry. Run via `npm test` (pretest bundles server.ts
   with @opencode/plugin kept external). */
import assert from "node:assert"
import fs from "node:fs/promises"

process.env.HOME = "/tmp/fastdraw-v2-server"
await fs.rm("/tmp/fastdraw-v2-server", { recursive: true, force: true })
await fs.mkdir("/tmp/fastdraw-v2-server/.config/opencode", { recursive: true })
process.chdir("/tmp/fastdraw-v2-server")

// Seed FastDraw state BEFORE setup(): "build" exists in the fake engine,
// "ghost" is phantom — it must be skipped by the agent transform.
await fs.writeFile(
  "/tmp/fastdraw-v2-server/.config/opencode/.fastdraw.json",
  JSON.stringify({ schemaVersion: 2, agents: { build: "prov/m1", ghost: "p/q" } }),
)

const mod = await import(new URL("./.build/server.mjs", import.meta.url))

let pass = 0
const ok = (name) => { pass++; console.log(`PASS ${name}`) }

/* 1. default export = V2 definition + V1 server bridge */
assert.equal(mod.default.id, "fastdraw")
assert.equal(typeof mod.default.setup, "function")
assert.equal(typeof mod.default.server, "function")
ok("default export has stable id + setup + server() bridge")

/* 2. setup registers exactly one agent transform and one tool transform */
const agentCbs = []
const toolCbs = []
let reloads = 0
const ctx = {
  agent: {
    transform: async (cb) => { agentCbs.push(cb); return { dispose() {} } },
    reload: async () => { reloads++ },
  },
  tool: {
    transform: async (cb) => { toolCbs.push(cb); return { dispose() {} } },
  },
}
await mod.default.setup(ctx)
assert.equal(agentCbs.length, 1, "one agent transform")
assert.equal(toolCbs.length, 1, "one tool transform")
ok("setup registers agent + tool transforms")

/* 3. the tool registry: all 9 fastdraw_* tools, JSON Schema inputs */
const added = []
toolCbs[0]({ add: (t) => added.push(t) })
assert.deepEqual(added.map((t) => t.name).sort(), [
  "fastdraw_assign",
  "fastdraw_delete_preset",
  "fastdraw_export_preset",
  "fastdraw_import_preset",
  "fastdraw_list",
  "fastdraw_list_presets",
  "fastdraw_load_preset",
  "fastdraw_remove",
  "fastdraw_save_preset",
])
for (const t of added) {
  assert.ok(t.description && t.description.length > 10, `description: ${t.name}`)
  assert.equal(t.input.type, "object", `input.type: ${t.name}`)
  assert.equal(t.input.additionalProperties, false, `additionalProperties: ${t.name}`)
  assert.equal(typeof t.execute, "function", `execute: ${t.name}`)
}
ok("9 fastdraw_* tools registered with JSON Schema inputs")

/* 4. agent transform: object-form model write via editor.update(id, fn).
   Engine truths (E0 2026-11-16, v2-reload-probe): the real AgentEditor
   persists writes ONLY through update(id, draftFn); list() mutations are
   inert; and update(id, fn) also reaches ids ABSENT from list() — custom
   agent/*.md agents merge after transforms, the callback still fires with
   their real draft. The fake mirrors all three. */
const agents = [
  { name: "build", model: { providerID: "base", id: "b0" } },
  { name: "plan" },
]
let updateCalls = 0
const absentDrafts = new Map()
const mkEditor = () => ({
  list: () => agents,
  update: (id, fn) => {
    updateCalls++
    const entry = agents.find((a) => a.name === id)
    if (entry) {
      fn(entry)
      return
    }
    if (!absentDrafts.has(id)) absentDrafts.set(id, { name: id })
    fn(absentDrafts.get(id))
  },
})
agentCbs[0](mkEditor())
assert.deepEqual(agents[0].model, { providerID: "prov", id: "m1" })
assert.equal(agents[1].model, undefined)
assert.ok(updateCalls > 0, "writes go through editor.update, not list() mutation")
assert.deepEqual(
  absentDrafts.get("ghost")?.model,
  { providerID: "p", id: "q" },
  "file-defined agents absent from list() are still written via update",
)
ok("agent transform writes present AND absent (file-defined) ids via update({providerID,id})")

/* 5. replayable: a second run of the same callback is idempotent */
agentCbs[0](mkEditor())
assert.deepEqual(agents[0].model, { providerID: "prov", id: "m1" })
assert.deepEqual(absentDrafts.get("ghost").model, { providerID: "p", id: "q" })
ok("agent transform is idempotent under replay")

console.log(`v2-server-entry: ${pass} checks passed`)
