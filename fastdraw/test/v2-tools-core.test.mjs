/* V2 tool core paths — assign/list/save/remove/load through the V2 execute
   wrapper (V1 factory underneath), with HOME in a temp dir and a fake agent
   editor to observe the transform apply/revert cycle. */
import assert from "node:assert"
import fs from "node:fs/promises"

const HOME = "/tmp/fastdraw-v2-core"
process.env.HOME = HOME
await fs.rm(HOME, { recursive: true, force: true })
await fs.mkdir(`${HOME}/.config/opencode`, { recursive: true })
process.chdir(HOME)

const mod = await import(new URL("./.build/server.mjs", import.meta.url))

let pass = 0
const ok = (name) => { pass++; console.log(`PASS ${name}`) }

let agentCb = null
let reloads = 0
const tools = {}
const ctx = {
  agent: {
    transform: async (cb) => { agentCb = cb; return { dispose() {} } },
    reload: async () => { reloads++ },
  },
  tool: {
    transform: async (cb) => { cb({ add: (t) => { tools[t.name] = t } }); return { dispose() {} } },
  },
}
await mod.default.setup(ctx)

const STATE = `${HOME}/.config/opencode/.fastdraw.json`
const readState = async () => JSON.parse(await fs.readFile(STATE, "utf-8"))
const agents = [{ name: "build", model: { providerID: "orig", id: "m0" } }]
// Engine truth (E0 2026-11-16): agent writes persist only via update(id, fn).
const editor = {
  list: () => agents,
  update: (id, fn) => {
    const entry = agents.find((a) => a.name === id)
    if (entry) fn(entry)
  },
}
agentCb(editor) // baseline transform run — records engine base models

/* 1. assign → string content, state file, ctx.agent.reload requested */
{
  const r = await tools.fastdraw_assign.execute({ agent: "build", model: "prov/m1" })
  assert.equal(typeof r.content, "string")
  assert.match(r.content, /prov\/m1/)
  assert.equal((await readState()).agents.build, "prov/m1")
  assert.ok(reloads >= 1, "hot tool must request agent.reload")
  ok("fastdraw_assign writes state + requests reload")
}

/* 2. transform applies the binding onto the editor agent */
agentCb(editor)
assert.deepEqual(agents[0].model, { providerID: "prov", id: "m1" })
ok("agent transform applies the new binding")

/* 3. list shows it */
{
  const r = await tools.fastdraw_list.execute({})
  assert.match(r.content, /build: prov\/m1/)
  ok("fastdraw_list reports the binding")
}

/* 4. save_preset snapshots it */
{
  const r = await tools.fastdraw_save_preset.execute({ name: "core", description: "core test" })
  assert.match(r.content, /preset "core" saved/)
  ok("fastdraw_save_preset persists the snapshot")
}

/* 5. remove → state cleared → transform REVERTS to the engine base model */
{
  const r = await tools.fastdraw_remove.execute({ agent: "build" })
  assert.match(r.content, /removed override for build/)
  assert.equal((await readState()).agents.build, undefined)
  agentCb(editor)
  assert.deepEqual(agents[0].model, { providerID: "orig", id: "m0" })
  ok("fastdraw_remove reverts the editor agent to its base model")
}

/* 6. load_preset re-applies, transform pushes it live again */
{
  const r = await tools.fastdraw_load_preset.execute({ name: "core" })
  assert.match(r.content, /core/)
  assert.equal((await readState()).agents.build, "prov/m1")
  agentCb(editor)
  assert.deepEqual(agents[0].model, { providerID: "prov", id: "m1" })
  ok("fastdraw_load_preset restores bindings (state + transform)")
}

/* 7. delete_preset cleans the store */
{
  const r = await tools.fastdraw_delete_preset.execute({ name: "core" })
  assert.match(r.content, /preset "core" deleted/)
  const store = JSON.parse(
    await fs.readFile(`${HOME}/.config/opencode/fastdraw-presets.json`, "utf-8"),
  )
  assert.equal(store.presets.core, undefined)
  ok("fastdraw_delete_preset removes the preset from the store")
}

console.log(`v2-tools-core: ${pass} checks passed`)
