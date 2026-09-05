# Plugin Security Model — `opencode-hr-agent`

Trust boundary and shipped-surface guarantees for the OpenCode HR plugin
(`opencode_plugin/`). Release-side procedures: `docs/PUSH.md` §5.

## 1. Trust boundary

The plugin performs exactly one privileged action: launching `python3 -m hr ...`
through `execFile` — never a shell. Trust assumptions, plainly: `HR_HOME`
(default `~/hr`) and the `PYTHONPATH` injected into the child, the resolved
`python3` binary, and everything under `HR_HOME` are trusted same-user surface —
an attacker who can write those can already run code as you. Hardening target:
**no attack surface beyond that baseline** — no shell, no remote fetching, no
`eval`, no dynamic module loading, no credential handling of the plugin's own.
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
ships. Install with an exact version pin:
`npm install -g "opencode-hr-agent@<version>"` (quote it for zsh); a floating
`-g` install auto-updates silently.

## 4. No lifecycle scripts

`npm install` never runs project scripts here: the manifest has no
pretest/posttest/prepare/install hooks — only `test` (asserted at CI and every
release, `docs/PUSH.md` §5 item 3). Caveat: npm's `allow-same-user` default
(`true`) means same-user tarball takeover of an already-published version
remains possible; a scoped org with per-package access grants is stronger
isolation. Documented posture — no config change implied or made here.

## 5. Pack invariant

The published tarball MUST equal exactly {`package.json`, `server.ts`,
`hr-invocation.ts`} — verified via `npm pack --dry-run` at every release, run
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

## 7. Reporting

Security issues: GitHub private vulnerability reporting via this repo's Security
tab; ordinary bugs follow the repository issue policy. Please do not file public
issues for suspected vulnerabilities.
