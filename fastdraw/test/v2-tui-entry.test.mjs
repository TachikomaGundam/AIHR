/* V2 entry-shape tests — TUI half. Exercises the Plugin.define default export
   plus the V1 `tui` bridge with fake contexts (no real engine). pretest
   bundles tui.ts with @opencode/plugin/tui + solid-js kept external. */
import assert from "node:assert"
import fs from "node:fs/promises"

process.env.HOME = "/tmp/fastdraw-v2-tui"
await fs.rm("/tmp/fastdraw-v2-tui", { recursive: true, force: true })
await fs.mkdir("/tmp/fastdraw-v2-tui", { recursive: true })
process.chdir("/tmp/fastdraw-v2-tui")

const mod = await import(new URL("./.build/tui.mjs", import.meta.url))

let pass = 0
const ok = (name) => { pass++; console.log(`PASS ${name}`) }

/* 1. default export = V2 definition + V1 tui bridge (dual exit) */
assert.equal(mod.default.id, "fastdraw")
assert.equal(typeof mod.default.setup, "function")
assert.equal(typeof mod.default.tui, "function")
ok("default export has stable id + setup + tui() bridge")

/* 2. import-picker named exports survive the rewrite */
for (const fn of ["findImportCandidates", "describeImport", "applyImportFile"])
  assert.equal(typeof mod[fn], "function", fn)
ok("named exports findImportCandidates/describeImport/applyImportFile intact")

/* 3. V2 setup registers the keymap layer with slash command + leader bind */
let layerFactory
const toasts = []
const ctx = {
  location: undefined,
  keymap: { layer: (f) => { layerFactory = f } },
  ui: {
    toast: { show: (o) => toasts.push(o) },
    dialog: {
      set() {},
      clear() {},
      select: async () => undefined, // every dialog ESC-cancels
      prompt: async () => undefined,
      confirm: async () => false,
      alert: async () => {},
    },
  },
  data: {
    location: {
      default: () => undefined,
      model: { list: () => [], sync: async () => {} },
      provider: { list: () => [], sync: async () => {} },
    },
  },
}
await mod.default.setup(ctx)
assert.equal(typeof layerFactory, "function")
const layer = layerFactory()
assert.equal(layer.mode, "global")
assert.deepEqual(layer.bindings, ["fastdraw.open"])
const cmd = layer.commands.find((c) => c.id === "fastdraw.open")
assert.ok(cmd, "fastdraw.open command")
assert.equal(cmd.bind, "<leader>f")
assert.equal(cmd.slash.name, "fastdraw")
assert.equal(cmd.palette, true)
assert.equal(typeof cmd.run, "function")
ok("keymap.layer: fastdraw.open — <leader>f (2.x engine owns <leader>m), /fastdraw slash, palette entry")

/* 4. run() with fake promise dialogs: menu opens, ESC cancels cleanly */
await cmd.run()
ok("menu run() completes cleanly when dialogs cancel (fake V2 dialogs)")

/* 5. V1 bridge path: legacy keymap.registerLayer still receives the command */
const layers = []
const legacy = {
  ui: { toast: () => {}, dialog: { setSize() {}, replace() {}, clear() {} } },
  keymap: { registerLayer: (l) => layers.push(l) },
}
await mod.default.tui(legacy)
assert.equal(layers.length, 1)
assert.equal(layers[0].commands[0].name, "fastdraw.open")
assert.equal(layers[0].commands[0].slashName, "fastdraw")
assert.deepEqual(layers[0].bindings, [{ key: "<leader>m", cmd: "fastdraw.open" }])
ok("V1 tui bridge registers keymap layer via legacy registerLayer")

console.log(`v2-tui-entry: ${pass} checks passed`)
