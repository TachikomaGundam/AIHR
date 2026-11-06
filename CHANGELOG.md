# Changelog

Notable changes per release. The authoritative notes for every version
(including full asset lists and checksums) live on the
[GitHub Releases page](https://github.com/TachikomaGundam/AIHR/releases);
this file summarizes the headline of each.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `docs/RELEASE-ROLLBACK.md`: the release-retraction playbook (PyPI yank,
  GitHub Release marking, npm deprecate, leaked-credential fire order),
  recording the verified 0.6.9/0.6.10 yank operations in public docs for
  the first time.
- `check_universal.sh --require-secret-scan`: fail-closed mode for the
  secret scan; CI now passes it with the needle wired to the repository
  secret `HR_SECRET_NEEDLE`, so a CI run can no longer certify the secret
  gate by leaving it unconfigured.
- `tests/test_plugin_import_closure.py`: every relative import of a shipped
  plugin source must exist and be registered in both ship lists
  (`package.json files`, `RELEASE_ASSETS`) — closes the plugin-side
  missing-file class the 0.6.10 wheel pin closed for Python data files.

### Published
- `opencode-hr-agent` 0.2.4: the plugin package rebuilt on the plain-.mjs
  core above, published through the npm train so existing installs receive the
  clean-checkout fix without waiting for the next engine release.

### Changed
- CI universality job now checks out full history (`fetch-depth: 0`): the secret scan's
  promise is a full-history sweep, which the default depth-1 checkout silently shrank
  to the tip commit (found while closing the 2026-11-05 eval campaign).
- `docs/PUSH.md` rewritten for the 0.6.x release lanes (tag-triggered
  bundle workflow + draft release, manual PyPI twine lane, npm Trusted
  Publishing train with guard job); the v0.2.x-era repo-creation and
  artifact-transport sections are retired.
- Plugin pure helpers moved to plain `opencode_plugin/hr-invocation-core.mjs`
  (`hr-invocation.ts` is now a typed re-export façade): the plugin test lane
  runs from a clean checkout on any engines-satisfying node — including
  distro builds without the TypeScript translator — with no gitignored
  `.js` fallback (removed from `.gitignore` with it).
- Release bundles: `macos-x86_64` promoted from experimental to a hard gate
  (three consecutive green release runs: v0.6.8, v0.6.11, v0.6.12).

## [0.6.12] - 2026-11-05

### Fixed
- Tape recording with the openai stack captured nothing against the real
  parser: the SSE parser breaks out of `iter_lines` at the `data: [DONE]`
  sentinel, leaving the tee generator suspended and never finalizing. The
  tee now treats the protocol sentinel as completion (recorded when the
  sentinel passed or the stream ran out; partial streams still never
  reach the tape). First live record + CLI replay round-trip verified on
  the local fleet.
- Malformed config values now fail loud by name instead of crashing in
  `float()`: pricing entries must be numbers (USD per 1M tokens - a
  nested dict from a hand-written overlay used to raise a deep
  TypeError), and preflight `budget.*`/`abort.error_rate` scalars are
  validated with the offending key named.

## [0.6.11] - 2026-11-04

### Fixed
- `hr preflight` (and `load_preflight_config`) now read the unified
  three-layer config loader: the per-user `~/.aihr/configs/preflight.local.yaml`
  overlay was previously ignored by the preflight's private two-file merge,
  so a pip-installed box with a user-layer overlay reported "no anchor
  models" (caught on the 191 testbed during the 0.6.10 field pass).

## [0.6.10] - 2026-11-04

### Fixed
- `configs/preflight.yaml` and `configs/model_parity.yaml` were missing from
  the wheel's shipped data (`share/aihr/configs`) - pip-installed preflight
  and the parity gate could not find their tracked config (found by the
  first pip-channel field use on the 191 testbed; invisible in
  repo/HR_HOME installs). A new pin
  (`tests/test_package_data.py`) fails when any tracked config is absent
  from the ship list.

### Added
- `hr bench` / `hr preflight` gained `--tape-record DIR` / `--tape-replay DIR`
  (S2b): explicit recording with a manifest receipt (path + sha256 +
  endpoints), and strict replay that answers only from the tape — a miss
  aborts by name with the re-record procedure and never falls through to
  live calls.
- Per-user config overlay layer `~/.aihr/configs/*.local.yaml` (Windows
  `%LOCALAPPDATA%\aihr\configs\`): machine truth now survives
  `pip install -U` and bundle replacements; it outranks the install-tree
  overlay, which keeps working unchanged.

## [0.6.8] - 2026-11-02

### Added
- `hr preflight`: control-baseline gate (non-empty responses, anchor
  scores inside calibrated bands, full-chain readback) with a fresh-pass
  marker required by `hr stage0`.
- Pricing-coverage gate: live sweeps refuse to start without per-model
  price entries (named refusal, exit 2).
- Per-request `max_tokens` caps for unbounded-thinking anchors.
- Turnkey bundles for linux/macos/windows plus code-gen interpreter
  packages published as release assets.

### Fixed
- macOS arm64 bundle build target migrated to the macos-15 runner image.

## [0.6.6] - 2026-10-14, [0.6.5] - 2026-10-13, [0.6.4] - 2026-10-11,
## [0.6.2] - 2026-10-10, [0.6.1] - 2026-10-09, [0.6.0] - 2026-10-08

Capability-benchmark and seat-matching iteration line. See the
[release notes](https://github.com/TachikomaGundam/AIHR/releases) for the
full per-version detail. (v0.6.3 was tagged but never published as a
release.)

## [0.5.1] - 2026-10-07, [0.5.0], and earlier (0.4.x)

Pre-0.6 history: see
[tags](https://github.com/TachikomaGundam/AIHR/tags) and release notes.
