# opencode-fastdraw

Quick-draw model switching for [opencode](https://opencode.ai) agents. Bind any configured model to any agent — OMO roles (`sisyphus`, `oracle`, `explore`, …), built-ins (`build`, `plan`, `general`), or your own custom agents — from a TUI dialog or agent tool calls. Snapshot bindings into named **presets**, preview them before applying, export/import them as portable JSON files, and hot-swap the whole setup instantly.

Built for heavy [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent) (OMO) setups, works with any opencode config.

## Features

- **Assign models from the TUI** — `/fastdraw` or `<leader>m` → pick agent → pick model. Agents grouped by OMO Roles / Overrideable / Custom; models grouped by provider.
- **Presets** — save the current assignment set as a named preset; loading a preset shows a **full preview of every role's binding** before you confirm.
- **Export / Import** — share presets as portable JSON files (`fastdraw-preset-<name>.json`), or import a whole preset store at once.
- **Hot-apply** — the `fastdraw_*` agent tools mutate the live config immediately (agents not in a freshly loaded preset revert to their original models). TUI changes persist and apply on restart.
- **Config-respecting** — assignments live in `~/.config/opencode/.fastdraw.json`; presets in `fastdraw-presets.json`. Your `opencode.jsonc` is never modified by assignment — the only write path is *load preset* in a restore mode (`global` / `original` / `path`), and even then every file is backed up as `<file>.bak-<timestamp>` before being touched.

## Install

### Dual-file registration (read this first)

opencode loads plugins from **two separate config files**. FastDraw has a server part (config hook + `fastdraw_*` tools) and a TUI part (the `/fastdraw` command + `<leader>m` keybind). **Both files must list the plugin** — if only one does, the other half loads silently absent (commonly the TUI half: tools keep working but `/fastdraw` and the keybind vanish).

| Config file | Loads | What you lose if missing |
|---|---|---|
| `~/.config/opencode/opencode.jsonc` (or `.json`) | server plugins | `fastdraw_*` agent tools |
| `~/.config/opencode/tui.json` | TUI plugins | `/fastdraw` command + `<leader>m` keybind |

Every install method below writes to BOTH. When registering manually, add the same entry to both files' `"plugin"` arrays.

### 1. npm (recommended)

```bash
# once, from the opencode config dir (or let opencode auto-install on first start)
cd ~/.config/opencode && bun add opencode-fastdraw
```

Then add to `plugin` in **both** files:

```jsonc
// ~/.config/opencode/opencode.jsonc
{ "plugin": ["opencode-fastdraw", /* …your other plugins… */] }
```

```json
// ~/.config/opencode/tui.json
{ "plugin": ["opencode-fastdraw"] }
```

**Restart opencode after installing.** The `/fastdraw` command and `<leader>m` binding appear in the TUI; the `fastdraw_*` tools become available to agents.

## Usage

### TUI

Press `<leader>m` or type `/fastdraw`:

```
FastDraw — Model Assignments & Presets
  Assign Model              Bind a model to an agent
  Save Current as Preset    Snapshot all current assignments
  Load Preset               Preview bindings, then apply
  Import Preset from File   Load preset(s) from a JSON file
  Export Preset to File     Share a preset as JSON
  Delete Preset             Remove a saved preset
```

### Agent tools

| Tool | What it does |
|---|---|
| `fastdraw_assign` | `agent`, `model` → bind (effective immediately) |
| `fastdraw_remove` | remove an agent's override, revert to default |
| `fastdraw_list` | show all agents and their current models |
| `fastdraw_save_preset` | `name`, `description?` → snapshot current assignments |
| `fastdraw_load_preset` | `name` → replace all assignments (hot-swap, with revert) |
| `fastdraw_list_presets` | list presets with full binding previews |
| `fastdraw_delete_preset` | `name` → delete a preset |
| `fastdraw_export_preset` | `name`, `path?` → write portable JSON |
| `fastdraw_import_preset` | `path`, `name?` → import from JSON (single or bulk) |

Ask any agent: *"use fastdraw to put oracle on provider/model and save it as preset 'reasoning'"*.

## Preset file formats

Bindings are stored in two sections: **`omo`** (standard OMO roles — `sisyphus`, `oracle`, `explore`, `prometheus`, …) and **`custom`** (your own agents plus opencode built-ins like `build`/`plan`/`general`). A preset saved on a machine with custom agents loads fine on a machine without them — missing custom roles are skipped with a warning listing exactly which roles were skipped.

**Store** (`~/.config/opencode/fastdraw-presets.json`):

```json
{
  "presets": {
    "reasoning": {
      "schemaVersion": 2,
      "description": "Heavy reasoning setup",
      "createdAt": "2026-08-18T10:00:00.000Z",
      "omo": {
        "oracle": {
          "model": "provider/model",
          "origin": { "layer": "state", "file": "${CONFIG_DIR}/.fastdraw.json" }
        }
      },
      "custom": {
        "my-custom-agent": { "model": "provider/model" }
      }
    }
  }
}
```

**Portable export** (`fastdraw-preset-reasoning.json`):

```json
{
  "fastdraw": 1,
  "schemaVersion": 2,
  "name": "reasoning",
  "description": "Heavy reasoning setup",
  "exportedAt": "2026-08-18T10:05:00.000Z",
  "omo": { "oracle": { "model": "provider/model" } },
  "custom": { "my-custom-agent": { "model": "provider/model" } }
}
```

Each binding is `{ "model": "provider/model", "origin": … }`; `origin` records where the binding came from as a portable placeholder path (`${CONFIG_DIR}`, `${PROJECT}`, `${HOME}`) so presets stay machine-portable. Legacy v1 presets — flat `"agents": { "oracle": "provider/model" }` or `{ "model": … }` values — load fine and are normalized to v2 on the next save. Import accepts either format (single export or whole store); loading a preset reports origin resolution conflicts and shows exactly where each role would be written in restore modes.

## How it works

opencode resolves plugins through the package's `exports` map: `"./server"` for the server runtime, `"./tui"` for the TUI process. The server plugin's `config()` hook runs after other plugins (e.g. OMO) have populated `cfg.agent`, then overlays your saved bindings on top — so FastDraw wins by load order. Tool-triggered changes mutate the same live config object, which is why `fastdraw_assign` / `fastdraw_load_preset` take effect immediately; the first-seen model of every overridden agent is snapshotted so un-override can restore it exactly. Config files on disk are touched only by *load preset* in a restore mode, which first backs each file up as `<file>.bak-<timestamp>`.

## Development

```bash
npm install
npm test        # esbuild-transpiles server.ts, runs preset round-trip tests
npm publish     # after updating the version
```

## License

MIT
