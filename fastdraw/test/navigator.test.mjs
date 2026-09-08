/* FastDraw navigator tests — run via `npm test` (pretest bundles navigator.ts
   with esbuild into test/.build/). Fully headless: every host capability is
   a fake, nothing touches opentui, the keymap, or the real dialog. */
import assert from "node:assert/strict"
import { test } from "node:test"

const { createNavigator } = await import(new URL("./.build/navigator.mjs", import.meta.url))

/** Recording fake host. `open` models the dialog's live state. */
function fakeHost({ withLayer = true, open = () => true } = {}) {
  const h = {
    screens: [],
    cleared: 0,
    sizes: [],
    layers: [],
    isOpen: open,
    replace: (render, onClose) => h.screens.push({ render, onClose }),
    clear: () => {
      h.cleared += 1
    },
    setSize: (s) => h.sizes.push(s),
  }
  if (withLayer) {
    h.registerLayer = (layer) => {
      h.layers.push(layer)
      return () => {}
    }
  }
  return h
}

const binding = (h) => h.layers[0].bindings[0]

test("root screen: layer registered once, gate closed, replace forwarded", () => {
  const h = fakeHost()
  const nav = createNavigator(h)
  const el = () => "A"
  nav.screen(el, { size: "large" })
  nav.screen(() => "B")
  assert.equal(h.layers.length, 1, "exactly one layer across screens")
  assert.equal(binding(h).key, "escape")
  assert.equal(h.layers[0].priority, 100)
  assert.equal(nav.hasBack(), false, "root must NOT intercept ESC")
  assert.equal(binding(h).when(), false)
  assert.deepEqual(h.sizes, ["large"], "setSize only when asked")
  assert.equal(h.screens.length, 2)
  assert.equal(h.screens[0].render(), el())
})

test("nested screen: gate open, cmd runs back exactly once", () => {
  const h = fakeHost()
  const nav = createNavigator(h)
  let backs = 0
  nav.screen(() => "parent")
  nav.screen(() => "child", { back: () => { backs += 1 } })
  assert.equal(binding(h).when(), true)
  binding(h).cmd()
  assert.equal(backs, 1)
})

test("reentrant transition: back swaps screens and the gate tracks the parent", () => {
  const h = fakeHost()
  const nav = createNavigator(h)
  const menuBacks = []
  const showChild = () => nav.screen(() => "child", { back: showParent })
  const showParent = () => nav.screen(() => "parent", { back: () => menuBacks.push("menu") })
  showParent()
  showChild()
  // ESC on child -> parent re-shows; gate now points at the parent's back.
  binding(h).cmd()
  assert.deepEqual(menuBacks, [])
  assert.equal(nav.hasBack(), true)
  binding(h).cmd() // ESC on parent -> menu back fires
  assert.deepEqual(menuBacks, ["menu"])
})

test("terminal after nested (confirm): gate closes again", () => {
  const h = fakeHost()
  const nav = createNavigator(h)
  nav.screen(() => "root")
  nav.screen(() => "nested", { back: () => assert.fail("must not fire") })
  nav.screen(() => "confirm") // terminal: no back
  assert.equal(binding(h).when(), false)
  binding(h).cmd() // defensive: cmd must no-op while gated closed
})

test("clear() disarms the gate even with a back pending", () => {
  const h = fakeHost()
  const nav = createNavigator(h)
  nav.screen(() => "x", { back: () => assert.fail("must not fire") })
  nav.clear()
  assert.equal(nav.hasBack(), false)
  assert.equal(h.cleared, 1)
})

test("dialog closed behind our back (ctrl+c): isOpen=false gates everything", () => {
  let open = true
  const h = fakeHost({ open: () => open })
  const nav = createNavigator(h)
  nav.screen(() => "nested", { back: () => assert.fail("zombie re-show!") })
  open = false // host closed the dialog without going through nav.clear()
  assert.equal(binding(h).when(), false)
  binding(h).cmd() // no-op, never resurrects the parent
  open = true
  assert.equal(nav.hasBack(), true) // stale back armed again only if reopened
})

test("legacy host without registerLayer: pure passthrough, no crash", () => {
  const h = fakeHost({ withLayer: false })
  const nav = createNavigator(h)
  nav.screen(() => "a", { back: () => {} })
  nav.clear()
  assert.equal(h.layers.length, 0)
  assert.equal(h.screens.length, 1)
})

test("registerLayer throwing: degrade to native ESC, screens keep working", () => {
  const h = fakeHost()
  h.registerLayer = () => {
    throw new Error("ancient keymap")
  }
  const nav = createNavigator(h)
  nav.screen(() => "a", { back: () => {} })
  assert.equal(h.screens.length, 1)
  assert.equal(h.layers.length, 0, "degraded: no layer ever registered")
})

test("custom priority honored", () => {
  const h = fakeHost()
  const nav = createNavigator({ ...h, priority: 42 })
  nav.screen(() => "a", { back: () => {} })
  assert.equal(h.layers[0].priority, 42)
})

test("root screen: gate stays closed so the host ESC binding runs (native close preserved)", () => {
  const h = fakeHost()
  const nav = createNavigator(h)
  nav.screen(() => "menu") // no back — main menu
  assert.equal(binding(h).when(), false, "ESC must NOT be intercepted at root")
  const before = h.screens.length
  binding(h).cmd() // simulate dispatch even though when() is false
  assert.equal(h.screens.length, before, "cmd at closed gate must not re-render anything")
})

test("no zombie re-show: cmd after the dialog closed behind our back is inert", () => {
  let open = true
  const h = fakeHost({ open: () => open })
  const nav = createNavigator(h)
  nav.screen(() => "menu")
  nav.screen(() => "child", { back: () => nav.screen(() => "menu") })
  open = false // ctrl+c / backdrop closed the dialog
  const before = h.screens.length
  binding(h).cmd()
  assert.equal(h.screens.length, before, "closed dialog: back must not fire")
  assert.equal(nav.hasBack(), false)
})

test("deep chain: 30 transitions share ONE layer and unwind one level per ESC", () => {
  const h = fakeHost()
  const nav = createNavigator(h)
  const depth = 30
  const renderAt = (d) => () => `screen-${d}`
  // opts[d] = the options used when showing screen d; back points at d-1.
  const opts = (d) => (d === 0 ? undefined : { back: () => nav.screen(renderAt(d - 1), opts(d - 1)) })
  nav.screen(renderAt(depth), opts(depth))
  assert.equal(h.layers.length, 1, "one layer for the whole lifecycle")
  assert.equal(h.screens.length, 1)
  for (let d = depth; d >= 1; d--) {
    assert.equal(binding(h).when(), true, `gate open at depth ${d}`)
    binding(h).cmd()
  }
  assert.equal(binding(h).when(), false, "unwound to root: gate closed again")
  assert.equal(h.screens.length, depth + 1, "each ESC re-rendered exactly one parent")
  assert.equal(h.layers.length, 1, "still one layer after the whole walk")
})
