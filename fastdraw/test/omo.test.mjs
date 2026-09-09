/* FastDraw OMO module tests — nested JSONC editing (chain creation, role
   routing) and the ~/.omo config read/write layer. Run via `npm test`
   (pretest bundles origins.ts → test/.build/origins.mjs and omo.ts →
   test/.build/omo.mjs). All fixtures live under a temp dir; the real
   ~/.omo and ~/.config are never touched. */
import assert from "node:assert"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"

const TMP = await fs.mkdtemp(path.join(os.tmpdir(), "fd-omo-"))
const HOME = path.join(TMP, "home")
const OMO_DIR = path.join(HOME, ".omo")
const OMO_FILE = path.join(OMO_DIR, "omo.jsonc")
const CONFIG_DIR = path.join(HOME, ".config", "opencode")

const origins = await import(new URL("./.build/origins.mjs", import.meta.url))
const omo = await import(new URL("./.build/omo.mjs", import.meta.url))
const { setNestedModelsJsonc, stripJsonComments } = origins
const {
  resolveOmoUserConfigFile,
  readOmoConfig,
  writeOmoModels,
  isOmoName,
  omoTargetKind,
  omoPortableFile,
  verifyWritten,
  OMO_STATIC_CATEGORIES,
  OMO_STATIC_AGENTS,
} = omo

let pass = 0
const ok = (name) => {
  pass++
  console.log(`PASS ${name}`)
}

const w = async (p, s) => {
  await fs.mkdir(path.dirname(p), { recursive: true })
  await fs.writeFile(p, s)
}
const rd = (p) => fs.readFile(p, "utf-8")
const parse = async (p) => JSON.parse(stripJsonComments(await rd(p)))

/* A fixture that mirrors the real ~/.omo/omo.jsonc shape: // header
   comments, $schema, [opencode] block with multi-field agent entries and
   category entries carrying BOTH models[] and the model main field. */
const OMO_FIXTURE = `{
  // OMO configuration — keep me
  "$schema": "https://example.invalid/omo.schema.json",
  "[opencode]": {
    "agents": {
      "sisyphus": {
        "description": "flagship", // trailing note
        "ultrawork": { "model": "prov/fallback" },
        "model": "prov/old-sisy",
        "fallback_models": ["prov/fb1", "prov/fb2"],
        "thinking": { "type": "enabled", "budgetTokens": 8192 }
      },
      "oracle": {
        "model": "prov/old-oracle",
        "reasoning": "high"
      },
      "legacy": "prov/bare"
    },
    "categories": {
      "deep": {
        "description": "autonomous",
        "models": ["prov/keep1", { "model": "prov/keep2" }],
        "thinking": { "type": "enabled", "budgetTokens": 8192 },
        "model": "prov/old-deep"
      },
      "quick": { "model": "prov/old-quick" }
    },
    "team_mode": { "enabled": true }
  },
  "_migrations": ["a", "b"]
}
`

/* 1. setNestedModelsJsonc: update existing agent + category entries in
   place; every sibling field and comment survives byte-for-byte */
{
  const r = setNestedModelsJsonc(OMO_FIXTURE, ["[opencode]", "agents"], {
    sisyphus: "prov/new-sisy",
  })
  assert.match(r, /\/\/ OMO configuration — keep me/, "header comment preserved")
  assert.match(r, /\/\/ trailing note/, "inline note preserved")
  const j = JSON.parse(stripJsonComments(r))
  const s = j["[opencode]"].agents.sisyphus
  assert.equal(s.model, "prov/new-sisy")
  assert.equal(s.description, "flagship")
  assert.deepEqual(s.ultrawork, { model: "prov/fallback" }, "nested non-model field untouched")
  assert.deepEqual(s.fallback_models, ["prov/fb1", "prov/fb2"])
  assert.deepEqual(s.thinking, { type: "enabled", budgetTokens: 8192 })
  assert.equal(j["[opencode]"].categories.deep.model, "prov/old-deep", "other sections untouched")
  assert.equal(j._migrations.length, 2)

  const r2 = setNestedModelsJsonc(OMO_FIXTURE, ["[opencode]", "categories"], {
    deep: "prov/new-deep",
  })
  const j2 = JSON.parse(stripJsonComments(r2))
  const d = j2["[opencode]"].categories.deep
  assert.equal(d.model, "prov/new-deep")
  assert.deepEqual(d.models, ["prov/keep1", { model: "prov/keep2" }], "models[] array untouched")
  assert.equal(j2["[opencode]"].agents.oracle.model, "prov/old-oracle")
  ok("setNestedModelsJsonc: in-place model rewrite preserves siblings + comments")
}

