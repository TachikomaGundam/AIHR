# HR2 Seat Profiles — Phase 0a Summary

Generated: 2026-07-27T09:47:21.656328+00:00
DB: `/home/lab/.local/share/opencode/opencode.db`
Total sessions scanned: **161**
Since filter: (none)

## Storage discovered

- Path: `~/.local/share/opencode/opencode.db` (sqlite, ~1.2 GB)
- Related dirs: `storage/`, `repos/`, `snapshot/`, `tool-output/`, `log/`
- `session` table → `message` table → `part` table (data JSON).
- No `~/.opencode/` legacy dir present; no `projects/` subdir (older layout).

## Per-seat task counts

| seat | task_count | source | inferred |
|------|-----------:|--------|----------|
| sisyphus_junior | 82 | logs |  |
| librarian | 29 | inferred | ✓ |
| ultrabrain | 17 | inferred | ✓ |
| explore | 12 | inferred | ✓ |
| multimodal_looker | 8 | inferred | ✓ |
| oracle | 4 | inferred | ✓ |
| hephaestus | 2 | inferred | ✓ |
| prometheus | 2 | inferred | ✓ |
| metis | 1 | inferred | ✓ |
| momus | 1 | inferred | ✓ |
| deep | 0 | inferred | ✓ |
| quick | 0 | inferred | ✓ |
| writing | 0 | inferred | ✓ |
| artistry | 0 | inferred | ✓ |
| visual_engineering | 0 | inferred | ✓ |
| atlas | 0 | inferred | ✓ |
| unspecified_low | 0 | inferred | ✓ |
| unspecified_high | 0 | inferred | ✓ |

## Unmapped agents (reported, not profiled)

- `circuit-engineer`: 3 session(s)

## Top 3 tools per seat (log-derived seats only)

- **sisyphus_junior**: `bash` (0.58), `read` (0.11), `edit` (0.10)

## Inferred seats & flags

17 seat(s) below the 30-session threshold and marked `source="inferred"` with role-description defaults.

## Keyscan incidents

No secret-pattern matches detected in any output.
