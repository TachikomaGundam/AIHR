/* FastDraw origins module tests — config layer collection, precedence
   harvest, conflict detection, portable path scheme, v1/v2 entry
   normalization. Run via `npm test` (pretest transpiles origins.ts with
   esbuild into test/.build/origins.mjs). All fixtures live under a temp dir;
   nothing under the real ~/.config is touched. */
import assert from "node:assert"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"

const TMP = await fs.mkdtemp(path.join(os.tmpdir(), "fd-origins-"))
process.env.HOME = path.join(TMP, "home")
const HOME = process.env.HOME

/* ── fixture tree ────────────────────────────────────────────────── */
// fake home: global config dir with all three files (jsonc wins)
// fake project repo with project configs + a nested cwd
// fake $OPENCODE_CONFIG single file, $OPENCODE_CONFIG_DIR dir
const CONFIG_DIR = path.join(HOME, ".config", "opencode")
const PROJ = path.join(TMP, "proj")
const CWD = path.join(PROJ, "sub")
const ENV_DIR = path.join(TMP, "cfgdir-extra")

const w = async (p, s) => {
  await fs.mkdir(path.dirname(p), { recursive: true })
  await fs.writeFile(p, s)
}

const j = (x) => JSON.stringify(x, null, 2)

await w(path.join(CONFIG_DIR, "config.json"), j({ agent: { oracle: { model: "prov/z" } } }))
await w(
  path.join(CONFIG_DIR, "opencode.json"),
  j({ agent: { oracle: { model: "prov/y" } } }),
)
await w(
  path.join(CONFIG_DIR, "opencode.jsonc"),
  `{\n  // global jsonc — highest global precedence\n  "agent": {\n    "oracle": { "model": "prov/a" },\n    "clash": { "model": "prov/c1" }\n  }\n}\n`,
)
await w(path.join(PROJ, ".git", "HEAD"), "")
await w(path.join(PROJ, "opencode.json"), j({ agent: { oracle: { model: "prov/b" } } }))
await w(
  path.join(PROJ, "opencode.jsonc"),
  j({ agent: { oracle: { model: "prov/c" }, "proj-only": { model: "prov/p" } } }),
)
await w(path.join(PROJ, ".opencode", "opencode.json"), j({ agent: { explorer: { model: "prov/d" } } }))
await w(
  path.join(PROJ, ".opencode", "agents", "explorer.md"),
  "---\nmode: subagent\nmodel: prov/e\n---\n# explorer agent\n",
)
await w(
  path.join(HOME, ".opencode", "opencode.jsonc"),
  j({ agent: { atlas: { model: "prov/f" } } }),
)
await w(
  path.join(ENV_DIR, "opencode.jsonc"),
  j({ agent: { clash: { model: "prov/c2" }, "cfgdir-agent": { model: "prov/g" } } }),
)
await w(path.join(TMP, "env-one.json"), j({ agent: { "env-agent": { model: "prov/h" } } }))

const mod = await import(new URL("./.build/origins.mjs", import.meta.url))
const {
  toPortablePath,
  resolvePortablePath,
  stripJsonComments,
  parseFrontmatterModel,
  findProjectRoot,
  collectLayerFiles,
  harvest,
  formatConflictReport,
  entryModel,
  sectionToBindings,
  bindingsToFlat,
  splitBindings,
} = mod

let pass = 0
const ok = (name) => { pass++; console.log(`PASS ${name}`) }

const ctx = {
  home: HOME,
  configDir: CONFIG_DIR,
  projectRoot: PROJ,
  cwd: CWD,
  env: {
    OPENCODE_CONFIG: path.join(TMP, "env-one.json"),
    OPENCODE_CONFIG_DIR: ENV_DIR,
    OPENCODE_CONFIG_CONTENT: j({ agent: { "inline-agent": { model: "prov/i" } } }),
  },
}