/* 2. setNestedModelsJsonc: legacy bare-string entry stays a bare string */
{
  const r = setNestedModelsJsonc(OMO_FIXTURE, ["[opencode]", "agents"], { legacy: "prov/bare2" })
  const j = JSON.parse(stripJsonComments(r))
  assert.equal(j["[opencode]"].agents.legacy, "prov/bare2", "bare string shape preserved")
  ok("setNestedModelsJsonc: legacy string entry keeps its shape")
}

/* 3. setNestedModelsJsonc: missing names appended to the existing
   containers; missing chain levels created (categories container, whole
   [opencode] block); non-object value replaced, never duplicated */
{
  const r = setNestedModelsJsonc(OMO_FIXTURE, ["[opencode]", "agents"], {
    "brand-new": "prov/n1",
    "another": "prov/n2",
  })
  const j = JSON.parse(stripJsonComments(r))
  assert.equal(j["[opencode]"].agents["brand-new"].model, "prov/n1")
  assert.equal(j["[opencode]"].agents.another.model, "prov/n2")
  assert.equal(j["[opencode]"].agents.oracle.model, "prov/old-oracle", "existing entries survive")

  const withNoCats = `{
  // note
  "[opencode]": { "agents": { "oracle": { "model": "prov/a" } } }
}`
  const r2 = setNestedModelsJsonc(withNoCats, ["[opencode]", "categories"], { deep: "prov/d" })
  const j2 = JSON.parse(stripJsonComments(r2))
  assert.equal(j2["[opencode]"].categories.deep.model, "prov/d", "categories level created")
  assert.equal(j2["[opencode]"].agents.oracle.model, "prov/a")
  assert.match(r2, /\/\/ note/, "comment preserved through chain creation")
  assert.equal(r2.match(/"categories"/g).length, 1, "no duplicated key")

  const flat = `{"name": "x"}`
  const r3 = setNestedModelsJsonc(flat, ["[opencode]", "agents"], { oracle: "prov/m" })
  const j3 = JSON.parse(stripJsonComments(r3))
  assert.equal(j3["[opencode]"].agents.oracle.model, "prov/m", "[opencode] block created at root")
  assert.equal(j3.name, "x")

  const broken = `{"[opencode]": {"agents": null}}`
  const r4 = setNestedModelsJsonc(broken, ["[opencode]", "agents"], { oracle: "prov/m" })
  const j4 = JSON.parse(stripJsonComments(r4))
  assert.equal(j4["[opencode]"].agents.oracle.model, "prov/m", "null value replaced by object")
  assert.equal(r4.match(/"agents"/g).length, 1, "no duplicated key on replace")
  ok("setNestedModelsJsonc: creates missing names, chain levels, replaces non-objects")
}

/* 4. setNestedModelsJsonc: malformed input never produces output */
{
  assert.throws(() => setNestedModelsJsonc(`{"agent": {`, ["agent"], { x: "p/m" }))
  assert.throws(() => setNestedModelsJsonc(`[1,2]`, ["agent"], { x: "p/m" }))
  assert.equal(setNestedModelsJsonc(OMO_FIXTURE, ["[opencode]", "agents"], {}), OMO_FIXTURE)
  ok("setNestedModelsJsonc: unbalanced/non-object input throws, empty update is identity")
}

/* 5. setAgentModelsJsonc wrapper keeps byte-identical legacy output when
   creating the agent block (single-key chain) */
{
  const legacy = setNestedModelsJsonc(`{\n  "name": "x"\n}`, ["agent"], { oracle: "prov/m" })
  assert.match(legacy, /,\n {2}"agent": \{\n {4}"oracle": \{\n {6}"model": "prov\/m"\n {4}\}\n {2}\}/)
  ok("setNestedModelsJsonc(['agent']): byte-compatible with the legacy creator format")
}

