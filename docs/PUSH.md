# PUSH — Perform the release (user executes)

Everything below is **executed by you** — the agent has no credentials and
deliberately never touches publish channels. All prior preparation (history
rewrite, secrets sweep, artifacts, tags, remote) is already done; these are
the only remaining steps. Run from this repo on **this machine** unless a
section says otherwise.

## Artifact locations (built at the final rewritten HEAD — see W3 report)

| Artifact | Path on this machine |
|----------|----------------------|
| Python wheel | `/tmp/opencode/hr-ship-artifacts/aihr-0.2.0-*.whl` |
| Python sdist | `/tmp/opencode/hr-ship-artifacts/aihr-0.2.0.tar.gz` |
| npm pack (opencode-hr-agent) | `/tmp/opencode/hr-ship-artifacts/opencode-hr-agent-0.2.0.tgz` |
| npm pack (opencode-fastdraw) | `/tmp/opencode/hr-ship-artifacts/opencode-fastdraw-1.0.0.tgz` |

Repo: `~/workspace/harness/hr` (git root), branch `main`, tags `v0.2.0`
and `v0.2.1` set. Origin is already configured as
`git@github.com:TachikomaGundam/AIHR.git`.

> **Published state (verified 2026-09-11 against the registries):**
> **PyPI `aihr` — latest is still 0.2.2; the 0.3.0 upload below is
> MAINTAINER-PENDING (§4 commands, token never through chat/agent).** ·
> npm `opencode-hr-agent` 0.2.2 · npm `opencode-fastdraw` 1.2.0 — both
> unchanged by 0.3.0 (no plugin JS change; `hrContractVersion` stays
> 1.0.0 — new shipped files are not an on-disk contract change).
>
> **0.3.0** (AIHR item 9: turnkey `aihr-db` container +
> `hr db-up/db-down/db-status` + 4-step DSN chain + foreign-takeover
> guard; end of the shared-`wikijs`-superuser arrangement) is committed &
> pushed (`f5ca1ed` + `5ed08ed`), CI green on 3.12+3.14 (+node 22/24),
> tagged `v0.3.0`, and mirrored as a **GitHub Release with wheel+sdist
> assets**: <https://github.com/TachikomaGundam/AIHR/releases/tag/v0.3.0>.
> Artifacts built at `5ed08ed`, twine check PASSED, sha256:
> wheel `0cad36eaac959d0ca07e94a2bcbe2947c4c342ee3c28c8d60d9a72485721e662`,
> sdist `2a45e2776dee13a75a2da02a51a1c1ffc0ae03e6716da61cc5593987c259623c`
> (GitHub asset digests verified identical 2026-09-11). This machine's
> engine install is `aihr 0.3.0` (editable → repo).
>
> **0.2.1** (audit-queue P0/P1 fixes + FastDraw contract decouple) is
> committed & pushed (`a750773`), CI green on 3.12+3.14, tagged `v0.2.1`,
> published on PyPI (§4), and mirrored as a **GitHub Release with
> wheel+sdist assets**: <https://github.com/TachikomaGundam/AIHR/releases/tag/v0.2.1>.
> Shipped artifacts (built at `a750773`, twine check PASSED, verified to
> contain the fixes) live in `/tmp/opencode/hr-ship-artifacts/`; the stale
> 09-02 pre-audit build was moved to `stale-0902/`. This machine's engine
> install is `aihr 0.2.1` (editable → repo).

---

## 0. Preflight (30 s)

```bash
cd ~/workspace/harness/hr
git status --porcelain            # expect: empty
git log --all --format='%ae' | sort -u   # expect: only TachikomaGundam@users.noreply.github.com
git tag -n                         # expect: v0.2.0 annotated
sha256sum /tmp/opencode/hr-ship-artifacts/*
```

## 1. Create the GitHub repository

Option A — web: open https://github.com/new → owner **TachikomaGundam**,
name **AIHR**, description *"HR Agent (人事) — model evaluation and role
assignment for oh-my-openagent"*, **Public** (default), *do NOT* initialize
with README/.gitignore/LICENSE (all exist in the repo already).
Create.

Option B — CLI (if `gh` is installed and authed):

```bash
gh repo create TachikomaGundam/AIHR --public --description "HR Agent (人事) — model evaluation and role assignment for oh-my-openagent"
```

