/* FastDraw navigator tests — run via `npm test` (pretest bundles navigator.ts
   with esbuild into test/.build/). Fully headless: every host capability is
   a fake, nothing touches opentui, the keymap, or the real dialog.
   The fake dispatcher mirrors @opentui/keymap's REAL binding contract
   (RESERVED_BINDING_FIELDS; only a literal `false` cmd return rejects a
   dispatch so lower layers see the key) — deliberately, because an earlier
   suite faked a `when()` predicate the real host never had and so proved
   nothing about gate-closed ESC. */
import assert from "node:assert/strict"
import { test } from "node:test"

const { createNavigator } = await import(new URL("./.build/navigator.mjs", import.meta.url))

/* Verbatim from @opentui/keymap@0.5.11 src/index.js:889-890. */
const RESERVED_BINDING_FIELDS = new Set(["key", "cmd", "event", "preventDefault", "fallthrough"])
const RESERVED_LAYER_FIELDS = new Set(["target", "targetMode", "priority", "bindings", "commands"])

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

/** Simulate one app-level ESC dispatch against the highest-priority layer:
 *  cmd returning literal false -> {handled:false} (lower layers still see
 *  the key); anything else -> {handled:true} (consumed, dialog-level ESC
 *  handlers no longer run). Mirrors executeResolvedCommand. */
function dispatchEscape(h) {
  const b = binding(h)
  for (const field of Object.keys(b)) {
    assert.ok(RESERVED_BINDING_FIELDS.has(field), `binding field "${field}" is not in the real keymap contract`)
  }
  const result = b.cmd()
  return { handled: result !== false }
}

test("root screen: layer registered once, ESC falls through to native close", () => {
  const h = fakeHost()
  const nav = createNavigator(h)
  const el = () => "A"
  nav.screen(el, { size: "large" })
  nav.screen(() => "B")
  assert.equal(h.layers.length, 1, "exactly one layer across screens")
  assert.equal(binding(h).key, "escape")
  assert.equal(h.layers[0].priority, 100)
  assert.equal(nav.hasBack(), false, "root must NOT intercept ESC")
  assert.equal(dispatchEscape(h).handled, false, "gate closed: dispatcher rejects, host layer runs")
  assert.deepEqual(h.sizes, ["large"], "setSize only when asked")
  assert.equal(h.screens.length, 2)
  assert.equal(h.screens[0].render(), el())
})

test("nested screen: gate open, cmd runs back exactly once and consumes ESC", () => {
  const h = fakeHost()
  const nav = createNavigator(h)
  let backs = 0
  nav.screen(() => "parent")
  nav.screen(() => "child", { back: () => { backs += 1 } })
  assert.equal(dispatchEscape(h).handled, true)
  assert.equal(backs, 1)
  assert.equal(dispatchEscape(h).handled, true, "second press still consumed (gate open)")
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
  dispatchEscape(h)
  assert.deepEqual(menuBacks, [])
  assert.equal(nav.hasBack(), true)
  dispatchEscape(h) // ESC on parent -> menu back fires
  assert.deepEqual(menuBacks, ["menu"])
})

test("terminal after nested (confirm): rejected dispatch, no side effects", () => {
  const h = fakeHost()
  const nav = createNavigator(h)
  nav.screen(() => "root")
  nav.screen(() => "nested", { back: () => assert.fail("must not fire") })
  nav.screen(() => "confirm") // terminal: no back
  assert.equal(dispatchEscape(h).handled, false)
})

test("clear() disarms the gate even with a back pending", () => {
  const h = fakeHost()
  const nav = createNavigator(h)
  nav.screen(() => "x", { back: () => assert.fail("must not fire") })
  nav.clear()
  assert.equal(nav.hasBack(), false)
  assert.equal(h.cleared, 1)
  assert.equal(dispatchEscape(h).handled, false, "disarmed gate rejects")
})

test("dialog closed behind our back (ctrl+c): isOpen=false rejects every ESC", () => {
  let open = true
  const h = fakeHost({ open: () => open })
  const nav = createNavigator(h)
  nav.screen(() => "nested", { back: () => assert.fail("zombie re-show!") })
  open = false // host closed the dialog without going through nav.clear()
  assert.equal(dispatchEscape(h).handled, false)
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

test("layer fields stay inside the real keymap contract (no fictional name/namespace)", () => {
  const h = fakeHost()
  const nav = createNavigator(h)
  nav.screen(() => "a", { back: () => {} })
  for (const field of Object.keys(h.layers[0])) {
    assert.ok(RESERVED_LAYER_FIELDS.has(field), `layer field "${field}" is not in the real keymap contract`)
  }
})

test("no zombie re-show: ESC after the dialog closed behind our back is inert", () => {
  let open = true
  const h = fakeHost({ open: () => open })
  const nav = createNavigator(h)
  nav.screen(() => "menu")
  nav.screen(() => "child", { back: () => nav.screen(() => "menu") })
  open = false // ctrl+c / backdrop closed the dialog
  const before = h.screens.length
  assert.equal(dispatchEscape(h).handled, false, "closed dialog: rejected, nothing re-rendered")
  assert.equal(h.screens.length, before)
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
    assert.equal(dispatchEscape(h).handled, true, `gate open at depth ${d}: consumed`)
  }
  assert.equal(dispatchEscape(h).handled, false, "unwound to root: rejected (native close)")
  assert.equal(h.screens.length, depth + 1, "each ESC re-rendered exactly one parent")
  assert.equal(h.layers.length, 1, "still one layer after the whole walk")
})