/* 6. resolveOmoUserConfigFile + readOmoConfig: jsonc wins, json fallback,
   missing file, unparseable file, agent-wins collision, base vs block */
{
  const env = { HOME }
  await w(OMO_FILE, OMO_FIXTURE)
  assert.equal(await resolveOmoUserConfigFile(env), OMO_FILE)

  const cfg = await readOmoConfig(env, HOME)
  assert.equal(cfg.exists, true)
  assert.equal(cfg.parseable, true)
  assert.equal(cfg.targets.sisyphus.kind, "agent")
  assert.equal(cfg.targets.sisyphus.model, "prov/old-sisy")
  assert.equal(cfg.targets.legacy.model, "prov/bare", "bare-string entry parsed")
  assert.equal(cfg.targets.deep.kind, "category")
  assert.equal(cfg.targets.deep.model, "prov/keep1", "category primary = models[0]; scalar ignored at runtime")
  assert.equal(cfg.targets.deep.legacyScalar, "prov/old-deep")
  assert.equal(cfg.targets.deep.legacyFallbacks, undefined)
  assert.deepEqual(cfg.targets.deep.models, ["prov/keep1", { model: "prov/keep2" }])
  assert.equal(cfg.targets.quick.model, "prov/old-quick", "scalar-only category: scalar answers alone")
  assert.equal(cfg.targets.quick.legacyScalar, "prov/old-quick")
  assert.equal(cfg.targets.team_mode, undefined, "non-agent/category sections ignored")
  assert.equal(isOmoName(cfg, "quick"), true)
  assert.equal(omoTargetKind(cfg, "deep"), "category")
  assert.equal(omoPortableFile(cfg, HOME, CONFIG_DIR), "${HOME}/.omo/omo.jsonc")

  // json fallback when jsonc absent
  await fs.rm(OMO_FILE)
  const jsonFile = path.join(OMO_DIR, "omo.json")
  await w(jsonFile, `{"[opencode]":{"agents":{"oracle":{"model":"prov/j"}}}}`)
  assert.equal(await resolveOmoUserConfigFile(env), jsonFile)
  assert.equal((await readOmoConfig(env, HOME)).targets.oracle.model, "prov/j")

  // unparseable
  await w(jsonFile, "{oops")
  const bad = await readOmoConfig(env, HOME)
  assert.equal(bad.exists, true)
  assert.equal(bad.parseable, false)
  assert.deepEqual(bad.targets, {})

  // collision + precedence: top-level agent + block category with the same
  // name → agent wins; block model beats the base-level model
  await fs.rm(jsonFile)
  await w(
    OMO_FILE,
    `{
  "agents": { "dup": { "model": "prov/base-agent" } },
  "[opencode]": {
    "agents": { "dup": { "model": "prov/block-agent" } },
    "categories": { "dup": { "model": "prov/block-cat" } }
  }
}`,
  )
  const prec = await readOmoConfig(env, HOME)
  assert.equal(prec.targets.dup.kind, "agent", "agent kind wins on name collision")
  assert.equal(prec.targets.dup.model, "prov/block-agent", "[opencode] block beats base keys")

  // missing file entirely: builtin categories AND agents are seeded as
  // unbound targets (routing works on a fresh machine), nothing else leaks in
  await fs.rm(OMO_FILE)
  const none = await readOmoConfig({ HOME: path.join(TMP, "nohome") }, HOME)
  assert.equal(none.exists, false)
  assert.equal(none.file, null)
  assert.deepEqual(none.targets.deep, { kind: "category", model: null, models: null })
  assert.deepEqual(none.targets.oracle, { kind: "agent", model: null, models: null })
  assert.equal(
    Object.keys(none.targets).length,
    OMO_STATIC_CATEGORIES.length + OMO_STATIC_AGENTS.length,
  )
  assert.ok(OMO_STATIC_CATEGORIES.every((c) => none.targets[c]?.kind === "category"))
  assert.ok(OMO_STATIC_AGENTS.every((a) => none.targets[a]?.kind === "agent"))
  assert.equal(none.targets["totally-made-up"], undefined, "unknown names stay unseeded")
  ok("readOmoConfig: detection order, precedence, collision, error states")
}

/* 7. project .omo configs shadow the user file → reported in shadowFiles */
{
  await w(OMO_FILE, OMO_FIXTURE)
  const proj = path.join(TMP, "proj")
  await w(path.join(proj, ".git", "HEAD"), "")
  const cwd = path.join(proj, "sub")
  await w(path.join(cwd, ".omo", "omo.jsonc"), `{"agents":{}}`)
  const cfg = await readOmoConfig({ HOME }, cwd)
  assert.deepEqual(cfg.shadowFiles, [path.join(cwd, ".omo", "omo.jsonc")])
  ok("readOmoConfig: project-level omo configs reported as shadow files")
}

