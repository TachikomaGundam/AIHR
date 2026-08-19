# UNIFICATION — DONE (2026-08-19)

Executed in full: hr-unification plan (29 tasks, 2026-08-19).
Final audit: private notes kept outside the tree at `~/.local/hr` (AUDIT.en.md/AUDIT.zh-CN.md in the AIHR workspace metadata).

---

# HR Agent Unification Plan

**Status:** Backlog (user wants changes to accumulate before applying)  
**User Decision:** Full merge into single hr package  
**Date:** 2026-08-15  

---

## Executive Summary

Merge `hr` (v1 live benchmarking) and `hr2` (v2 behavioral health) into a unified `hr` package with:
- Single install: `pip install -e /home/lab/hr`
- Single CLI: `hr benchmark`, `hr health`, `hr verdict`, etc.
- Unified adapters (adopt hr2's OpenAI/Anthropic compat layer)
- Combined scoring: capability benchmarks + behavioral health gates
- Preserved data: keep both hr_* and hr2.* tables long-term

---

## Current State Analysis

### hr (v1) — Live API Benchmarking
**Location:** `/home/lab/workspace/harness/hr/hr/`  
**Purpose:** Run code_gen, reasoning, instruction_follow, tool_use, speed, vision, long_context benchmarks via live API calls  
**Tables:** `hr_models`, `hr_benchmarks`, `hr_reference`, `hr_research`, `hr_assignments`, `hr_reports`  
**Strengths:**
- Comprehensive capability scoring with reference benchmarks
- Research adjustment (strengths/weaknesses from external sources)
- Wiki.js publishing
- Role assignment for 18 seats

**Weaknesses:**
- No behavioral health measurement
- Adapters less mature (Anthropic-only)
- Cannot analyze existing responses (only live calls)

### hr2 (v2) — Behavioral Health Measurement
**Location:** `/home/lab/workspace/harness/hr/hr2/`  
**Purpose:** Analyze existing API responses for behavioral metrics (loop_mean, truncation_rate, tok/pt, consistency, completion)  
**Tables:** `hr2.sweep`, `hr2.run`, `hr2.measurement`, `hr2.battery`, `hr2.model`, `hr2.seat`, `hr2.separation`, `hr2.infra_incident`  
**Strengths:**
- Behavioral health metrics from existing responses (zero API cost)
- Mature adapter system (OpenAI + Anthropic compat)
- Separation matrix for model discrimination
- Health gates (strict/moderate/lenient) for seat assignment
- Stage 0/1 sweep design for efficiency

**Weaknesses:**
- No capability benchmarking (code_gen, reasoning, etc.)
- No external research integration
- Separate install, separate CLI

---

## Unification Architecture

### Package Structure
```
hr/
├── __init__.py
├── cli.py                    # Unified CLI entry point
├── adapters/                 # From hr2 (more mature)
│   ├── __init__.py
│   ├── base.py
│   ├── openai_compat.py      # DeepSeek official, kimi-for-coding, etc.
│   └── anthropic_compat.py   # Bailian, kimi (bailian), etc.
├── benchmark/                # From hr (v1 live benchmarking)
│   ├── __init__.py
│   ├── code_gen.py
│   ├── reasoning.py
│   ├── instruction_follow.py
│   ├── tool_use.py
│   ├── speed.py
│   ├── vision.py
│   ├── long_context.py
│   └── runner.py
├── health/                   # From hr2 (behavioral health)
│   ├── __init__.py
│   ├── health.py             # Core health metrics
│   ├── loop.py               # Repetition detection
│   ├── stage1.py             # Behavioral sweep runner
│   └── battery.py            # Item battery management
├── verdict/                  # Unified scoring + assignment (merge both)
│   ├── __init__.py
│   ├── scoring.py            # Capability + health combined
│   ├── seats.py              # Role assignment
│   ├── gates.py              # Health gates (from hr2)
│   └── reference.py          # External benchmarks (from hr)
├── db/                       # Unified data access layer
│   ├── __init__.py
│   ├── connection.py
│   ├── models.py             # Model registry
│   ├── benchmarks.py         # Live benchmark results
│   └── health.py             # Behavioral health results
├── wiki/                     # Wiki publishing (from hr)
│   ├── __init__.py
│   └── publisher.py
├── research/                 # External research (from hr)
│   ├── __init__.py
│   └── research.py
└── config.py                 # Unified configuration
```

### Unified CLI Design
```bash
# Model discovery
hr discover                    # Register models from all providers
hr status                      # Show registered models + current assignments

# Live benchmarking (from hr v1)
hr benchmark --model X         # Run all 7 capability benchmarks
hr benchmark --model X --category reasoning  # Run specific category

# Behavioral health (from hr2)
hr health --sweep <id>         # Run behavioral health analysis on existing responses
hr health --model X --dry-run  # Show what would be measured (no API calls)

# Verdict (combined scoring)
hr verdict --latest            # Combined capability + health scoring + seat assignment
hr verdict --sweep <id>        # Verdict for specific sweep
hr verdict --include-retired   # Include retired models

# Reporting
hr report                      # Full evaluation report (capability + health)
hr report --model X            # Report for specific model
hr publish                     # Publish to Wiki.js

# Recommendations
hr recommend "task description"  # Get model ranking for a task
```

### Unified Scoring Formula

**Phase 1: Parallel scoring (initial implementation)**
- Capability scores from v1 (code_gen, reasoning, etc.)
- Health metrics from hr2 (loop_mean, truncation_rate, etc.)
- Display both side-by-side
- Seat assignment uses health gates (strict/moderate/lenient) to filter candidates

**Phase 2: Integrated scoring (future)**
```
capability_score = min(live_benchmark, effective_reference)  # from v1
health_gate_pass = (loop_mean ≤ threshold) AND (truncation ≤ threshold)
eligible_for_seat = health_gate_pass AND (capability_score ≥ min_score)
seat_assignment = rank(eligible_for_seat, by=capability_score)
```

### Adapter Unification

**Adopt hr2's adapter system as the unified layer:**
- `OpenAICompatAdapter`: DeepSeek official, kimi-for-coding, kimi-for-coding-highspeed
- `AnthropicCompatAdapter`: Bailian (all models), kimi (bailian variants)
- hr's benchmark runner rewritten to use unified adapters

**Benefits:**
- Single adapter codebase to maintain
- Supports both OpenAI and Anthropic wire formats
- Handles thinking/reasoning tokens correctly
- Rate limiting and retry logic in one place

### Data Model Strategy

**Short-term (maintain both schemas):**
- Keep `hr_*` tables for live benchmark results
- Keep `hr2.*` tables for behavioral health results
- Add unified data access layer that joins both

**Long-term (optional migration):**
- Migrate to unified schema:
  - `hr_models` (from hr_models + hr2.model)
  - `hr_benchmarks` (live API results)
  - `hr_health` (behavioral metrics)
  - `hr_assignments` (role assignments)
- Provide migration script for existing data

**Migration path:**
1. Add unified views: `hr_models_unified` = UNION of hr_models and hr2.model
2. Update CLI to use views instead of direct table access
3. Gradually migrate code to use unified schema
4. Deprecate old tables once migration complete

---

## Implementation Phases

### Phase 1: Structural Merge (1-2 days)
**Goal:** Move hr2 code into hr package, preserve functionality

1. Move `hr2/adapters/` → `hr/adapters/`
2. Move `hr2/health.py`, `hr2/loop.py` → `hr/health/`
3. Move `hr2/stage1.py`, `hr2/battery.py` → `hr/health/`
4. Update imports throughout
5. Test that both `hr benchmark` and `hr health` still work independently

**Validation:** Run both v1 benchmark and v2 health on a model, verify no regressions

### Phase 2: CLI Unification (1 day)
**Goal:** Single `hr` CLI with subcommands

1. Merge `hr/cli.py` and `hr2/cli.py` into unified CLI
2. Add subcommands: `hr benchmark`, `hr health`, `hr verdict`, `hr report`, `hr publish`
3. Update hr-workflow skill to call unified CLI
4. Deprecate `hr` (v1) and `hr2` standalone CLIs

**Validation:** All existing workflows work via unified CLI

### Phase 3: Adapter Unification (1-2 days)
**Goal:** hr benchmark uses hr2 adapters

1. Rewrite hr's benchmark runner to use `hr/adapters/`
2. Test all providers (bailian, deepseek official, kimi)
3. Remove hr's legacy adapter code
4. Update hr-workflow skill to use unified adapters

**Validation:** Run `hr benchmark` on all 18 models, verify results match previous runs

### Phase 4: Scoring Integration (2-3 days)
**Goal:** Combined capability + health scoring

1. Add health gate checks to seat assignment logic
2. Implement unified verdict: capability_score × health_gate_pass
3. Update `hr verdict` to show both capability and health metrics
4. Update Wiki.js publishing to include health data

**Validation:** Run `hr verdict`, verify seat assignments reflect health gates

### Phase 5: Documentation & Cleanup (1 day)
**Goal:** Update all docs, remove deprecated code

1. Update hr-workflow skill with unified CLI commands
2. Update README.md with new architecture
3. Remove deprecated hr and hr2 standalone packages
4. Update any external references (scripts, automation)

**Validation:** End-to-end workflow from model discovery to Wiki publishing works

---

## Migration Checklist

- [ ] Phase 1: Structural merge complete, imports updated, tests pass
- [ ] Phase 2: Unified CLI working, skill updated
- [ ] Phase 3: Adapter unification complete, all providers tested
- [ ] Phase 4: Scoring integration done, gates enforced
- [ ] Phase 5: Documentation updated, deprecated code removed
- [ ] Data: Existing hr_* and hr2.* tables preserved (no data loss)
- [ ] Wiki: Publishing works with unified data (capability + health)
- [ ] Tests: Full benchmark run on all 18 models succeeds
- [ ] User validation: User runs `hr verdict` and confirms results make sense

---

## Risk Mitigation

### Data Preservation
- **Risk:** Merging schemas loses data
- **Mitigation:** Keep both schemas initially, add unified views, migrate gradually with backup

### Backward Compatibility
- **Risk:** Existing scripts/automation break
- **Mitigation:** Keep old CLI as deprecated aliases for 1 month, then remove

### Adapter Regressions
- **Risk:** Unified adapters have bugs in providers not used by hr2
- **Mitigation:** Test all 18 models before deploying, keep old adapters as fallback during transition

### Scoring Inconsistency
- **Risk:** Combined scoring gives unexpected results
- **Mitigation:** Phase 1 keeps parallel scoring (display both), Phase 2 integrates gradually with validation

---

## Success Criteria

1. **User calls:** `hr benchmark`, `hr health`, `hr verdict` — never needs to know about hr vs hr2
2. **Single install:** `pip install -e /home/lab/hr` works, no separate hr2 package
3. **Data preserved:** All historical benchmark and health data accessible
4. **Gates enforced:** Seat assignments respect health gates (loop_mean, truncation)
5. **Wiki publishing:** Model pages show both capability scores and health metrics
6. **Performance:** Full sweep (18 models × 7 benchmarks) completes in <2 hours

---

## Open Questions

1. **Schema migration:** Should we fully migrate to unified schema, or keep both long-term with views?
   - **Recommendation:** Start with views, migrate if performance/complexity warrants

2. **Config unification:** hr uses `hr.toml`, hr2 uses `opencode.jsonc` + `auth.json`
   - **Recommendation:** Keep `opencode.jsonc` for provider config (used by both), `hr.toml` for hr-specific settings

3. **Item banks:** hr has benchmark items inline, hr2 has item batteries
   - **Recommendation:** Move hr's benchmark items into hr2's battery format for consistency

4. **Separation matrix:** hr2's stage 0 separation is sophisticated, hr doesn't have it
   - **Recommendation:** Adopt hr2's separation as the unified approach

---

## Appendix: File Inventory

### hr (v1) files to migrate:
- `/home/lab/workspace/harness/hr/hr/hr/benchmark.py` → `hr/benchmark/runner.py`
- `/home/lab/workspace/harness/hr/hr/hr/config.py` → `hr/config.py`
- `/home/lab/workspace/harness/hr/hr/hr/registry.py` → merge into `hr/adapters/`
- `/home/lab/workspace/harness/hr/hr/hr/reference.py` → `hr/verdict/reference.py`
- `/home/lab/workspace/harness/hr/hr/hr/research.py` → `hr/research/research.py`
- `/home/lab/workspace/harness/hr/hr/hr/wiki.py` → `hr/wiki/publisher.py`

### hr2 (v2) files to migrate:
- `/home/lab/workspace/harness/hr/hr2/hr2/adapters/` → `hr/adapters/`
- `/home/lab/workspace/harness/hr/hr2/hr2/health.py` → `hr/health/health.py`
- `/home/lab/workspace/harness/hr/hr2/hr2/loop.py` → `hr/health/loop.py`
- `/home/lab/workspace/harness/hr/hr2/hr2/stage1.py` → `hr/health/stage1.py`
- `/home/lab/workspace/harness/hr/hr2/hr2/battery.py` → `hr/health/battery.py`
- `/home/lab/workspace/harness/hr/hr2/hr2/verdict.py` → `hr/verdict/`
- `/home/lab/workspace/harness/hr/hr2/hr2/separation.py` → `hr/verdict/separation.py`

### Files to retire (not migrated):
- `/home/lab/workspace/harness/hr/hr2/hr2/cli.py` → replaced by unified `hr/cli.py`
- `/home/lab/workspace/harness/hr/hr/hr/adapter.py` → replaced by hr2 adapters

---

## Next Steps

1. **Wait for current sweeps to complete** (~4 hours)
2. **Review A/B/C comparison** from sweep results
3. **Begin Phase 1** when user gives go-ahead
4. **Test thoroughly** before moving to Phase 2
5. **Update hr-workflow skill** after each phase
6. **Document decisions** in this file for future reference
