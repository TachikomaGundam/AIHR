/* V2 tool core paths — the live engine E0 defect of 2026-11-19: execute that
   resolved to a bare STRING crashed inside the engine (JSC: `a is not an
   Object. (evaluating '"output" in a')`) because the V2 Result contract is an
   OBJECT ({content} or {output}). These tests pin the Result shape plus the
   success / engine-missing / launcher-missing classification through the V2
   execute wrapper, using an HR_BIN stub (resolveHrLaunch operator seam). */
import assert from "node:assert"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"

const tmp = await fs.mkdtemp(path.join(os.tmpdir(), "hr-v2-core-"))
const okStub = path.join(tmp, "hr-ok.sh")
await fs.writeFile(
  okStub,
  '#!/bin/sh\necho "stub-ok $*"\n',
)
await fs.chmod(okStub, 0o755)
const moduleStub = path.join(tmp, "hr-nomodule.sh")
await fs.writeFile(
  moduleStub,
  '#!/bin/sh\necho "No module named \'hr\'" >&2\nexit 1\n',
)
await fs.chmod(moduleStub, 0o755)

let pass = 0
const ok = (name) => { pass++; console.log(`PASS ${name}`) }

const mod = await import(new URL("./.build/server.mjs", import.meta.url))
const tools = {}
const ctx = {
  tool: {
    transform: async (cb) => {
      cb({ add: (t) => { tools[t.name] = t } })
      return { dispose() {} }
    },
  },
}
await mod.default.setup(ctx)

/* 0. the six tools register through the V2 transform */
assert.deepEqual(Object.keys(tools).sort(), [
  "hr_apply",
  "hr_apply_backups",
  "hr_apply_preview",
  "hr_apply_rollback",
  "hr_recommend",
  "hr_status",
])
ok("V2 transform registers all six hr tools")

/* 1. success path — Result must be an OBJECT with string content (defect pin) */
{
  process.env.HR_BIN = okStub
  const r = await tools.hr_status.execute({})
  assert.equal(typeof r, "object", "V2 execute must resolve to an object")
  assert.equal(typeof r.content, "string")
  assert.match(r.content, /stub-ok/)
  ok("hr_status resolves to {content: string}")
}

/* 2. required-input schema is attached (recommend takes task) */
{
  const spec = tools.hr_recommend
  assert.equal(spec.input.required[0], "task")
  ok("hr_recommend keeps its required input schema")
}

/* 3. engine-missing classification comes through as guidance text */
{
  process.env.HR_BIN = moduleStub
  const r = await tools.hr_status.execute({})
  assert.equal(typeof r, "object")
  assert.match(r.content, /no HR engine resolved/)
  ok("module-missing stderr classifies to actionable guidance")
}

/* 4. launcher-missing classification */
{
  process.env.HR_BIN = path.join(tmp, "definitely-not-here")
  const r = await tools.hr_status.execute({})
  assert.equal(typeof r, "object")
  assert.match(r.content, /HR command failed/)
  ok("missing launcher classifies to HR command failed guidance")
}

delete process.env.HR_BIN
console.log(`v2-tools-core: ${pass} checks passed`)
