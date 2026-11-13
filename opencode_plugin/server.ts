import { execFile } from "node:child_process"
import { existsSync } from "node:fs"
import { homedir } from "node:os"
import { promisify } from "node:util"

import { Plugin } from "@opencode/plugin"
import { tool } from "@opencode-ai/plugin"

import { ensureHrHome, buildHrArgs, classifyHrError, resolveHrLaunch } from "./hr-invocation.js"

const executeFile = promisify(execFile)

async function runHr(args: readonly string[]): Promise<string> {
  const launch = resolveHrLaunch(process.env, homedir(), existsSync)
  if (launch.ensureHome) {
    try {
      ensureHrHome(launch.cwd)
    } catch (error) {
      return classifyHrError(error, launch.cwd)
    }
  }
  try {
    const result = await executeFile(launch.command, [...launch.prefixArgs, ...args], {
      cwd: launch.cwd,
      env: launch.pythonpath === null ? { ...process.env } : { ...process.env, PYTHONPATH: launch.pythonpath },
      maxBuffer: 1024 * 1024,
      timeout: 30_000,
    })
    return result.stdout.trim() || result.stderr.trim() || "HR command completed."
  } catch (error) {
    return classifyHrError(error, launch.cwd)
  }
}

// Single source of truth for the HR tool surface. The V1 plugin API and the
// V2 plugin API render the same registry through different shapes.
type HrToolSpec = {
  readonly name: string
  readonly description: string
  readonly v1Args: Record<string, unknown>
  readonly v2Input: Record<string, unknown>
  readonly command: string
  readonly passthrough: readonly string[]
}

const objectSchema = (properties: Record<string, unknown>, required: readonly string[] = []) => ({
  type: "object",
  properties,
  required: [...required],
  additionalProperties: false,
})

const HR_TOOL_SPECS: readonly HrToolSpec[] = [
  {
    name: "hr_status",
    description: "HR: show the latest evaluated sweep and verdict status",
    v1Args: {},
    v2Input: objectSchema({}),
    command: "status",
    passthrough: [],
  },
  {
    name: "hr_recommend",
    description:
      "HR: recommend evaluated models for a task description (tri-state eligible / excluded / indeterminate with evidence and reasons)",
    v1Args: { task: tool.schema.string().describe("Task to match against evaluated capabilities") },
    v2Input: objectSchema({ task: { type: "string", description: "Task to match against evaluated capabilities" } }, ["task"]),
    command: "recommend",
    passthrough: ["task"],
  },
  {
    name: "hr_apply",
    description: "HR: safely apply the latest verdict after an automatic FastDraw backup",
    v1Args: {
      preset: tool.schema.string().optional().describe("Optional FastDraw preset name"),
      setState: tool.schema.boolean().optional().describe("Also write .fastdraw.json; requires OpenCode restart"),
    },
    v2Input: objectSchema({
      preset: { type: "string", description: "Optional FastDraw preset name" },
      setState: { type: "boolean", description: "Also write .fastdraw.json; requires OpenCode restart" },
    }),
    command: "apply",
    passthrough: ["preset", "setState"],
  },
  {
    name: "hr_apply_preview",
    description: "HR: preview the FastDraw changes from the latest verdict without writing files",
    v1Args: {
      preset: tool.schema.string().optional().describe("Optional FastDraw preset name"),
      setState: tool.schema.boolean().optional().describe("Include boot-time state changes"),
    },
    v2Input: objectSchema({
      preset: { type: "string", description: "Optional FastDraw preset name" },
      setState: { type: "boolean", description: "Include boot-time state changes" },
    }),
    command: "apply-preview",
    passthrough: ["preset", "setState"],
  },
  {
    name: "hr_apply_rollback",
    description: "HR: restore FastDraw files from a named automatic apply backup",
    v1Args: { backup: tool.schema.string().describe("Backup name from hr_apply_backups") },
    v2Input: objectSchema({ backup: { type: "string", description: "Backup name from hr_apply_backups" } }, ["backup"]),
    command: "apply-rollback",
    passthrough: ["backup"],
  },
  {
    name: "hr_apply_backups",
    description: "HR: list FastDraw backups available for rollback",
    v1Args: {},
    v2Input: objectSchema({}),
    command: "apply-backups",
    passthrough: [],
  },
]

function selectArgs(spec: HrToolSpec, input: Record<string, unknown>): Record<string, unknown> {
  const args: Record<string, unknown> = {}
  for (const key of spec.passthrough) {
    if (input[key] !== undefined) args[key] = input[key]
  }
  return args
}

// V2 path: Promise-context plugin registered through the tool transform.
async function setupV2(context: { tool: { transform(fn: (editor: { add(tool: unknown): void }) => void): Promise<unknown> } }) {
  await context.tool.transform((editor) => {
    for (const spec of HR_TOOL_SPECS) {
      editor.add({
        name: spec.name,
        description: spec.description,
        input: spec.v2Input,
        execute: async (input: Record<string, unknown>) => runHr(buildHrArgs(spec.command, selectArgs(spec, input))),
      })
    }
  })
}

// V1 bridge path: kept for OpenCode >=1.18.29,<2 compatibility per the
// official dual-entrypoint migration pattern.
async function hrPluginV1() {
  const registry: Record<string, unknown> = {}
  for (const spec of HR_TOOL_SPECS) {
    registry[spec.name] = tool({
      description: spec.description,
      args: spec.v1Args,
      execute: (input: Record<string, unknown>) => runHr(buildHrArgs(spec.command, selectArgs(spec, input))),
    } as never)
  }
  return { tool: registry }
}

export default { ...Plugin.define({ id: "hr", setup: setupV2 }), server: hrPluginV1 }