/* 1. toPortablePath: every known root → placeholder form, always / separators */
{
  const cases = [
    [path.join(CONFIG_DIR, "opencode.jsonc"), "${CONFIG_DIR}/opencode.jsonc"],
    [path.join(HOME, ".opencode", "opencode.jsonc"), "${HOME}/.opencode/opencode.jsonc"],
    [path.join(PROJ, "sub", "deep", "x.json"), "${PROJECT}/sub/deep/x.json"],
    [path.join(TMP, "elsewhere.json"), null],
  ]
  for (const [abs, want] of cases) {
    assert.equal(toPortablePath(abs, ctx), want, `${abs} → ${want}`)
  }
  ok("toPortablePath encodes configDir/home/project, rejects outside paths")
}

/* 2. resolvePortablePath: placeholders resolve on ANOTHER machine's roots */
{
  const otherHome = "/home/other-user"
  const other = { home: otherHome, configDir: path.join(otherHome, ".config", "opencode"), projectRoot: path.join(otherHome, "repo") }
  assert.equal(
    resolvePortablePath("${CONFIG_DIR}/opencode.jsonc", other),
    path.join(otherHome, ".config", "opencode", "opencode.jsonc"),
    "CONFIG_DIR resolves under the other machine's home",
  )
  assert.equal(
    resolvePortablePath("${HOME}/.config/opencode/opencode.jsonc", other),
    path.join(otherHome, ".config", "opencode", "opencode.jsonc"),
  )
  const p = resolvePortablePath("${HOME}/a/b/c.json", other)
  assert.equal(p, path.join(otherHome, "a", "b", "c.json"), "stored / separators rejoin with the local sep")
  assert.equal(
    resolvePortablePath("${PROJECT}/x.json", { home: otherHome, configDir: path.join(otherHome, ".config", "opencode") }),
    null,
    "unresolvable placeholder → null",
  )
  ok("resolvePortablePath works cross-machine, ${PROJECT} without project → null")
}

/* 3. stripJsonComments: quote-aware, keeps URLs inside strings */
{
  const s = `{\n  // comment\n  "url": "https://a/b?x=1//2", /* block */ "m": "prov/x" // tail\n}`
  const out = stripJsonComments(s)
  assert.ok(out.includes('"url": "https://a/b?x=1//2"'), "slashes inside strings survive")
  assert.ok(!out.includes("// comment"), "line comment removed")
  assert.ok(!out.includes("/* block */"), "block comment removed")
  assert.ok(!out.includes("// tail"), "trailing comment removed")
  assert.ok(out.endsWith("}"), "structure intact")
  ok("stripJsonComments strips comments without touching strings")
}

/* 4. parseFrontmatterModel: quoted/unquoted model lines, no-match */
{
  assert.equal(parseFrontmatterModel("---\nmodel: prov/x\n---\nbody"), "prov/x")
  assert.equal(parseFrontmatterModel('---\nmodel: "prov/y" # comment\n---\n'), "prov/y")
  assert.equal(parseFrontmatterModel("---\nmode: subagent\n---\n"), null)
  assert.equal(parseFrontmatterModel("no frontmatter here"), null)
  ok("parseFrontmatterModel extracts quoted/unquoted model lines")
}

/* 5. findProjectRoot: nearest ancestor with .git; fallback = cwd */
{
  assert.equal(await findProjectRoot(CWD), PROJ)
  const plain = path.join(TMP, "norepo")
  await fs.mkdir(plain, { recursive: true })
  assert.equal(await findProjectRoot(plain), plain)
  ok("findProjectRoot finds .git ancestor, falls back to cwd")
}

/* 6. collectLayerFiles: precedence order global → env → project → .opencode */
{
  const files = await collectLayerFiles(ctx)
  const seq = files.map((f) => `${f.layer}:${path.basename(f.file ?? "(inline)")}`)
  assert.deepEqual(seq, [
    "global:config.json",
    "global:opencode.json",
    "global:opencode.jsonc",
    "env:env-one.json",
    "project:opencode.json",
    "project:opencode.jsonc",
    ".opencode:opencode.json", // project .opencode
    ".opencode:explorer.md", // md AFTER json within the same dir (md wins)
    ".opencode:opencode.jsonc", // home .opencode
    ".opencode:opencode.jsonc", // $OPENCODE_CONFIG_DIR — appended last (wins)
    "inline:(inline)",
  ], `layer order wrong: ${seq.join(" → ")}`)
  assert.equal(files[3].portable, null, "paths outside every root carry no portable form")
  assert.equal(files[0].portable, "${CONFIG_DIR}/config.json", "global files use CONFIG_DIR")
  ok("collectLayerFiles returns layers in opencode precedence order")
}