/* 8. writeOmoModels: routing (category vs agent vs unknown), backup,
   comment preservation, postcondition verification */
{
  await w(OMO_FILE, OMO_FIXTURE)
  const res = await writeOmoModels(
    { sisyphus: "prov/new-sisy", deep: "prov/new-deep", "circuit-engineer": "prov/ce" },
    { HOME },
    HOME,
  )
  assert.equal(res.written, true, res.error)
  assert.equal(res.file, OMO_FILE)
  assert.ok(res.backup && res.backup.includes(".bak-"), "timestamped backup created")
  const bakText = await rd(res.backup)
  assert.equal(bakText, OMO_FIXTURE, "backup is the exact pre-write content")
  assert.match(bakText, /\/\/ OMO configuration — keep me/)

  const j = await parse(OMO_FILE)
  assert.equal(j["[opencode]"].agents.sisyphus.model, "prov/new-sisy")
  assert.equal(j["[opencode]"].agents["circuit-engineer"].model, "prov/ce", "unknown → agents")
  assert.deepEqual(
    j["[opencode]"].categories.deep.models,
    ["prov/new-deep", { model: "prov/keep2" }],
    "chain tail preserved, primary swapped",
  )
  assert.equal(
    j["[opencode]"].categories.deep.model,
    undefined,
    "superseded legacy scalar dropped from the category entry",
  )
  assert.equal(j["[opencode]"].categories.deep.description, "autonomous", "category siblings survive")
  assert.deepEqual(j["[opencode]"].categories.deep.thinking, { type: "enabled", budgetTokens: 8192 })
  ok("writeOmoModels: routes by kind, backs up, preserves comments and siblings")
}

/* 8b. category rebind CANONICALIZES the chain: the tail survives, only the
    primary is swapped, superseded scalar keys vanish. Explicit spec.models
    (revert path) restores the array verbatim. */
{
  await w(OMO_FILE, OMO_FIXTURE)
  const res = await writeOmoModels(
    { deep: "prov/rebind", quick: "prov/quick-new" },
    { HOME },
    HOME,
  )
  assert.equal(res.written, true, res.error)
  let j = await parse(OMO_FILE)
  assert.deepEqual(j["[opencode]"].categories.deep.models, ["prov/rebind", { model: "prov/keep2" }])
  assert.equal(j["[opencode]"].categories.deep.model, undefined, "no legacy scalar left")
  assert.equal(j["[opencode]"].categories.deep.fallback_models, undefined)
  assert.deepEqual(
    j["[opencode]"].categories.quick.models,
    ["prov/quick-new"],
    "scalar-only legacy category folds to the canonical chain",
  )
  assert.equal(j["[opencode]"].categories.quick.model, undefined, "superseded scalar removed")
  assert.equal(j["[opencode]"].agents.oracle.model, "prov/old-oracle", "untouched agents intact")

  const restore = await writeOmoModels(
    { deep: { model: "prov/keep1", models: ["prov/keep1", { model: "prov/keep2" }] } },
    { HOME },
    HOME,
  )
  assert.equal(restore.written, true, restore.error)
  j = await parse(OMO_FILE)
  assert.deepEqual(j["[opencode]"].categories.deep.models, ["prov/keep1", { model: "prov/keep2" }])
  assert.equal(
    j["[opencode]"].categories.deep.model,
    undefined,
    "revert keeps the entry canonical — no scalar keys",
  )

  // agent entries are NOT canonicalized (runtime reads only .model)
  await w(OMO_FILE, OMO_FIXTURE)
  const ag = await writeOmoModels({ sisyphus: "prov/agent-new" }, { HOME }, HOME)
  assert.equal(ag.written, true, ag.error)
  j = await parse(OMO_FILE)
  assert.equal(j["[opencode]"].agents.sisyphus.model, "prov/agent-new")
  assert.deepEqual(
    j["[opencode]"].agents.sisyphus.fallback_models,
    ["prov/fb1", "prov/fb2"],
    "agent fallback_models untouched",
  )
  assert.equal(
    j["[opencode]"].agents.sisyphus.models,
    undefined,
    "no models chain invented for agents",
  )
  assert.match(await rd(OMO_FILE), /\/\/ trailing note/, "agent inner comment survives fast path")
  ok("writeOmoModels: category chain canonicalized; spec.models verbatim; agents scalar-only")
}

