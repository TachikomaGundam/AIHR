/* FastDraw home-reach tests — hardlink-aware safeWriteFile (link survival,
   in-place semantics), OMO home-resolution priority, hijack / stale-config
   warnings, and the link-partner registry (wiki: dev/tools/omo-home-pollution-cadence).
   Run via `npm test` (pretest bundles origins.ts → test/.build/origins.mjs,
   omo.ts → test/.build/omo.mjs, home-reach.ts → test/.build/home-reach.mjs).
   All fixtures live under a temp dir; the real ~/.omo and ~/.config are never
   touched — the one write path that resolves paths itself (writeOmoModels via
   safeWriteFile's default registry) is sandboxed by temporarily pointing
   $HOME/$USERPROFILE at the temp home. */
import assert from "node:assert"
import fs from "node:fs/promises"
import fsSync from "node:fs"
import os from "node:os"
import path from "node:path"

const TMP = await fs.mkdtemp(path.join(os.tmpdir(), "fd-hreach-"))

const origins = await import(new URL("./.build/origins.mjs", import.meta.url))
const omo = await import(new URL("./.build/omo.mjs", import.meta.url))
const hr = await import(new URL("./.build/home-reach.mjs", import.meta.url))
const { safeWriteFile } = origins
const { writeOmoModels } = omo
const {
  resolveOmoHomeDir,
  checkHomeReachability,
  verifyLinkIntegrity,
  upsertLinkPartner,
  loadLinkRegistry,
  scanHomeReach,
  homeReachToastLines,
  renderHomeReachWarnings,
  HOME_REACH_WIKI,
} = hr

let pass = 0
const ok = (name) => {
  pass++
  console.log(`PASS ${name}`)
}

const w = async (p, s) => {
  await fs.mkdir(path.dirname(p), { recursive: true })
  await fs.writeFile(p, s)
}
const kinds = (ws) => ws.map((x) => x.kind)

/* (a) resolveOmoHomeDir — OMO 4.19.4 rule: HOME wins, then USERPROFILE, then cwd */
assert.equal(resolveOmoHomeDir({ HOME: "/h", USERPROFILE: "/u" }, "/c"), "/h")
assert.equal(resolveOmoHomeDir({ USERPROFILE: "/u" }, "/c"), "/u")
assert.equal(resolveOmoHomeDir({}, "/c"), "/c")
assert.equal(resolveOmoHomeDir({ HOME: undefined, USERPROFILE: undefined }, "/w/d"), "/w/d")
ok("resolveOmoHomeDir priority HOME > USERPROFILE > cwd")

/* (b) hardlinked config: safeWriteFile updates BOTH paths, link survives */
{
  const dir = path.join(TMP, "hl")
  const a = path.join(dir, "omo.jsonc")
  const b = path.join(dir, "mirror.jsonc")
  const reg = path.join(dir, "links.json")
  await w(a, '{"v":1}')
  await fs.link(a, b)
  const ino0 = fsSync.statSync(a).ino

  await safeWriteFile(a, '{"v":2}', reg)
  assert.equal(fsSync.readFileSync(b, "utf8"), '{"v":2}')
  let sa = fsSync.statSync(a)
  let sb = fsSync.statSync(b)
  assert.equal(sa.ino, ino0)
  assert.equal(sa.ino, sb.ino)
  assert.ok(sa.nlink >= 2 && sb.nlink >= 2)

  await safeWriteFile(b, '{"v":3}', reg)
  assert.equal(fsSync.readFileSync(a, "utf8"), '{"v":3}')

  const leftovers = (await fs.readdir(dir)).filter((f) => f.startsWith(".fastdraw-tmp"))
  assert.deepEqual(leftovers, [])

  const partners = loadLinkRegistry(fsSync, reg)
  assert.deepEqual(partners.map((p) => p.path).sort(), [a, b].sort())
  assert.ok(partners.every((p) => p.ino === ino0))
  assert.deepEqual(verifyLinkIntegrity({ registryPath: reg }), [])
  ok("hardlinked omo.jsonc: both paths updated, link survived (same ino, nlink>=2), registry recorded")
}

/* (b2) non-linked file keeps atomic temp+rename semantics, no registry entry */
{
  const dir = path.join(TMP, "plain")
  const f = path.join(dir, "cfg.json")
  const reg = path.join(dir, "links.json")
  await safeWriteFile(f, '{"x":1}', reg)
  assert.equal(fsSync.readFileSync(f, "utf8"), '{"x":1}')
  assert.equal(fsSync.statSync(f).nlink, 1)
  assert.deepEqual(loadLinkRegistry(fsSync, reg), [])
  await safeWriteFile(f, '{"x":2}', reg)
  assert.equal(fsSync.readFileSync(f, "utf8"), '{"x":2}')
  ok("non-linked write stays atomic and records no link partner")
}

