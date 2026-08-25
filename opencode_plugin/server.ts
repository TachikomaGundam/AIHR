import { execFile } from "node:child_process"
import { homedir } from "node:os"
import path from "node:path"
import { promisify } from "node:util"

import { tool } from "@opencode-ai/plugin"

const executeFile = promisify(execFile)
const HR_HOME = process.env.HR_HOME ?? path.join(homedir(), "hr")
const HR_PYTHON = "python3"

async function runHr(args: readonly string[]): Promise<string> {
  try {
    const result = await executeFile(HR_PYTHON, ["-m", "hr", ...args], {
      cwd: HR_HOME,
      env: { ...process.env, PYTHONPATH: HR_HOME },
      maxBuffer: 1024 * 1024,
      timeout: 30_000,
    })
    return result.stdout.trim() || result.stderr.trim() || "HR command completed."
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)
    return `HR command failed: ${detail}`
  }
}

async function hrPlugin() {
  return {
    tool: {
      hr_status: tool({
        description: "HR: show the latest evaluated sweep and verdict status",
        args: {},
        execute: () => runHr(["status"]),
      }),
      hr_recommend: tool({
        description: "HR: recommend evaluated models for a task description (tri-state eligible / excluded / indeterminate with evidence and reasons)",
        args: {
          task: tool.schema.string().describe("Task to match against evaluated capabilities"),
        },
        execute: ({ task }) => runHr(["recommend", "--task", task]),
      }),
      hr_apply: tool({
        description: "HR: safely apply the latest verdict after an automatic FastDraw backup",
        args: {
          preset: tool.schema.string().optional().describe("Optional FastDraw preset name"),
          setState: tool.schema.boolean().optional().describe("Also write .fastdraw.json; requires OpenCode restart"),
        },
        execute: ({ preset, setState }) => {
          const args = ["apply"]
          if (preset) args.push("--preset", preset)
          if (setState) args.push("--set-state")
          return runHr(args)
        },
      }),
      hr_apply_preview: tool({
        description: "HR: preview the FastDraw changes from the latest verdict without writing files",
        args: {
          preset: tool.schema.string().optional().describe("Optional FastDraw preset name"),
          setState: tool.schema.boolean().optional().describe("Include boot-time state changes"),
        },
        execute: ({ preset, setState }) => {
          const args = ["apply-preview"]
          if (preset) args.push("--preset", preset)
          if (setState) args.push("--set-state")
          return runHr(args)
        },
      }),
      hr_apply_rollback: tool({
        description: "HR: restore FastDraw files from a named automatic apply backup",
        args: { backup: tool.schema.string().describe("Backup name from hr_apply_backups") },
        execute: ({ backup }) => runHr(["apply-rollback", backup]),
      }),
      hr_apply_backups: tool({
        description: "HR: list FastDraw backups available for rollback",
        args: {},
        execute: () => runHr(["apply-backups"]),
      }),
    },
  }
}

export default { id: "hr", server: hrPlugin }
