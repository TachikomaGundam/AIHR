# Plugin Security Model — `opencode-hr-agent`

Trust boundary and shipped-surface guarantees for the OpenCode HR plugin
(`opencode_plugin/`). Release-side procedures: `docs/PUSH.md` §5.

## 1. Trust boundary

The package exposes two privileged actions, each contained:

1. **At runtime (the plugin):** launching `python3 -m hr ...` through
   `execFile` — never a shell. Trust assumptions, plainly: `HR_HOME`
   (default `~/hr`) and the `PYTHONPATH` injected into the child, the resolved
   `python3` binary, and everything under `HR_HOME` are trusted same-user surface —
   an attacker who can write those can already run code as you.
2. **At operator invocation (the `opencode-hr` bin):** writing the two
   user-owned opencode config files (`opencode.json`, `tui.json`) to register
   plugin entries. It runs ONLY when a human executes `opencode-hr …` (or `hr
   setup` chains it); nothing in the package lifecycle triggers it
   automatically (§4). Containment details: §7.

Hardening target: **no attack surface beyond that baseline** — no shell, no
remote fetching, no `eval`, no dynamic module loading, no credential handling
of the plugin's own.
The Python engine is a separate trust domain with its own containment
(fail-closed `bwrap` sandbox, path confinement, transactional backup/rollback —
see `hr/plugin_safety.py`), out of scope here.

## 2. Execution surface facts

- Only `opencode_plugin/server.ts` imports `child_process`; one `execFile`
  alias call-site, inside `runHr`.
- Argv is built by the pure `buildHrArgs` from a fixed constant vocabulary of
  six tool names plus schema-validated strings (zod at each `tool()`); an
  unknown tool name is a compile error, never a silent default arm.
- Bounds: timeout 30_000 ms, maxBuffer 1 MiB; output returned as text only.

## 3. Supply-chain posture

Zero runtime npm dependencies — the peer `@opencode-ai/plugin` is a
types/tool-surface module provided by OpenCode at runtime; no bundled code
ships. One-click installs (`hr setup`) resolve `opencode-hr-agent@latest` and
the registrar writes `@latest` entries into the two config files: floating
specs trade startup-time reproducibility for never running a stale pin, and
`hr setup` prints the concrete resolved versions right after install so every
run is auditable after the fact. If you prefer a frozen running surface,
install and declare an exact version instead: `npm install -g
"opencode-hr-agent@<version>"` (quote it for zsh). The `-g` install exists to place the
`opencode-hr` registrar CLI on PATH — opencode itself loads the plugin only
via its config `"plugin"` arrays (which `opencode-hr install` / `hr setup`
write; see README Install), never by scanning the global prefix.

PATH persistence: when pip's scripts directory (the `hr` entry point) is
missing from PATH — the Windows/macOS user-site default — `hr setup` appends
exactly one user-scope entry (`HKCU\Environment` on Windows, a marked block in
existing rc files elsewhere), expanding `%VAR%` at read time so other tools'
entries are preserved verbatim. `hr setup --uninstall` removes precisely that
entry and every other artifact setup created (configs, globals, cache copies,
config copies); nothing elevated or system-wide is ever touched.

## 4. No lifecycle scripts

`npm install` never runs project scripts here: the manifest has no
pretest/posttest/prepare/install hooks — only `test` (asserted at CI and every
release, `docs/PUSH.md` §5 item 3). Caveat: npm's `allow-same-user` default
(`true`) means same-user tarball takeover of an already-published version
remains possible; a scoped org with per-package access grants is stronger
isolation. Documented posture — no config change implied or made here.

## 5. Pack invariant

The published tarball MUST equal exactly {`package.json`, `server.ts`,
`hr-invocation.ts`, `install-cli.js`} — verified via `npm pack --dry-run` at
every release, run
**outside** `opencode_plugin/` so its gitignored package-local `.npmrc` never
loads into an agent's npm environment view. General rule: registry publish
tokens belong in your user-level `~/.npmrc` (via `npm config set
//registry.npmjs.org/:_authToken <token>`) or `NPM_TOKEN` — never in a tracked
or package-local file.

## 6. Fail-soft error surface

`classifyHrError` maps missing engine, `ENOENT`, and `EACCES`/`EPERM` to fixed,
actionable install/permission guidance strings; it never echoes argv or env
beyond that guidance and never throws into plugin load — every failure path
returns text.

## 7. The `opencode-hr` registrar surface

`install-cli.js` (bin `opencode-hr`, node builtins only — no network, no
`child_process`, no dynamic import):

- Writes exactly the `"plugin"` key of `opencode.json`/`tui.json` under
  `OPENCODE_CONFIG_DIR` (default `~/.config/opencode`); every other config key
  round-trips untouched.
- Refuses JSONC and malformed JSON instead of rewriting them (prints the exact
  lines to paste); a bad second file cannot corrupt the already-written first
  file beyond an idempotent, re-runnable state.
- Atomic write = unique temp name (`pid`+random) created with `O_EXCL` — a
  pre-planted symlink or file at the temp path fails closed instead of being
  followed — then `rename`. Existing file mode is preserved; newly created
  configs default to `0600` (opencode configs may carry provider keys).
- Never logs or reads credentials; `install`/`status`/`uninstall` only touch
  the plugin entries it owns (foreign `"plugin"` specs keep position and form;
  array-form entries keep their options object, version upgraded in place).
- The Python `hr setup` wrapper spawns `npm` (argv lists, no shell, capped
  timeouts) and the registrar resolved through the npm-global bin — npm being
  already in the user's code-execution trust band; the wrapper adds no wider
  authority and never suggests elevated installs.

## 8. Reporting

Security issues: GitHub private vulnerability reporting via this repo's Security
tab; ordinary bugs follow the repository issue policy. Please do not file public
issues for suspected vulnerabilities.
