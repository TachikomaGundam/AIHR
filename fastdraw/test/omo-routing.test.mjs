/* FastDraw OMO-routing e2e tests — boot with a fake HOME holding an OMO
   config and a legacy .fastdraw.json, then verify: legacy bindings migrate
   into the OMO file (not cfg.agent), assigns/removes/lists/preset-loads
   route roles AND categories to ~/.omo/omo.jsonc, and custom roles keep the
   opencode-cfg path. Run via `npm test` (pretest bundles server.ts).
   Nothing under the real ~/.omo or ~/.config is touched. */
import assert from "node:assert"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"

let pass = 0
const TMP = await fs.mkdtemp(path.join(os.tmpdir(), "fd-route-"))
const HOME = path.join(TMP, "home")
process.env.HOME = HOME
const OMO_FILE = path.join(HOME, ".omo", "omo.jsonc")
const CONFIG_DIR = path.join(HOME, ".config", "opencode")
const STATE = path.join(CONFIG_DIR, ".fastdraw.json")

const w = async (p, s) => {
  await fs.mkdir(path.dirname(p), { recursive: true })
  await fs.writeFile(p, s)
}
const parse = async (p) => JSON.parse((await import("./.build/origins.mjs")).stripJsonComments(await fs.readFile(p, "utf-8")))

/* legacy broken state: OMO roles bound via FastDraw "agents" map */
await w(
  OMO_FILE,
  `{
  // user notes stay
  "[opencode]": {
    "agents": {
      "oracle": { "model": "prov/oracle-orig" },
      "explore": { "model": "prov/explore-orig" }
    },
    "categories": {
      "deep": { "description": "d", "models": ["prov/m1"], "model": "prov/deep-orig" }
    }
  }
}
`,
)
await w(STATE, JSON.stringify({ agents: { explore: "prov/explore-fastdraw" } }))

const mod = await import(new URL("./.build/server.mjs", import.meta.url))
const freshServer = async () => {
  const server = await mod.default.server()
  return { server, t: server.tool }
}

/* 1. boot migration: legacy OMO-role state moves into the OMO file and the
   omo section; opencode cfg.agent is never touched for them */
{
  const { server, t } = await freshServer()
  const cfg = { agent: {} }
  await server.config(cfg)
  assert.deepEqual(cfg.agent, {}, "no phantom role applied to opencode config")
  const omo = await parse(OMO_FILE)
  assert.equal(omo["[opencode]"].agents.explore.model, "prov/explore-fastdraw", "migrated binding landed in OMO file")
  assert.equal(omo["[opencode]"].agents.oracle.model, "prov/oracle-orig", "untouched role left alone")
  assert.match(await fs.readFile(OMO_FILE, "utf-8"), /\/\/ user notes stay/)
  const st = JSON.parse(await fs.readFile(STATE, "utf-8"))
  assert.deepEqual(st.agents, {})
  assert.equal(st.omo.explore.model, "prov/explore-fastdraw")
  assert.equal(st.omo.explore.original, "prov/explore-orig", "original recorded for revert")
  ok("boot migration: OMO-role state re-routed to ~/.omo/omo.jsonc, zero phantom roles")
}

/* 2. assign role → OMO file; assign category → categories section;
   custom agent → still opencode cfg path */
{
  const { server, t } = await freshServer()
  const cfg = { agent: {} }
  await server.config(cfg)
  const rRole = await t.fastdraw_assign.execute({ agent: "oracle", model: "prov/new-oracle" })
  assert.match(rRole, /bound in OMO config/)
  assert.equal((await parse(OMO_FILE))["[opencode]"].agents.oracle.model, "prov/new-oracle")

  const rCat = await t.fastdraw_assign.execute({ agent: "deep", model: "prov/new-deep" })
  assert.match(rCat, /bound in OMO config/)
  const omo = await parse(OMO_FILE)
  assert.equal(omo["[opencode]"].categories.deep.model, "prov/new-deep", "category bound in categories")
  assert.deepEqual(
    omo["[opencode]"].categories.deep.models,
    ["prov/new-deep"],
    "dominant models[] normalized so models[0] cannot shadow the new binding",
  )
  assert.equal(omo["[opencode]"].agents.deep, undefined, "category did NOT become a new agent")

  const rCustom = await t.fastdraw_assign.execute({ agent: "pcb-router", model: "prov/pcb" })
  assert.match(rCustom, /pcb-router → prov\/pcb/)
  assert.equal(cfg.agent["pcb-router"]?.model, "prov/pcb", "custom agent uses live cfg path")
  assert.equal((await parse(OMO_FILE))["[opencode]"].agents["pcb-router"], undefined)
  ok("assign routes roles/categories to the OMO file, customs to cfg.agent")
}