/* 7. harvest: last-write-wins across layers, md beats json within a dir */
{
  const h = await harvest(ctx)
  assert.equal(h.agents.oracle.model, "prov/c", "project opencode.jsonc beats global + project opencode.json")
  assert.equal(h.agents.oracle.origin?.layer, "project")
  assert.equal(h.agents.oracle.origin?.file, "${PROJECT}/opencode.jsonc", "origin stores the portable form")
  assert.equal(h.agents.explorer.model, "prov/e", "agents/*.md frontmatter beats opencode.json in same .opencode dir")
  assert.equal(h.agents.atlas.model, "prov/f", "home .opencode applies")
  assert.equal(h.agents["env-agent"].model, "prov/h")
  assert.equal(h.agents["cfgdir-agent"].model, "prov/g")
  assert.equal(h.agents["inline-agent"].model, "prov/i", "inline content seen (origin has no file)")
  assert.equal(h.agents["inline-agent"].origin?.file, null)
  ok("harvest computes effective bindings with correct origins")
}

/* 8. conflicts: same role → different models in multiple layers */
{
  const h = await harvest(ctx)
  const conf = h.conflicts.find((c) => c.role === "clash")
  assert.ok(conf, "clash role reported as conflict")
  assert.equal(conf?.entries.length, 2)
  assert.equal(conf?.winner, "prov/c2", "$OPENCODE_CONFIG_DIR is appended last, so it wins")
  const report = formatConflictReport(h.conflicts)
  assert.match(report, /⚠ Conflicts/)
  assert.match(report, /clash/)
  assert.match(report, /→ prov\/c1/)
  assert.match(report, /→ prov\/c2.*wins/)
  assert.equal(formatConflictReport([]), "")
  ok("conflict detection reports each location + precedence winner")
}

/* 9. entry normalization: v1 strings and v2 {model, origin} entries coexist */
{
  assert.deepEqual(entryModel("prov/x"), { model: "prov/x" })
  assert.deepEqual(entryModel({ model: "prov/y", origin: { layer: "state", file: "${CONFIG_DIR}/.fastdraw.json" } }), {
    model: "prov/y",
    origin: { layer: "state", file: "${CONFIG_DIR}/.fastdraw.json" },
  })
  assert.equal(entryModel("no-slash"), null)
  assert.equal(entryModel({ model: 7 }), null)
  assert.equal(entryModel({ origin: { layer: "x" } }), null, "object without model rejected")
  const s = sectionToBindings({
    oracle: "prov/o",
    router: { model: "prov/r", origin: { layer: "global", file: "${CONFIG_DIR}/opencode.jsonc" } },
    broken: { model: "nope" },
  })
  assert.deepEqual(Object.keys(s).sort(), ["oracle", "router"])
  assert.deepEqual(bindingsToFlat(s), { oracle: "prov/o", router: "prov/r" })
  const split = splitBindings({ oracle: { model: "prov/o" }, router: { model: "prov/r" } })
  assert.deepEqual(Object.keys(split.omo), ["oracle"])
  assert.deepEqual(Object.keys(split.custom), ["router"])
  ok("entryModel/sectionToBindings normalize v1+v2, splitBindings separates standard vs custom")
}

