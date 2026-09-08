/**
 * navigator.ts — per-screen ESC semantics for FastDraw dialog flows.
 *
 * The opencode host's plugin dialog stack is collapse-only: replace() keeps
 * exactly one entry and the built-in ESC binding empties it — so ESC always
 * terminates the whole flow instead of going back one level (there is no
 * push API to build on). This module owns the back-chain instead: every
 * screen may register an optional `back` re-render closure, and ONE
 * app-level keymap layer (registered lazily, exactly once) binds ESC at a
 * priority above the host dialog layer. The binding's `when` gate is true
 * only while a back target exists AND the dialog is actually open, so:
 *
 *   nested screen               → ESC re-renders the parent (consumed);
 *   root / terminal screen      → gate closed → the host's own ESC binding
 *                                 runs → the dialog closes once, unchanged;
 *   dialog closed behind our
 *   back (ctrl+c, backdrop)     → isOpen() false → gate closed → no stray
 *                                 swallow, no zombie re-show later.
 *
 * Everything is injected (replace/clear/setSize/registerLayer/isOpen), so
 * node:test drives it headless with fakes — this module imports nothing.
 * Hosts without `registerLayer` degrade to native ESC behavior untouched.
 */

export type Back = (() => void) | undefined

export type ScreenSize = "small" | "medium" | "large" | "xlarge"

export interface NavigatorHost {
  replace: (render: () => unknown, onClose?: () => void) => void
  clear: () => void
  setSize?: (size: ScreenSize) => void
  registerLayer?: (layer: Record<string, unknown>) => unknown
  isOpen?: () => boolean
  /** Keymap priority for the back binding — above every host layer. */
  priority?: number
}

export interface ScreenOptions {
  size?: ScreenSize
  back?: Back
}

export interface Navigator {
  /** Render a screen; `back` = what ESC should do (undefined → native close). */
  screen(render: () => unknown, opts?: ScreenOptions): void
  /** Close the dialog and disarm the gate (every leave-flow path routes here). */
  clear(): void
  /** Whether ESC is currently intercepted (test/debug hook). */
  hasBack(): boolean
}

export function createNavigator(host: NavigatorHost): Navigator {
  let back: Back
  let layerRegistered = false

  // Read `back` at dispatch time only — the binding lives across screens.
  const gateOpen = () => back !== undefined && (host.isOpen === undefined || host.isOpen())

  const ensureLayer = () => {
    if (layerRegistered || host.registerLayer === undefined) return
    layerRegistered = true
    try {
      host.registerLayer({
        name: "fastdraw.back",
        namespace: "fastdraw",
        priority: host.priority ?? 100,
        bindings: [
          {
            key: "escape",
            when: gateOpen,
            cmd: () => {
              const go = back
              if (go !== undefined && gateOpen()) go()
            },
          },
        ],
      })
    } catch {
      // Legacy/broken keymap API: degrade to native ESC (exit-to-close).
      layerRegistered = false
    }
  }

  return {
    screen(render, opts) {
      back = opts?.back
      if (opts?.size !== undefined) host.setSize?.(opts.size)
      host.replace(() => render())
      ensureLayer()
    },
    clear() {
      back = undefined
      host.clear()
    },
    hasBack: gateOpen,
  }
}
