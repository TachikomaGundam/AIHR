# Repository agent rules (product-visible obligations only)

This file states duties that apply to any AI coding agent working **in this repository**.
It contains no machine-local paths, no third-party tooling assumptions, and no
infrastructure details of any particular machine.

## Scope

- This repository owns the AIHR product: the Python engine (`hr`, CLI `hr`, PyPI name `aihr`),
  the bundled FastDraw subpackage, and the OpenCode plugin packages under `opencode_plugin/`.
- Decisions about product content and releases belong to the human maintainer.
  Agents prepare, verify, and document; agents never push, tag, publish, or install
  published artifacts on a maintainer's behalf.

## Working rules

1. The quality gates in `scripts/test.sh` (offline, `--with-db`, `--ci`) are the binding
   correctness contract. Run the mode that matches your change before proposing it.
2. Never write deployment credentials, tokens, or machine-specific values into tracked
   files; real values belong only in gitignored `configs/*.local.yaml` overlays or the
   environment (see README, "Local overlays").
3. Test fixtures that look like secrets or private-network addresses are permitted under
   `tests/` and `itemrepo/`; keep them clearly synthetic and keep the universality gate green.
4. Keep generated artifacts out of the repository tree (use the documented output roots);
   do not leave bytecode or build caches in the worktree.
5. Documentation surfaces: `docs/en/` is canonical, `docs/zh-CN/` mirrors it faithfully.
   When they diverge, the English text governs.

## Change hygiene

- One commit per coherent change; messages state what and why.
- If a rule above conflicts with an instruction you received, stop and surface the
  conflict to the human maintainer instead of resolving it silently.