/* (b3) real writer path: writeOmoModels through a hardlinked omo.jsonc */
{
  const home = path.join(TMP, "wrhome")
  const omoFile = path.join(home, ".omo", "omo.jsonc")
  const mirror = path.join(home, ".omo", "mirror.jsonc")
  const saved = { HOME: process.env.HOME, USERPROFILE: process.env.USERPROFILE }
  try {
    process.env.HOME = home
    process.env.USERPROFILE = home
    await writeOmoModels({ oracle: "prov/first" }, { HOME: home }, home)
    await fs.link(omoFile, mirror)
    const res = await writeOmoModels({ oracle: "prov/second" }, { HOME: home }, home)
    assert.ok(res.written, `writeOmoModels failed: ${res.error ?? ""}`)
    assert.equal(fsSync.readFileSync(omoFile, "utf8"), fsSync.readFileSync(mirror, "utf8"))
    assert.match(fsSync.readFileSync(mirror, "utf8"), /prov\/second/)
    assert.equal(fsSync.statSync(omoFile).ino, fsSync.statSync(mirror).ino)
    assert.ok(fsSync.statSync(omoFile).nlink >= 2)
    const sidecar = path.join(home, ".config", "opencode", "fastdraw-links.json")
    assert.ok(fsSync.existsSync(sidecar), "registry sidecar missing under resolved home")
  } finally {
    if (saved.HOME === undefined) delete process.env.HOME
    else process.env.HOME = saved.HOME
    if (saved.USERPROFILE === undefined) delete process.env.USERPROFILE
    else process.env.USERPROFILE = saved.USERPROFILE
  }
  ok("writeOmoModels on a hardlinked config updates both partners in place + writes sidecar")
}

/* (c) broken link: partner replaced by a separate file (new inode) → flagged;
       partner deleted → flagged as missing */
{
  const dir = path.join(TMP, "broken")
  const a = path.join(dir, "a.json")
  const b = path.join(dir, "b.json")
  const reg = path.join(dir, "links.json")
  await w(a, "same")
  await fs.link(a, b)
  await safeWriteFile(a, "v2", reg)
  await safeWriteFile(b, "v2", reg)
  assert.deepEqual(verifyLinkIntegrity({ registryPath: reg }), [])

  await fs.rm(b)
  await w(b, "diverged-via-editor-atomic-save")
  const ws = verifyLinkIntegrity({ registryPath: reg })
  assert.equal(ws.length, 1)
  assert.equal(ws[0].kind, "link-partner-broken")
  assert.match(ws[0].detail, /lost its link/)
  assert.ok(ws[0].detail.includes(HOME_REACH_WIKI))

  await fs.rm(b)
  const ws2 = verifyLinkIntegrity({ registryPath: reg })
  assert.equal(ws2.length, 1)
  assert.equal(ws2[0].kind, "link-partner-broken")
  assert.match(ws2[0].detail, /no longer exists/)
  ok("verifyLinkIntegrity flags detached (inode change) and missing partners")
}

/* (d) config-outside-resolved-home: managed config under realHome, none under HOME */
{
  const real = path.join(TMP, "real-home")
  const hijacked = path.join(TMP, "cadence-data")
  await w(path.join(real, ".omo", "omo.jsonc"), "{}")
  await fs.mkdir(hijacked, { recursive: true })

  const r = checkHomeReachability({ env: { USERPROFILE: hijacked }, cwd: "/", realHome: real })
  assert.ok(!r.ok)
  assert.deepEqual(kinds(r.warnings), ["config-outside-resolved-home"])
  assert.equal(r.omoHome, hijacked)
  assert.match(r.warnings[0].detail, /omo-home-pollution-cadence/)

  const both = checkHomeReachability({ env: { HOME: hijacked }, cwd: "/", realHome: real })
  assert.deepEqual(kinds(both.warnings), ["home-hijack-suspect", "config-outside-resolved-home"])

  const clean = checkHomeReachability({ env: { HOME: real }, cwd: "/", realHome: real })
  assert.ok(clean.ok)
  assert.equal(clean.omoHome, real)

  ok("config-outside-resolved-home fires only when managed config sits outside the resolved home")
}

/* (e) home-hijack-suspect: HOME set, differing from realHome and USERPROFILE */
{
  const r = checkHomeReachability({ env: { HOME: "/x/y" }, cwd: "/c", realHome: "/real" })
  assert.equal(r.warnings.length, 1)
  assert.equal(r.warnings[0].kind, "home-hijack-suspect")
  assert.ok(r.warnings[0].detail.includes("/x/y"))
  assert.ok(r.warnings[0].detail.includes(HOME_REACH_WIKI))

  const win = checkHomeReachability({ env: { HOME: "/x/y", USERPROFILE: "/x/y" }, cwd: "/c", realHome: "/real" })
  assert.deepEqual(win.warnings, [])

  const none = checkHomeReachability({ env: {}, cwd: "/c", realHome: "/real" })
  assert.ok(none.ok)
  assert.equal(none.omoHome, "/c")
  ok("home-hijack-suspect fires on HOME != realHome/USERPROFILE, suppressed when HOME===USERPROFILE")
}

/* scan + render: every warning kind merges into one additive report */
{
  const real = path.join(TMP, "scan-real")
  const ghost = path.join(TMP, "scan-ghost")
  await w(path.join(real, ".omo", "omo.json"), "{}")
  const reg = path.join(TMP, "scan-links.json")
  upsertLinkPartner({ path: ghost, dev: 7, ino: 8, updatedAt: "2026-09-10T00:00:00.000Z" }, { registryPath: reg })

  const rep = scanHomeReach({ env: { HOME: ghost }, cwd: "/", realHome: real, registryPath: reg })
  assert.ok(!rep.ok)
  assert.deepEqual(
    kinds(rep.warnings).sort(),
    ["config-outside-resolved-home", "home-hijack-suspect", "link-partner-broken"],
  )
  const lines = homeReachToastLines(rep.warnings)
  assert.equal(lines.length, 3)
  assert.ok(lines.every((l) => l.startsWith("⚠ home-reach: ")))
  assert.equal(renderHomeReachWarnings(rep.warnings).split("\n").length, 3)
  assert.deepEqual(renderHomeReachWarnings([]), "")
  ok("scanHomeReach merges hijack + stale-config + broken-link; render prefix per line")
}

await fs.rm(TMP, { recursive: true, force: true })
console.log(`\n${pass} tests passed`)