/* 3. list shows OMO roles + categories with fastdraw marks */
{
  const { t } = await freshServer()
  const r = await t.fastdraw_list.execute()
  assert.match(r, /OMO Roles:/)
  assert.match(r, /OMO Categories:/)
  assert.match(r, /oracle: prov\/new-oracle/)
  assert.match(r, /deep: prov\/new-deep/)
  assert.match(r, /\[fastdraw\]/)
  ok("fastdraw_list surfaces OMO roles and categories")
}

/* 4. remove reverts the OMO binding to the recorded original */
{
  const { t } = await freshServer()
  const r = await t.fastdraw_remove.execute({ agent: "oracle" })
  assert.match(r, /reverted to prov\/oracle-orig/)
  assert.equal((await parse(OMO_FILE))["[opencode]"].agents.oracle.model, "prov/oracle-orig")
  const st = JSON.parse(await fs.readFile(STATE, "utf-8"))
  assert.equal(st.omo.oracle, undefined, "record dropped after revert")
  ok("fastdraw_remove reverts OMO roles to their pre-fastdraw model")
}

/* 4b. remove reverts a CATEGORY exactly: original model AND the pre-
   fastdraw dominant models[] array come back verbatim */
{
  const { t } = await freshServer()
  const before = (await parse(OMO_FILE))["[opencode]"].categories.deep
  assert.deepEqual(before.models, ["prov/new-deep"], "still normalized from the test-2 assign")
  const r = await t.fastdraw_remove.execute({ agent: "deep" })
  assert.match(r, /reverted to prov\/deep-orig/)
  const omo = (await parse(OMO_FILE))["[opencode]"].categories.deep
  assert.equal(omo.model, "prov/deep-orig")
  assert.deepEqual(omo.models, ["prov/m1"], "dominant models[] restored to pre-fastdraw content")
  const st = JSON.parse(await fs.readFile(STATE, "utf-8"))
  assert.equal(st.omo.deep, undefined, "category record dropped after revert")
  ok("fastdraw_remove restores the category's pre-fastdraw models[] array exactly")
}

/* 5. load_preset: OMO names bind via the OMO file, custom names via the
   restore plan — and never leak into opencode config files */
{
  await w(path.join(CONFIG_DIR, "opencode.jsonc"), `{\n  // global note\n  "agent": {}\n}\n`)
  await w(path.join(CONFIG_DIR, "agents", "pcb-router.md"), "---\nmodel: prov/x\n---\n# pcb-router\n")
  await w(
    path.join(CONFIG_DIR, "fastdraw-presets.json"),
    JSON.stringify({
      presets: {
        fleet: {
          schemaVersion: 2,
          omo: {
            oracle: { model: "prov/preset-oracle", origin: { layer: "state", file: "${CONFIG_DIR}/.fastdraw.json" } },
            deep: { model: "prov/preset-deep" },
          },
          custom: {
            "pcb-router": { model: "prov/preset-pcb", origin: { layer: "state", file: "${CONFIG_DIR}/.fastdraw.json" } },
          },
        },
      },
    }),
  )
  const { server, t } = await freshServer()
  await server.config({ agent: {} })
  const r = await t.fastdraw_load_preset.execute({ name: "fleet", mode: "global" })
  assert.match(r, /loaded \(3 agents\)/)
  assert.match(r, /OMO config .*:/, "OMO write reported")
  const omo = await parse(OMO_FILE)
  assert.equal(omo["[opencode]"].agents.oracle.model, "prov/preset-oracle")
  assert.equal(omo["[opencode]"].categories.deep.model, "prov/preset-deep")
  const global = await parse(path.join(CONFIG_DIR, "opencode.jsonc"))
  assert.equal(global.agent["pcb-router"].model, "prov/preset-pcb")
  assert.equal(global.agent.oracle, undefined, "OMO role never written into opencode config")
  assert.equal(global.agent.deep, undefined)
  assert.match(await fs.readFile(path.join(CONFIG_DIR, "opencode.jsonc"), "utf-8"), /\/\/ global note/)
  ok("load_preset routes OMO roles/categories to the OMO file, customs to config")
}

/* 6. save_preset harvests OMO targets with an omo origin */
{
  const { t } = await freshServer()
  const r = await t.fastdraw_save_preset.execute({ name: "snap" })
  assert.match(r, /saved \(\d+ agents\)/)
  const store = JSON.parse(await fs.readFile(path.join(CONFIG_DIR, "fastdraw-presets.json"), "utf-8"))
  const snap = store.presets.snap
  const all = { ...snap.omo, ...snap.custom }
  assert.equal(all.deep.model, "prov/preset-deep")
  assert.equal(all.deep.origin.layer, "omo")
  assert.match(all.deep.origin.file, /\.omo\/omo\.jsonc$/)
  ok("save_preset captures OMO-side bindings with their file origin")
}

function ok(name) {
  pass++
  console.log(`PASS ${name}`)
}

await fs.rm(TMP, { recursive: true, force: true })
console.log(`\n${pass} tests passed`)
