# PUSH — Perform the release (human executes)

Everything below is **executed by the human maintainer** — the agent has no
credentials and deliberately never touches publish channels. All prior
preparation (gates green, artifacts built, changelog written) is done by the
agent on the tip commit; these are the remaining steps. Run from this repo
unless a section says otherwise. If anything published turns out to be
broken, the retraction playbook is **docs/RELEASE-ROLLBACK.md**.

## Channels — what travels on each

| Channel | Payload | Lane |
|---|---|---|
| GitHub Release | turnkey bundles (4 platform legs) + on-demand interpreter assets | `release.yml`, auto on tag `v[0-9]*`; creates a **draft**, human un-drafts after green |
| PyPI `aihr` | wheel + sdist — the README's primary install path | manual `twine upload` by the maintainer (the only manual channel) |
| npm `opencode-hr-agent`, `opencode-fastdraw` | the two OpenCode plugins | `npm-publish.yml` auto on tag `v*` via **npm Trusted Publishing (OIDC)** — no npm tokens exist in this repo or its secrets, by design |

The three channels share one train: one engine tag must leave all three
consistent. `npm-publish.yml` ends with a guard job asserting the registry
serves the tag's plugin versions — a red guard means the train shed a wagon
(see RELEASE-ROLLBACK.md §"npm").

## 0. Preflight (the agent closes these before the cut; re-check the list)

1. Full test suite green on the tip, including the DB-gated modes — see
   `tests/README.md` (Skip Semantics + the CI recipe).
2. Universality gate green **with the secret scan actually running**:
   `HR_COMPOSE_FILE=<compose-file> bash scripts/check_universal.sh`
   (or `HR_SECRET_NEEDLE=<value>`). A run without a needle skips check (d)
   with a notice — locally acceptable, never sufficient for a release.
3. `uv run ruff check .` and basedpyright facade clean.
4. Release surface manifest green (`tests/test_release_surface.py`,
   `tests/test_package_data.py` — the latter is the ship-list pin that
   caught the 0.6.10 wheel defect on a customer box).
5. Plugin lanes from a clean checkout with zero build step: copy the
   explicit file list from `.github/workflows/ci.yml` (plugin lane) to a
   temp dir and run `node --test test/*.test.mjs` on any engines-satisfying
   node. The plugin core (`opencode_plugin/hr-invocation-core.mjs`) is plain
   `.mjs` precisely so this works on distro node builds without TypeScript
   support — no `.js` fallback file exists anymore, and none may be added.
6. Turnkey Acceptance Protocol (§6) run on the designated testbed box.

## 1. Version bump (one commit)

- `pyproject.toml` `[project] version = "X.Y.Z"`.
- `uv.lock`: edit **only the project entry's version**; leave dependency
  hashes untouched. Do not re-resolve the lock for a version-only bump.
- `CHANGELOG.md`: new `## [X.Y.Z]` section at the top.
- Commit as `chore(release): version X.Y.Z`. No other file may ride in this
  commit.

## 2. Build the PyPI artifacts (repo-local, gitignored)

```bash
uv build --out pypi-dist-<VERSION>
twine check --strict pypi-dist-<VERSION>/*
```

Both files must pass `--strict`. Record the `sha256sum` of both artifacts —
the same digests are re-verified against the registry after upload (§5).

## 3. Push + tag (human; the credential-bearing step)

```bash
git push origin main
git tag -a vX.Y.Z -m "X.Y.Z: <one-line release summary>"
git push origin vX.Y.Z
```

- Annotated tags with a release summary are the convention since 0.6.9.
- Published tags are immutable: never move, never reuse, never delete one
  (a re-cut lives at the next version — RELEASE-ROLLBACK.md).
- If your token is fine-grained it needs **Workflows: Read and write** in
  addition to Contents — pushes touching `.github/workflows/` are rejected
  without it. Classic `repo`-scoped tokens are unaffected.
- Transport note (release-day lesson 2026-11-04): if SSH auth mysteriously
  denies while the HTTPS API works, disable any HTTP proxy first — GitHub
  risk-controls SSH source addresses; push+tag with the proxy off.

