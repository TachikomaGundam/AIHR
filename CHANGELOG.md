# Changelog

Notable changes per release. The authoritative notes for every version
(including full asset lists and checksums) live on the
[GitHub Releases page](https://github.com/TachikomaGundam/AIHR/releases);
this file summarizes the headline of each.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
