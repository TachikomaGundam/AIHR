# AIHR turnkey bundle build guide

Owners: `scripts/build_bundle.py` (orchestrator) + `scripts/bundle_core.py`
(pure, offline-testable logic) + `scripts/bundle_selftest.py` (vendorless gate).
Installers: `scripts/install.sh` (POSIX) / `scripts/install.ps1` (Windows).
CI: `.github/workflows/release.yml` (push tag `v*` → 4 platform bundles →
draft release + turnkey smokes). `ci.yml` stays standalone and untouched.

## Bundle layout `D`

```
D/                     ~/.aihr (posix) | %LOCALAPPDATA%\aihr (windows)
  app/                 PyInstaller onedir: hr(.exe) + _internal/
  pg/                  vendored postgres: bin/{initdb,pg_ctl,psql,postgres} lib/ share/
  share/aihr/          hr.toml.example + configs/*.yaml (never docker/, never *.local.yaml)
  data/                created at runtime by `hr` — installer/reinstall NEVER touches it
  bin/ db.env receipt.json   created at runtime by `hr install-post`
```

## Building

```sh
# offline logic gate — no network, no PyInstaller, no root
uv run --no-project python scripts/build_bundle.py --self-test

# real build for one target (needs network: PyPI + github.com releases for pg)
uv sync
uv run --with pyinstaller python scripts/build_bundle.py \
    --os linux|windows|macos --arch x86_64|aarch64 \
    --version 0.4.0 --out dist

# core-only re-archive without freezing (reuses/stubs app/, still needs staged pg/)
... --skip-pyinstaller [--skip-pg]

# inspect a finished artifact
python scripts/build_bundle.py --verify --archive dist/aihr-0.4.0-linux-x86_64.tar.gz
python scripts/build_bundle.py --list   --archive dist/aihr-0.4.0-linux-x86_64.tar.gz
```

Output: `dist/aihr-<V>-<os>-<arch>.tar.gz` (posix, exec bits preserved) or
`.zip` (windows), each with a `.sha256` sidecar (sha256sum format).
Vendored postgres archives cache under `~/.cache/aihr-build/pg` (override
`--pg-cache DIR`); the cache is only reused after the pinned sha256 matches.

## Pin refresh (`release/pg-pin.json`)

Pins reference **theseus-rs/postgresql-binaries** GitHub releases:
`{source_repo, release_tag, pg_version, targets.<os>-<arch>.{triple, asset, kind, sha256}}`.

1. Pick a release tag from <https://github.com/theseus-rs/postgresql-binaries/releases>
   (must be a full `MAJOR.MINOR.PATCH`; the asset must start
   `postgresql-<pg_version>-<triple>`).
2. For every target fetch the official sidecar and read the digest:

   ```sh
   curl -fsSL -H 'Accept: application/octet-stream' \
     "https://api.github.com/repos/theseus-rs/postgresql-binaries/releases/assets/<ASSET_ID>" \
     | head -1   # → "<sha256>  postgresql-…tar.gz"
   ```

   (or the `.sha256` under the release's expanded assets).
3. Update `pg_version`, `release_tag`, each `asset`/`sha256`; keep target keys
   exactly `linux-x86_64 windows-x86_64 macos-aarch64 macos-x86_64`.
4. Gate: `python scripts/build_bundle.py --self-test` (the committed pin file
   is validated there) + rebuild one bundle so the sha gate actually downloads.

## Pin refresh (`release/libs-pin.json`, linux only)

Ubuntu 26.04 ships libxml2 SONAME `.16` only while the vendored server still
DT_NEEDEDs `libxml2.so.2`; the bundle therefore re-distributes the noble
`libxml2.so.2` + `liblzma.so.5` closure into `pg/lib/` (loaded only by
bundled processes via a scoped `LD_LIBRARY_PATH`; NOTICE carries licenses).

1. Pick the `.deb` from `http://archive.ubuntu.com/ubuntu/pool/main/...`
   (security-pool rebuilds are expected; pin the exact filename).
2. `sha256sum lib.deb` → update `url`/`sha256`; keep `member` paths relative
   to the deb's `data.tar.*` and `symlink` equal to the file's SONAME.
3. Gate: `--self-test` validates the committed pin, and `--verify` on a fresh
   linux build asserts `pg/lib/libxml2.so.2`/`liblzma.so.5`/`NOTICE` members.

## Installing / turnkey acceptance

```sh
sh scripts/install.sh [--version V] [--bundle FILE] [--port N] [--reinstall] [--no-run-installer]
pwsh scripts/install.ps1 -Version V [-Bundle FILE] [-Port N] [-Reinstall] [-NoRunInstaller]
```

Both: detect platform, download `aihr-V-os-arch.*` + verify `.sha256`, refuse
root/elevation, refuse an existing `D` unless `--reinstall` (which replaces
`app/ pg/ share/` and never `data/`), then run `hr install-post` and a
best-effort `hr db-up`.

### Local acceptance recipe (builder box → target box)

```sh
# on the builder (has network + uv)
uv run --with pyinstaller python scripts/build_bundle.py \
    --os linux --arch x86_64 --version "$V" --out dist

# ship BOTH the bundle and its sidecar
# TESTBED = user@host of the designated bare-ubuntu box (address+creds live ONLY in the machine-owner ops layer, by design not in any repo)
scp dist/aihr-$V-linux-x86_64.tar.gz* "$TESTBED":/tmp/

# on the target machine (user account, NOT root)
ssh "$TESTBED"
sh /tmp/install.sh --bundle /tmp/aihr-$V-linux-x86_64.tar.gz
ls ~/.aihr/{app/pg,pg/bin,share/aihr} ~/.aihr/receipt.json
~/.aihr/app/hr db-status | grep -i backend

# re-run path: refresh without losing the database
touch ~/.aihr/data/pgdata/CANARY_MARKER   # (data/ pre-created by install-post run)
sh /tmp/install.sh --bundle /tmp/aihr-$V-linux-x86_64.tar.gz --reinstall
find ~/.aihr/data -name CANARY_MARKER   # must still exist

# uninstall audit: must leave $HOME/.aihr gone, per receipt manifest
hr self-uninstall --yes
test ! -d ~/.aihr && echo CLEAN
```

## CI notes

- `release.yml` builds ubuntu-24.04 / windows-2025 / macos-14 (+ macos-13
  best-effort), caches `~/.cache/aihr-build/pg` keyed on the pin hash, uploads
  everything to a **draft** release via `gh` (promote manually after audit).
- Turnkey smokes install the CI artifact with the real installer scripts and
  assert: `D` layout, `hr --help`, `receipt.json`, `db-status` backend line,
  windows adds an embedded-pg `SELECT 1`; then `self-uninstall --yes` must
  remove `D` entirely.
- `install.ps1` is first executed in CI (no local pwsh here); its
  `-AllowElevated` switch exists because GH-hosted Windows runners carry an
  admin token — human users are still refused by the elevation guard.