/* 8c. legacy `{model, fallback_models}` category → canonical `models` chain
   with the old fallbacks kept as tail (fold, then swap the primary). */
{
  await w(
    OMO_FILE,
    `{"[opencode]": {"categories": {"mixed": {"model": "prov/old", "fallback_models": ["prov/y"]}}}}`,
  )
  const res = await writeOmoModels({ mixed: "prov/M" }, { HOME }, HOME)
  assert.equal(res.written, true, res.error)
  const j = await parse(OMO_FILE)
  assert.deepEqual(j["[opencode]"].categories.mixed, { models: ["prov/M", "prov/y"] })
  ok("writeOmoModels: legacy scalar+fallback_models folds into the canonical chain")
}

/* 8d. object chain heads keep every non-model setting through a primary
   swap; string/object tail entries are untouched. */
{
  await w(
    OMO_FILE,
    `{
  "[opencode]": {
    "categories": {
      "objcat": {
        "models": [
          { "model": "prov/old", "thinking": { "type": "enabled" }, "temperature": 0.2 },
          "prov/tail1",
          { "model": "prov/tail2" }
        ]
      }
    }
  }
}`,
  )
  const res = await writeOmoModels({ objcat: "prov/M" }, { HOME }, HOME)
  assert.equal(res.written, true, res.error)
  const j = await parse(OMO_FILE)
  assert.deepEqual(j["[opencode]"].categories.objcat.models, [
    { model: "prov/M", thinking: { type: "enabled" }, temperature: 0.2 },
    "prov/tail1",
    { model: "prov/tail2" },
  ])
  ok("writeOmoModels: object head keeps its settings; full tail survives")
}

/* 8e. an agent carrying BOTH .model and .models keeps both untouched fields
   after a write (dead weight stays; only .model is swapped). */
{
  await w(
    OMO_FILE,
    `{"[opencode]": {"agents": {"both": {"model": "prov/old", "models": ["prov/x"]}}}}`,
  )
  const res = await writeOmoModels({ both: "prov/M" }, { HOME }, HOME)
  assert.equal(res.written, true, res.error)
  const j = await parse(OMO_FILE)
  assert.deepEqual(j["[opencode]"].agents.both, { model: "prov/M", models: ["prov/x"] })
  ok("writeOmoModels: agent write touches only .model, leaves coexisting .models alone")
}

/* 8f. post-write binding simulation: the delegate path resolves a category
   seat as models[0].model ?? models[0]; the agent path as .model. */
{
  await w(OMO_FILE, OMO_FIXTURE)
  const res = await writeOmoModels(
    { deep: "prov/M-deep", quick: "prov/M-quick", oracle: "prov/M-oracle" },
    { HOME },
    HOME,
  )
  assert.equal(res.written, true, res.error)
  const j = await parse(OMO_FILE)
  const delegate = (name) => {
    const head = j["[opencode]"].categories[name].models[0]
    return typeof head === "string" ? head : head.model
  }
  assert.equal(delegate("deep"), "prov/M-deep", "delegate seat for object-tail chain")
  assert.equal(delegate("quick"), "prov/M-quick", "delegate seat for folded legacy chain")
  assert.equal(j["[opencode]"].agents.oracle.model, "prov/M-oracle", "agent seat reads .model")
  ok("writeOmoModels: runtime seats resolve to the requested models")
}

/* 8g. verifyWritten rejects a residual legacy scalar next to the chain and
   accepts the canonical models-only shape. */
{
  const dirty = `{"[opencode]": {"categories": {"deep": {"models": ["prov/M"], "model": "prov/stale"}}}}`
  assert.throws(
    () => verifyWritten(dirty, { agents: {}, categories: { deep: ["prov/M"] } }),
    /legacy model\/fallback_models keys/,
  )
  const staleTail = `{"[opencode]": {"categories": {"deep": {"models": ["prov/other"]}}}}`
  assert.throws(
    () => verifyWritten(staleTail, { agents: {}, categories: { deep: ["prov/M"] } }),
    /models != /,
  )
  const clean = `{"[opencode]": {"categories": {"deep": {"models": ["prov/M"]}}}}`
  verifyWritten(clean, { agents: {}, categories: { deep: ["prov/M"] } })
  ok("verifyWritten: canonical chain required, residual scalar rejected")
}

