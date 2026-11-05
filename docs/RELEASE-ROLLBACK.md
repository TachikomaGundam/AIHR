# RELEASE-ROLLBACK — Retract or supersede a published release

Published artifacts are append-only memory. This playbook marks bad
artifacts so default resolution avoids them and users see why — it never
erases history. If you are about to ship instead, see docs/PUSH.md.

## Ground rules (why "mark, never erase")

1. **PyPI forbids re-uploading a version number, yanked or not** — once
   `X.Y.Z` exists there, `X.Y.Z` is burned forever. The recovery for any
   bad release is always the next version (forward-fix), never a re-cut.
2. **Published git tags are immutable.** Never move, reuse, or delete a tag
   that fired a release. A corrected build gets a new tag.
3. **Deleting a GitHub Release permanently breaks its asset URLs** — direct
   installs pinned to those URLs die silently. Mark instead; delete only
   when an asset itself leaks a secret (see §"GitHub Release", case C).
4. Every retraction is a *record*: registry-side yank/deprecate reason
   text, a CHANGELOG line, and the forward-fix version named in all three
   places.

## Which layer do you need?

| Symptom | Layer |
|---|---|
| A user's box is misconfigured / a bad `hr apply` or activation | §1 product commands (no registry involved) |
| A published PyPI version is broken for pip users | §2 yank + forward-fix |
| A GitHub Release carries wrong assets or wrong notes | §3 |
| A plugin version on npm is broken | §4 |
| A credential actually leaked into a published artifact | §5 (fire) |

## 1. Product-level rollback (user-facing, already shipped)

- `hr apply-backups` / `hr apply-rollback <backup>` — undo a FastDraw
  verdict application from its backup manifest.
- `hr release-list` / `hr release-rollback` — return to the pre-activation
  symlink + config state of an installed release.
  Command table: README.md ("Install & release management").

These touch only the user's machine. Nothing in this file does.

## 2. PyPI — yank a version (primary engine channel)

**Effect.** A yanked file is skipped by default resolution (`pip install
aihr` will not pick it), still installable by exact pin (`aihr==X.Y.Z`)
with a warning, and still downloadable. Yank is reversible and keeps the
audit trail. Deletion is effectively unavailable: PyPI only permits file
removal within a short window for brand-new projects, never once a later
version exists.

**Operation (web UI only — twine has no yank command):**

1. <https://pypi.org/manage/project/aihr/releases/> (requires the project
   owner's token/2FA — maintainer's credential, never in this repo).
2. Expand version `X.Y.Z` → for **each file** (wheel *and* sdist) click
   **yank**.
3. Reason text becomes public API output; write it for the user who hits
   the pin, in the established form:
   `missing shipped configs; use <fixed-version>` (0.6.9),
   `preflight ignores the user overlay layer; use <fixed-version>` (0.6.10).
4. Verify mechanically — the JSON API exposes the flag per file:

   ```bash
   curl -s https://pypi.org/pypi/aihr/X.Y.Z/json \
     | python3 -c 'import json,sys; [print(u["filename"], "->", u["yanked"]) for u in json.load(sys.stdin)["urls"]]'
   ```

   Both files must report `True` (non-empty string = yanked + reason).
5. Confirm default resolution skips it:
   `python3 -m pip index versions aihr` (pip ≥ 21.2 lists yanked entries
   with a marker) and a fresh venv `pip install aihr` resolves to the fix.

**Verified history (this project, 2026-11-04):** `0.6.9` yanked (wheel
shipped without `configs/preflight.yaml` + `configs/model_parity.yaml` —
pip-channel preflight crashed with FileNotFoundError, caught on the turnkey
testbed, ship-list pin added in `tests/test_package_data.py`); `0.6.10`
yanked (preflight kept a private two-file merge and could not see the
`~/.aihr/configs` user overlay layer — caught by live testbed use, fixed by
the unified three-layer loader). Both forward-fixed (0.6.10 → 0.6.11), both
registry-verified `yanked=True` afterwards.

## 3. GitHub Release

**Case A — wrong notes / superseded, assets fine (default):**

```bash
gh release edit vX.Y.Z --repo TachikomaGundam/AIHR \
  --notes "SUPERSEDED by vA.B.C — <one-line reason>. Do not use these assets." \
  --latest=false
```

Keeping the release keeps every asset URL alive for anyone mid-install.

**Case B — a specific asset is broken but the release is right:** delete
just that asset and re-upload the correct one for the *same* tag only if
the tag's source itself is unchanged; otherwise Case A + ship forward:

```bash
gh release delete-asset vX.Y.Z <asset-name> --repo TachikomaGundam/AIHR --yes
```

**Case C — an asset leaks a live credential (fire):** see §5. Asset
deletion is the *last* step there, never the first — the credential is the
emergency, the URL is the evidence.

**Never** run `gh release delete --cleanup-tag` on a published tag.

## 4. npm plugins (`opencode-hr-agent`, `opencode-fastdraw`)

**Deprecate (default lever, permanent):**

```bash
npm deprecate opencode-hr-agent@X.Y.Z "<reason>; use <fixed-version>"
```

`npm install` still resolves it only on exact pin, now with the warning
shown. Verify: `npm view opencode-hr-agent@X.Y.Z deprecated` prints the
reason.

**Unpublish** is only available within npm's short post-publish window
(72 h from first publish of that version, and only if it has no
dependents pulled in by real users); after that, unpublish requests go
through npm support and are granted for moderation cases (leaked secrets,
malware) — i.e. only for §5. Plan on deprecate being the lever.

**Train dropped a wagon** (the `npm-publish.yml` guard job is red because
the publish step died mid-run): fix whatever made the version bad, bump
the plugin's `package.json` version, tag again. If the registry version is
already correct and only the guard raced CDN lag, re-fire
`npm-publish.yml` via **Run workflow** (workflow_dispatch) — the publish
step skips versions already on the registry, so re-fires are safe.

## 5. A live credential was actually published (fire order)

Speed matters in this order, not the intuitive one:

1. **Rotate the credential first.** The artifact is already cached
   everywhere; revocation is the only act that actually contains the leak.
2. Revoke/rotate any secondary systems that reuse it (DBs reachable with
   it, tokens minted from it).
3. Yank the PyPI files (§2) and deprecate npm versions (§4) with the
   reason `security: superseded by <fixed-version>`.
4. Delete the leaking asset / release (Case C, §3) — with the rotation
   already done, losing the URL loses nothing you need.
5. Publish the fixed version (no secret in it), verify §2 step 5.
6. Sweep your own distribution surface: build dirs, archives, evidence
   copies all hold copies of a secret forever — inventory and treat each,
   or the "fixed" version just re-leaks from the next bundle.

## Post-retraction checklist (every layer)

- [ ] Registry-side mark visible via the verification command for that
      registry (§2 step 4, §3 `gh release view`, §4 `npm view … deprecated`).
- [ ] CHANGELOG entry on the fixing commit names the retracted version and
      reason.
- [ ] Forward-fix released and §0/§6 gates of docs/PUSH.md ran on it.
- [ ] Ops incident note records: what was bad, which versions are marked,
      what the clean floor is (`min installable == <fixed-version>`).