/* 10. inline env content is read-only: effective, but no origin file */
{
  const pristine = path.join(TMP, "pristine")
  const h = await harvest({
    home: path.join(pristine, "home"),
    configDir: path.join(pristine, "home", ".config", "opencode"),
    projectRoot: pristine,
    cwd: pristine,
    env: { OPENCODE_CONFIG_CONTENT: j({ agent: { only: { model: "prov/i" } } }) },
  })
  assert.deepEqual(Object.keys(h.agents), ["only"])
  assert.equal(h.agents.only.origin?.file, null)
  ok("inline content harvests with null origin (falls back to global on restore)")
}

/* 11. write-side: surgical JSONC editing preserves comments/whitespace,
   legacy string shapes and untouched keys; frontmatter model set */
{
  const t1 = `{\n  // top note\n  "agent": {\n    "oracle": { "model": "prov/a" }, // oracle model\n    "pcb-router": "prov/b"\n  },\n  "name": "x"\n}`
  const r1 = mod.setAgentModelsJsonc(t1, { oracle: "prov/a2", explorer: "prov/c" })
  assert.match(r1, /\/\/ top note/, "root comment preserved")
  assert.match(r1, /\/\/ oracle model/, "inline comment preserved")
  const j1 = JSON.parse(mod.stripJsonComments(r1))
  assert.equal(j1.agent.oracle.model, "prov/a2")
  assert.equal(j1.agent["pcb-router"], "prov/b", "untouched role keeps legacy string shape")
  assert.equal(j1.agent.explorer.model, "prov/c", "new role inserted into agent object")
  assert.equal(j1.name, "x")
  const r2 = mod.setAgentModelsJsonc(`{\n  "name": "x"\n}`, { oracle: "prov/m" })
  const j2 = JSON.parse(mod.stripJsonComments(r2))
  assert.equal(j2.agent.oracle.model, "prov/m", "agent object created when missing")
  const r3 = mod.setAgentModelsJsonc(
    `{"agent": {"oracle": {"model": "a", "mode": "subagent"}}}`,
    { oracle: "b" },
  )
  const j3 = JSON.parse(mod.stripJsonComments(r3)).agent.oracle
  assert.equal(j3.model, "b")
  assert.equal(j3.mode, "subagent", "other keys in the entry survive")
  const r4 = mod.setAgentModelsJsonc(`{"agent": {"oracle": "a",},}`, { oracle: "z" })
  assert.equal(JSON.parse(mod.stripJsonComments(r4)).agent.oracle, "z", "trailing commas tolerated")
  const fm1 = mod.setFrontmatterModel("---\nname: x\n---\nbody", "prov/n")
  assert.match(fm1, /^---\nmodel: prov\/n\nname: x\n---\nbody$/, "model line inserted after ---")
  const fm2 = mod.setFrontmatterModel("---\nmodel: old/m\nname: x\n---\nbody", "prov/new")
  assert.match(fm2, /model: prov\/new/, "existing model line replaced")
  assert.match(fm2, /name: x/, "other frontmatter keys kept")
  const fm3 = mod.setFrontmatterModel("plain body, no frontmatter", "prov/p")
  assert.match(fm3, /^---\nmodel: prov\/p\n---\nplain body/, "frontmatter block created")
  assert.match(
    mod.backupName("/x/y.jsonc", new Date("2026-08-19T12:34:56")),
    /^\/x\/y\.jsonc\.bak-20260819-123456$/,
    "backup name is Windows-safe, no colons",
  )
  ok("setAgentModelsJsonc/setFrontmatterModel: comment-preserving surgical edits")
}