> **PAT scope gotcha (learned at first push):** a fine-grained token MUST
> have **Workflows: Read and write** in addition to Contents — this repo
> ships `.github/workflows/ci.yml`, and GitHub rejects pushes containing
> workflow paths when the token lacks that scope. Classic tokens with the
> `repo` scope are unaffected.

## 2. Push (the remote is already added)

```bash
cd ~/workspace/harness/hr
git remote -v          # confirm origin -> git@github.com:TachikomaGundam/AIHR.git
git push -u origin main --follow-tags
```

SSH key required for `git@github.com`. If you prefer HTTPS+PAT:
`git remote set-url origin https://github.com/TachikomaGundam/AIHR.git`
and use a PAT as the password when prompted.

> **Status 2026-09-01:** the first push was executed by the user over
> HTTPS+PAT (`origin/main` == tag == the fastdraw-fix commit). An ed25519
> key was generated on this machine at `~/.ssh/id_ed25519_github` and the
> remote switched back to the SSH URL above — add the public key to
> GitHub → Settings → SSH and GPG keys once, and future pushes need no
> token. Verify with `ssh -T git@github.com` (expect: "Hi TachikomaGundam!").

## 3. GitHub Release v0.2.0 (web)

1. Open https://github.com/TachikomaGundam/AIHR/releases/new
2. Tag: `v0.2.0` (exists) · Target: `main` · Title: `HR Agent 0.2.0 — unified evaluator`
3. Notes (suggested):

   > Unified `hr` CLI (23 commands): discover/bench/verdict/health/sweeps/
   > calibrate/reference/research/publish/recommend/status/apply.
   > 8 livebench batteries, 52 items; half-width capability thresholds;
   > health gates; 18 seats; FastDraw verdict seam. PyPI distribution
   > renamed `hr-agent` → `aihr` (import `hr`, console script `hr`).
   > Full history authored as TachikomaGundam.

4. Attach binaries (drag & drop):
   - `/tmp/opencode/hr-ship-artifacts/aihr-0.2.0-py3-none-any.whl`
   - `/tmp/opencode/hr-ship-artifacts/aihr-0.2.0.tar.gz`
   - `/tmp/opencode/hr-ship-artifacts/opencode-hr-agent-0.2.0.tgz`
   - `/tmp/opencode/hr-ship-artifacts/opencode-fastdraw-1.0.0.tgz`
5. Publish release.

## 4. PyPI upload — PRIMARY engine channel (README front page)

The engine ships on PyPI as **`aihr`**; `pip install "aihr[vision]"` is the
install path the README advertises, so every engine release must land on
PyPI. The GitHub Release wheel (§3) stays as a mirror for direct-URL
installs:

```bash
pip install "aihr[vision] @ https://github.com/TachikomaGundam/AIHR/releases/download/v0.2.2/aihr-0.2.2-py3-none-any.whl"
```

Upload procedure (`twine` already installed at `~/.local/bin/twine` via
`python3 -m pip install --user twine`):

```bash
cd /tmp/opencode/hr-ship-artifacts
twine upload aihr-<VERSION>-py3-none-any.whl aihr-<VERSION>.tar.gz
# prompts: username -> __token__   (literally the underscore token)
#          password -> your PyPI API token (project: aihr)
```

Non-interactive alternative (token still never stored in the repo):

```bash
TWINE_USERNAME=__token__ TWINE_PASSWORD=pypi-xxxxxxxx twine upload /tmp/opencode/hr-ship-artifacts/aihr-<VERSION>-*
```

## 5. npm publish (two packages): executed by the human maintainer

```bash
cd ~/workspace/harness/hr

# --- opencode-hr-agent 0.2.2 (unscoped) ---
# Every npm step for this package runs OUTSIDE opencode_plugin/ (checklist
# item 1 below): stay in the repo root and pass the folder as an argument.
npm login                 # add NPM_OTP=... to env if you use a TOTP app
npm publish ./opencode_plugin --access public --otp "$NPM_OTP"   # maintainer only

# --- opencode-fastdraw 1.1.1 (unscoped) ---
cd fastdraw
npm login
npm publish --access public --otp "$NPM_OTP"   # maintainer only
cd ..
```

Both names are unscoped — confirm you own them in npm. The `files` lists in
`package.json` restrict what ships (no `.build/`, no secrets, no absolute
paths — verified at pack time). If you use a hardware key instead of TOTP,
omit `--otp` and answer the interactive prompt.

