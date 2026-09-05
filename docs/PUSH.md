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

> **Published state (verified 2026-09-05 against the registries):**
> **PyPI `aihr` 0.2.1 — UPLOADED and registry-verified** (latest; wheel +
> sdist sha256 byte-identical to the locally validated artifacts) ·
> npm `opencode-hr-agent` **0.2.0** · npm `opencode-fastdraw` **1.1.0**
> (2026-09-02; the 1.1.0 release changed only the plugin JS, so no new
> engine build was due).
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
pip install "aihr[vision] @ https://github.com/TachikomaGundam/AIHR/releases/download/v0.2.0/aihr-0.2.0-py3-none-any.whl"
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

## 5. npm publish (two packages)

```bash
cd ~/workspace/harness/hr

# --- opencode-hr-agent 0.2.0 (unscoped) ---
cd opencode_plugin
npm login                 # add NPM_OTP=... to env if you use a TOTP app
npm publish --access public --otp "$NPM_OTP"
cd ..

# --- opencode-fastdraw 1.0.0 (unscoped) ---
cd fastdraw
npm login
npm publish --access public --otp "$NPM_OTP"
cd ..
```

Both names are unscoped — confirm you own them in npm. The `files` lists in
`package.json` restrict what ships (no `.build/`, no secrets, no absolute
paths — verified at pack time). If you use a hardware key instead of TOTP,
omit `--otp` and answer the interactive prompt.

## 6. Verify from the second machine

```bash
# engine (primary — PyPI):
pip install "aihr[vision]"
hr --help                                # expect all 23 commands
# engine (mirror — Release wheel, no PyPI required):
pip install "aihr[vision] @ https://github.com/TachikomaGundam/AIHR/releases/download/v0.2.0/aihr-0.2.0-py3-none-any.whl"
npm view opencode-hr-agent               # expect 0.2.0
npm view opencode-fastdraw               # expect 1.0.0
npm install -g opencode-hr-agent opencode-fastdraw   # or per-project
git clone git@github.com:TachikomaGundam/AIHR.git    # history: all TachikomaGundam
```

---

## What must NOT happen

- Do not reuse the scratch `wikijs` DSN from this machine on the second box —
  the CLI takes `HR_TEST_PG_DSN`/per-provider keys from `hr.toml` / env that
  you configure locally (template: `configs/hr.toml.example`).
- Do not re-upload if a publish partially fails without checking
  https://pypi.org/p/aihr / https://www.npmjs.com/package/... first —
  `twine upload`/`npm publish` of the same version are rejects, not re-runs.
- PyPI test index (TestPyPI) needs a different token/scoped project; this
  guide targets production PyPI directly.