## 4. Tag-triggered automation (watch, then un-draft)

Watch both workflows the tag fires:

```bash
gh run watch --repo TachikomaGundam/AIHR            # or: gh run list
```

- `release.yml`: four bundle legs (linux-x86_64, windows-x86_64,
  macos-aarch64, macos-x86_64 — **all four are hard gates** as of
  2026-11-05), a draft GitHub Release with the bundles, and the
  Linux/Windows turnkey smoke lanes. Under macOS runner congestion a job
  can die queued at its `timeout-minutes`; re-run rather than re-tag:
  `gh run rerun <run-id> --failed`.
- `npm-publish.yml`: publishes each plugin **only if its package.json
  version is not already on the registry** (idempotent re-fires are safe),
  then the guard job asserts registry == tag source.

When every job is green, publish the draft:

```bash
gh release edit vX.Y.Z --repo TachikomaGundam/AIHR --draft=false
```

## 5. PyPI upload (human; primary channel)

```bash
twine upload pypi-dist-<VERSION>/*
# username: __token__   (literally)   password: your PyPI API token (project aihr)
```

- **Never re-upload a version.** `twine upload` of an existing version is a
  reject, not a re-run; if a publish partially fails, inspect
  <https://pypi.org/p/aihr> first and decide per RELEASE-ROLLBACK.md.
- Post-verify: fresh venv installs the new version; the downloaded file
  digests match §2; `pip index versions aihr` shows the expected ladder with
  any yanked entries marked.
- The GitHub Release wheel stays a mirror for direct-URL installs:
  `pip install "aihr[vision] @ https://github.com/TachikomaGundam/AIHR/releases/download/vX.Y.Z/aihr-X.Y.Z-py3-none-any.whl"`

## 6. The Turnkey Acceptance Protocol — the permanent turnkey courtroom

The designated turnkey testbed box (address + SSH credentials deliberately
NOT recorded in this repo — identity here is the hardware profile only; the
machine owner keeps the live address in the ops layer) (Ubuntu 26.04, system
python 3.14 under PEP 668, **no Docker installed**, no passwordless sudo) is
this project's permanent turnkey courtroom — the exact machine where 0.3.0's
"turnkey" claim died with "docker not found". Every bundle-version ship
passes this protocol on the testbed box from factory state before it ships:
it is the standing release gate, not a one-off (ops backlog items 11 and 14,
maintained outside the product repo).

Steps run in order, on a clean box, nothing skipped:

1. **One command. Nothing preinstalled, zero sudo, zero prerequisites:**
   (Linux floor: glibc ≥ 2.28 — the vendored-PostgreSQL baseline; nothing
   else.)

   ```bash
   curl -fsSL https://raw.githubusercontent.com/TachikomaGundam/AIHR/main/scripts/install.sh | sh
   ```

2. **Green database on the embedded backend.** Fresh shell so the
   `~/.aihr/bin` shim is on PATH:

   ```bash
   hr db-up && hr status      # expect: green, ZERO env exports, no sudo
   ```

   `hr db-status` must name the embedded vendored Postgres as the live
   backend — the testbed box has no Docker, so the compose lane cannot even
   mask a broken default.