/* 12. write-side: planRestore modes + restoreWrite filesystem behavior */
{
  const wd = path.join(TMP, "writetest")
  const cfgDir = path.join(wd, ".config", "opencode")
  await w(path.join(cfgDir, "opencode.jsonc"), `{\n  // keep me\n  "agent": {\n    "oracle": { "model": "prov/old" }\n  }\n}\n`)
  await w(path.join(cfgDir, "agents", "atlas.md"), "---\nname: atlas\n---\nno model in frontmatter")
  const ctx = { home: wd, configDir: cfgDir, cwd: wd, env: {} }
  const plan = await mod.planRestore(
    {
      oracle: { model: "prov/new", origin: { layer: "global", file: null } },
      "pcb-router": { model: "prov/ok", origin: { layer: "global", file: null } },
    },
    "original",
    ctx,
  )
  assert.deepEqual(plan.fallback, ["oracle", "pcb-router"], "origin-less roles fall back")
  assert.equal(plan.files.length, 1)
  assert.equal(plan.files[0].file, path.join(cfgDir, "opencode.jsonc"))
  const out = await mod.restoreWrite(plan, new Date("2026-08-19T12:34:56"))
  const txt = await fs.readFile(path.join(cfgDir, "opencode.jsonc"), "utf-8")
  assert.match(txt, /\/\/ keep me/, "comments survive a real write")
  const jw = JSON.parse(mod.stripJsonComments(txt))
  assert.equal(jw.agent.oracle.model, "prov/new")
  assert.equal(jw.agent["pcb-router"].model, "prov/ok", "fallback roles land in global target")
  assert.equal(out[0].backup, path.join(cfgDir, "opencode.jsonc.bak-20260819-123456"))
  assert.equal(
    await fs.readFile(out[0].backup, "utf-8"),
    `{\n  // keep me\n  "agent": {\n    "oracle": { "model": "prov/old" }\n  }\n}\n`,
    "backup holds pre-restore bytes",
  )
  assert.ok(
    !(await fs.readdir(cfgDir)).some((n) => n.includes("fastdraw-tmp")),
    "no temp-file leftovers after atomic write",
  )
  const pMd = await mod.planRestore(
    { atlas: { model: "prov/md", origin: { layer: ".opencode", file: "${CONFIG_DIR}/agents/atlas.md" } } },
    "original",
    ctx,
  )
  assert.equal(pMd.files[0].kind, "md")
  await mod.restoreWrite(pMd)
  assert.match(await fs.readFile(path.join(cfgDir, "agents", "atlas.md"), "utf-8"), /model: prov\/md/, "md frontmatter written")
  const pState = await mod.planRestore(
    { oracle: { model: "prov/x", origin: { layer: "state", file: "${CONFIG_DIR}/.fastdraw.json" } } },
    "original",
    ctx,
  )
  assert.deepEqual(pState.fromState, ["oracle"])
  assert.equal(pState.files.length, 0, "state-origin bindings need no config write")
  const cwdPrj = path.join(wd, "no-project-here")
  const pPrj = await mod.planRestore(
    { oracle: { model: "prov/p", origin: { layer: "project", file: "${PROJECT}/opencode.jsonc" } } },
    "original",
    { home: wd, configDir: cfgDir, cwd: cwdPrj, env: {} },
  )
  assert.equal(pPrj.files.length, 1)
  assert.equal(pPrj.files[0].file, path.join(cwdPrj, "opencode.jsonc"), "${PROJECT} resolves to the active project context")
  assert.equal(pPrj.files[0].create, true)
  const pPath = await mod.planRestore(
    { oracle: { model: "prov/t", origin: null } },
    "path",
    ctx,
    path.join(TMP, "new-config", "target.jsonc"),
  )
  assert.equal(pPath.files.length, 1)
  assert.equal(pPath.files[0].create, true)
  await mod.restoreWrite(pPath)
  const jt = JSON.parse(mod.stripJsonComments(await fs.readFile(pPath.files[0].file, "utf-8")))
  assert.equal(jt.agent.oracle.model, "prov/t", "path mode creates a fresh config file")
  const pGlobal = await mod.planRestore(
    { oracle: { model: "prov/g1", origin: null }, explorer: { model: "prov/g2", origin: null } },
    "global",
    { home: wd, configDir: cfgDir, cwd: wd, env: {} },
  )
  assert.equal(pGlobal.files.length, 1)
  assert.deepEqual(Object.keys(pGlobal.files[0].entries).sort(), ["explorer", "oracle"], "global mode merges every role into one file")
  ok("planRestore/restoreWrite: backups, comment-preserving writes, fallback rules")
}

await fs.rm(TMP, { recursive: true, force: true })
console.log(`\n${pass}/12 tests passed`)