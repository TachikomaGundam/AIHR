/**
 * FastDraw role registry — single source of truth for the preset omo/custom
 * split. Both server.ts and tui.ts import from here so the standard-role list
 * (and the classification rule) can never drift between the two plugin halves.
 *
 * Standard roles = OMO built-in agents + opencode's overridable built-ins
 * (build/plan/general). Everything else (e.g. agents discovered from
 * ~/.config/opencode/agents/*.md) is CUSTOM.
 */

export const OMO_BUILTINS = [
  "sisyphus", "sisyphus-junior", "prometheus", "metis", "momus",
  "hephaestus", "oracle", "explore", "librarian", "atlas",
  "multimodal-looker", "OpenCode-Builder",
] as const

export const OVERRIDABLE_LIST = ["build", "plan", "general"] as const

export const OMO_ROLES: ReadonlySet<string> = new Set(OMO_BUILTINS)
export const OVERRIDABLE: ReadonlySet<string> = new Set(OVERRIDABLE_LIST)

const STANDARD_ROLES: ReadonlySet<string> = new Set([
  ...OMO_BUILTINS,
  ...OVERRIDABLE_LIST,
])

/** Is this a standard (OMO / opencode built-in) role rather than a custom one? */
export function isStandardRole(name: string): boolean {
  return STANDARD_ROLES.has(name)
}

export interface PresetSections {
  omo: Record<string, string>
  custom: Record<string, string>
}

/** Split a flat role→model map into standard ("omo") and custom sections. */
export function splitRoles(agents: Record<string, string>): PresetSections {
  const omo: Record<string, string> = {}
  const custom: Record<string, string> = {}
  for (const [name, model] of Object.entries(agents)) {
    if (isStandardRole(name)) omo[name] = model
    else custom[name] = model
  }
  return { omo, custom }
}

/** Merge sections back into a single flat map (previews, hot-swap, state). */
export function mergeSections(sections: PresetSections | null | undefined): Record<string, string> {
  const out: Record<string, string> = {}
  if (sections) {
    if (sections.omo) Object.assign(out, sections.omo)
    if (sections.custom) Object.assign(out, sections.custom)
  }
  return out
}