3. **opencode tool surface.** Restart opencode on the testbed box and verify
   BOTH halves of the dual registration written by `hr install-post`: the
   `plugin` array in `~/.config/opencode/opencode.jsonc` (on this box the
   file IS `.jsonc` — there is no `opencode.json`) and the array in
   `tui.json`. Expect `hr_*` / `fastdraw_*` agent tools enumerable and
   `/fastdraw` + `<leader>m` alive in the TUI. (Machine-readable
   discovery-contract proof: the `/experimental/tool/ids` probe — see
   docs/PLUGIN_SECURITY.md notes.) No npm step is run by hand —
   opencode/bun auto-fetches the package specs at startup.
   **Enumeration is not usage — it is a necessary, not sufficient, half.**
   At least one `hr_*` call must be INVOKED live in the restarted session
   and must return an engine reply (on a factory-fresh database the correct,
   passing answer is `error: no sweeps found in hr.sweep` — a real
   round-trip through the plugin's launch chain into the engine and its
   database). An `HR command failed: no HR engine resolved` reply on a
   turnkey box is a release blocker. Regression (2026-09-29): the 0.2.2
   plugin was pip-lane-only (`python3 -m hr`, `PYTHONPATH=~/hr`) and
   enumeration alone passed while every live call failed and the guidance
   text taught session agents to `pip install --user --break-system-packages`
   — contaminating user-site and violating the self-containment contract.
   The plugin now resolves the engine turnkey-first (`HR_BIN` →
   `~/.aihr/bin/hr` → pip lane; see `opencode_plugin/hr-invocation-core.mjs`
   `resolveHrLaunch`), but the live-invocation step stays in this protocol
   permanently: the ruler that only checks the menu will certify a kitchen
   that cannot cook.

4. **Full reversal:**

   ```bash
   hr self-uninstall --yes
   ```

5. **Filesystem audit == printed residual manifest, EXACTLY.** Walk the box
   (owned-dir remnants, shell rc files, the two opencode configs, bin dirs,
   caches) and diff every leftover against the residual manifest printed in
   step 4. Every surviving path must appear there with its exact removal
   command (e.g. the bun plugin cache under `~/.cache/opencode/packages/…`);
   any leftover absent from the manifest is a release blocker. Contract law:
   uninstall removes ALL it placed or reports each residual — nothing silent.

6. **First-boot live bench (ops backlog item 14 standing gate):** on the
   freshly installed machine run the owner's real thing — `hr discover`,
   then `hr run` on one discoverable model against a live endpoint.
   Acceptance = measurements > 0 and no `adapter_setup_failure` rows.
   Artifact checks alone never close this courtroom; the machine must
   produce observations.

The same protocol then runs on the Windows and macOS boxes when available.
A bundle version without a green testbed-box run does not ship.

> **npm takeover posture:** npm's `allow-same-user` default is `true`, so
> the same account can still overwrite an already-published tarball of the
> same version; same-user takeover therefore remains possible. Publishing
> under a scoped org with per-package access grants is stronger isolation.
> Note only: no registry config change is implied or made here.

---

## What must NOT happen

- The installers (`install.sh` / `install.ps1`) and their tail
  `hr install-post` NEVER run sudo or request elevation — on a box without
  passwordless sudo (the box is that box, permanently) the install must
  complete clean or the release fails the gate.
- Installer writes are limited to: the owned directory (`~/.aihr` /
  `%LOCALAPPDATA%\aihr`), one clearly-marked PATH block in the shell rc, the
  two opencode config arrays (`plugin` in `opencode.jsonc` + `tui.json`),
  and the `~/.local/bin`-style PATH shim. The rc block, the two config
  arrays and the shim are the ONLY sanctioned writes outside the owned dir,
  and every one of them is receipted; anything written off-receipt is a
  contract violation that `hr self-uninstall` will surface as an unexplained
  residual.
- No credential, token, or machine-local path ever enters this repo —
  including this file. The secret-scan needle in particular is passed via
  env/secret only, and CI fails closed when `--require-secret-scan` finds
  no needle (§0.2, `ci.yml`).
- Do not reuse this machine's local scratch PostgreSQL DSN on a second box —
  the CLI takes `HR_TEST_PG_DSN`/per-provider keys from `hr.toml` / env that
  you configure locally (template: `hr.toml.example`).
- Do not re-upload if a publish partially fails without checking
  <https://pypi.org/p/aihr> / <https://www.npmjs.com/package/...> first —
  `twine upload`/`npm publish` of the same version are rejects, not
  re-runs; the human maintainer reviews registry state before any retry.
- Do not "fix" a shipped version in place on any registry. Published
  payloads are marked (yank/deprecate) and superseded forward — never
  rewritten. The full playbook: docs/RELEASE-ROLLBACK.md.
- PyPI test index (TestPyPI) needs a different token/scoped project; this
  guide targets production PyPI directly.