Pre-publish checklist (agents may prepare items 1-3; items 4-5 are the human
maintainer's call):

1. Test out-of-package: `npm --prefix "$RUNNER_TEMP/pkg" run test` against the
   CI-style copy the node test lane builds (`mkdir -p "$RUNNER_TEMP/pkg"`, then
   copy `package.json`, `server.ts`, `hr-invocation.ts`, `install-cli.js`
   and `test/` into it (plus `fastdraw/package.json` two levels up — drift guard);
   see `.github/workflows/ci.yml`). NEVER run `npm` inside `opencode_plugin/`:
   its gitignored package-local `.npmrc` would load into npm's env view.
2. Content check: `npm pack --dry-run` in a /tmp copy (same pattern as item 1).
   The packed content MUST equal exactly {package.json, server.ts,
   hr-invocation.ts, install-cli.js}, nothing more, nothing less. The `bin`
   entry (`opencode-hr`) must resolve: `npm i -g ./<tgz> --prefix /tmp/x && /tmp/x/bin/opencode-hr status`.
3. Manifest asserts via `node -e`: `dependencies` is empty AND no lifecycle
   scripts exist except `test` (pretest, posttest and prepare are FORBIDDEN);
   plugin specs float at `@latest` in `hr/cli_setup.py` and
   `install-cli.js` — there are NO version pins to keep in sync anymore (CI
   enforces the @latest specs via tests/test_plugin_setup.py +
   opencode_plugin/test/install-cli.test.mjs).
4. Publish execution is RESERVED to the human maintainer: agents prepare, never publish.
5. Tag decision (user-side): this repo has NO version->tag convention; the
   existing `v0.2.1` tag points to an engine-only release, not to this
   package. For the next release prefer either `v0.2.2` or a per-package tag
   like `opencode-hr-agent@0.2.2` (this is the convention adopted since 0.2.1);
   agents never tag.

## 6. Verify from the second machine

```bash
# engine (primary — PyPI):
pip install "aihr[vision]"
hr --help                                # expect all 27 commands
hr db-up && hr status                    # turnkey DB: docker required, ZERO env exports
# engine (mirror — Release wheel, no PyPI required):
pip install "aihr[vision] @ https://github.com/TachikomaGundam/AIHR/releases/download/v0.2.2/aihr-0.2.2-py3-none-any.whl"
npm view opencode-hr-agent               # expect 0.2.2
npm view opencode-fastdraw               # expect 1.1.1
# One-shot plugin bootstrap (installs the pair at @latest via npm AND registers
# both opencode config files):
hr setup
hr setup --no-npm                        # re-run registration only; expect already-registered OK
# Fallback (config-only, no npm): add to ~/.config/opencode/opencode.json
# "plugin": "opencode-hr-agent@latest", "opencode-fastdraw@latest" and to
# ~/.config/opencode/tui.json "plugin": "opencode-fastdraw@latest" — a bare
# `npm install -g` alone is NEVER visible to opencode (prefix not scanned).
# Restart opencode; expect hr_* + fastdraw_* agent tools, /fastdraw + <leader>m in TUI.
# (Machine-readable discovery-contract proof recipe: see docs/PLUGIN_SECURITY.md notes
#  or ops/dev-env-pitfalls wiki page — /experimental/tool/ids probe.)
git clone git@github.com:TachikomaGundam/AIHR.git    # history: all TachikomaGundam
```

> **Takeover posture:** npm's `allow-same-user` default is `true`, so the same
> account can still overwrite an already-published tarball of the same version;
> same-user takeover therefore remains possible. Publishing under a scoped org
> with per-package access grants is stronger isolation. Note only: no registry
> config change is implied or made here.

---

## What must NOT happen

- Do not reuse this machine's local scratch PostgreSQL DSN on the second box —
  the CLI takes `HR_TEST_PG_DSN`/per-provider keys from `hr.toml` / env that
  you configure locally (template: `hr.toml.example`).
- Do not re-upload if a publish partially fails without checking
  https://pypi.org/p/aihr / https://www.npmjs.com/package/... first —
  `twine upload`/`npm publish` of the same version are rejects, not re-runs;
  the human maintainer reviews registry state before any retry.
- PyPI test index (TestPyPI) needs a different token/scoped project; this
  guide targets production PyPI directly.