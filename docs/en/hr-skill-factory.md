# HR Skill Factory (`hr skill`)

**English** | [简体中文](../zh-CN/hr-skill-factory.md)

The skill factory creates, validates, installs, and audits the OpenCode
skills this repository ships. One rule drives the whole command family:
**the authoritative copy of every skill is the file under
`opencode-config/skills/` in this repository. Every other copy on any
machine is a derived copy.** `hr skill doctor` measures the drift between
the authority and its copies; nothing else is ever treated as a source.

## Why it exists (the defect it kills)

The engine advertises a skill to the model only when the skill's
frontmatter carries a non-empty `description`
([official rule](https://opencode.ai/docs/skills)). The shipped
`hr-workflow` skill long lived without frontmatter: it installed fine,
loaded fine by hand, and was invisible to the model — and no tool noticed,
because installation was a hand-copied `cp` in the README and no validator
existed. Three divergent copies of that one skill (repo, `~/.config`, a
second agent tool) accumulated unnoticed. `hr skill` closes both holes:
a fail-closed validator, and a single authority with drift measurement.

## Layout

```text
opencode-config/
  skills/
    <skill-id>/
      SKILL.md        # the authority; frontmatter: name + description
  agents/
    hr.md             # the hr sub-agent (installed by copy)
```

The V2 directory layout (`<skill-id>/SKILL.md`) is the target for both
OpenCode and Codex, so moving a skill between tools is a pure file copy
with no rewriting. Legacy flat `<skill-id>.md` files are still READ
(validate / doctor handle them, `install` normalizes them to the directory
form); they are never written.

The skill ID is path-derived (`skills/foo/SKILL.md` → ID `foo`); the
frontmatter `name` is a label and must equal that ID.

## Commands

### `hr skill validate [PATH | --all]`

The fail-closed frontmatter gate. Any defect → exit 1, naming the file and
the missing field. Never "warned and passed". Checks:

| Check | Rule |
|---|---|
| frontmatter block | present, terminated, valid YAML mapping |
| `name` | required; `^[a-z0-9]+(-[a-z0-9]+)*$`, 1–64 chars, **equal to the path-derived ID** |
| `description` | required, non-empty — without it the engine never advertises the skill |
| `disable-model-invocation` | optional; boolean when present |

```bash
hr skill validate opencode-config/skills/hr-workflow   # one skill (dir or SKILL.md)
hr skill validate --all                                # the whole authority
```

### `hr skill list [--json]`

Lists the authoritative skills and reports, per skill, whether the local
engine advertises it. The engine column is produced by

```bash
opencode api get "/api/skill?location%5Bdirectory%5D=<skills-dir>"
```

with three measured v2.2.7 quirks handled (see “Engine probe” below). When
the engine is unreachable, the local facts are still listed and the engine
column says `UNREACHABLE` — it is never silently blank. Exit code is 0
(listing has no failure semantics); `--json` emits strict parseable JSON.

### `hr skill new NAME [--dir DIR]`

Scaffolds `<root>/NAME/SKILL.md` (default root: the authority). The
skeleton ships valid frontmatter, so it passes `validate` the moment it is
born. Invalid or duplicate IDs are refused without writing anything.

### `hr skill install NAME [--dest DIR] [--dry-run] [--force]`

Copies one authoritative skill to the destination skills directory
(default: the opencode config dir's `skills/`, i.e.
`~/.config/opencode/skills`).

Order of operations, all of them load-bearing:

1. **Validate first.** An invalid skill is refused with its defect list;
   installing a skill the engine would not advertise is pointless.
2. **Print hashes.** Source `sha256` up front; destination state after.
3. **Idempotent.** Re-running on an identical destination writes nothing
   and exits 0.
4. **Divergence fails.** A destination that differs from the authority
   fails; `--force` replaces it (and normalizes a legacy flat copy out of
   the destination namespace).
5. **`--dry-run` never writes**, and reports the verdict the real run
   would reach (including a would-be failure).

### `hr skill doctor [--copy DIR ...] [--json]`

Hash table across the authority and every copy root (default install dir +
`HR_SKILL_COPIES` + explicit `--copy`). Per skill: `in sync`, `not
installed`, `DRIFT`, or `ORPHAN` (a copy whose ID the authority does not
define — loudly reported, but a third-party skill beside ours is not our
drift). **Any drift → exit 1.** Comparison is whole-directory
(per-file SHA-256), so a mutated support file counts as drift too.

## Engine probe (measured on opencode v2.2.7)

Three facts the probe code handles; each was measured, each fails loudly
if it stops holding:

1. The response's outer key is `data`, not `skills`.
2. The query parameter must stay URL-encoded: `location%5Bdirectory%5D=`.
3. The first query against a never-opened directory legitimately returns
   an empty table. The probe retries (3 attempts, 1 s apart); an empty
   answer after the full budget is reported as "nothing", never as
   "unreachable".

## Environment seams

| Variable | Effect |
|---|---|
| `HR_SKILL_SOURCE` | Override the authoritative root (tests, experiments) |
| `HR_SKILL_COPIES` | `os.pathsep`-separated extra copy roots for `doctor` |
| `HR_HOME` | Standard resource resolution; the authority is `<HR_HOME>/opencode-config/skills` |

## Lifecycle symmetry

`hr setup --uninstall` reverses the skill installation of BOTH layouts:
the `<skills>/hr-workflow/` directory (installed by `hr skill install`)
and the pre-migration flat `skills/hr-workflow.md` residue, plus the
`agents/hr.md` copy.

## Machine surface for scripts

`hr skill list --json` and `hr skill doctor --json` write strict JSON to
stdout (per-file hash maps included in the doctor report). Human tables
use a 12-hex combined digest per skill directory.