/* 9. writeOmoModels: creates a missing file with only the needed skeleton */
{
  const freshHome = path.join(TMP, "fresh")
  const res = await writeOmoModels({ oracle: "prov/x" }, { HOME: freshHome }, freshHome)
  assert.equal(res.written, true, res.error)
  assert.equal(res.backup, null, "nothing to back up")
  const j = await parse(res.file)
  assert.equal(j["[opencode]"].agents.oracle.model, "prov/x")
  assert.equal(res.file, path.join(freshHome, ".omo", "omo.jsonc"), "created as omo.jsonc")
  ok("writeOmoModels: fresh-file creation lands in [opencode].agents")
}

/* 9b. fresh machine (no OMO config at all): builtin category AND agent
    names are known targets, so binds create [opencode].categories|agents —
    never opencode's cfg.agent (phantom-role trap, prod incident 2026-09-07) */
{
  const freshHome = path.join(TMP, "fresh2")
  const cfgFresh = await readOmoConfig({ HOME: freshHome }, freshHome)
  assert.equal(cfgFresh.exists, false)
  assert.equal(cfgFresh.targets.deep?.kind, "category", "builtin category seeded without a file")
  assert.equal(cfgFresh.targets.oracle?.kind, "agent", "builtin agent seeded without a file")

  const res = await writeOmoModels({ deep: "prov/cat-bind" }, { HOME: freshHome }, freshHome)
  assert.equal(res.written, true, res.error)
  const j = await parse(path.join(freshHome, ".omo", "omo.jsonc"))
  assert.deepEqual(
    j["[opencode]"].categories.deep.models,
    ["prov/cat-bind"],
    "created entry carries the canonical chain",
  )
  assert.equal(j["[opencode]"].categories.deep.model, undefined, "created entry has no legacy scalar")
  assert.equal(j["[opencode]"].agents, undefined, "no agents skeleton written")
  ok("writeOmoModels: fresh-machine category bind routes to categories, not phantom agents")

  // fresh-machine AGENT bind: routes into [opencode].agents of a created
  // omo.jsonc — never into the opencode config layer
  const agFresh = path.join(TMP, "fresh3")
  const agRes = await writeOmoModels({ sisyphus: "prov/sisy-bind" }, { HOME: agFresh }, agFresh)
  assert.equal(agRes.written, true, agRes.error)
  const agJ = await parse(path.join(agFresh, ".omo", "omo.jsonc"))
  assert.equal(agJ["[opencode]"].agents.sisyphus.model, "prov/sisy-bind")
  assert.equal(agJ.agent, undefined, "opencode-shaped top-level agent key never written")
  ok("writeOmoModels: fresh-machine agent bind creates [opencode].agents")
}

/* 10. writeOmoModels failure semantics: unparseable file refuses to write;
    no leftover backup/temp files, original bytes intact */
{
  await w(OMO_FILE, "{definitely not json")
  for (const n of await fs.readdir(OMO_DIR)) {
    if (n.includes(".bak-") || n.startsWith(".fastdraw-tmp-")) {
      await fs.rm(path.join(OMO_DIR, n), { force: true })
    }
  }
  const before = await rd(OMO_FILE)
  const res = await writeOmoModels({ oracle: "prov/y" }, { HOME }, HOME)
  assert.equal(res.written, false)
  assert.match(res.error, /unparseable/)
  assert.equal(await rd(OMO_FILE), before, "file untouched")
  const residue = (await fs.readdir(OMO_DIR)).filter(
    (n) => n.includes(".bak-") || n.startsWith(".fastdraw-tmp-"),
  )
  assert.deepEqual(residue, [], "no backup or temp residue on refused write")

  // jsonc wins so a valid jsonc + corrupt json still writes to the jsonc
  await w(OMO_FILE, OMO_FIXTURE)
  await w(path.join(OMO_DIR, "omo.json"), "{broken-json")
  const res2 = await writeOmoModels({ oracle: "prov/z" }, { HOME }, HOME)
  assert.equal(res2.written, true, res2.error)
  assert.equal(await rd(path.join(OMO_DIR, "omo.json")), "{broken-json", "loser file untouched")
  ok("writeOmoModels: refuses unparseable targets, cleans up, jsonc shield keeps json safe")
}

await fs.rm(TMP, { recursive: true, force: true })
console.log(`\n${pass} tests passed`)
