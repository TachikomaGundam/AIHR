#!/usr/bin/env bash
# scripts/check_universal.sh — universality gate for the hr engine (plan F2).
#
# Four checks; every failing check prints a message and exits non-zero.
#
#   (a) zero model-name literals in hr/ engine code (allowlisted comments,
#       docstrings, and provider-wire constants documented below)
#   (b) zero absolute /home/ paths in hr/ + tests/ + itemrepo/ (itemrepo is
#       benchmark task DATA, scrub-guarded by tests/items/data-integrity)
#   (c) hr.scheduler/taxonomy.py + hr.items/loader.py alive (import smoke)
#   (d) no secrets: known DB password absent from full git history + worktree
#
# Classification rules for (a) — each allowance is a judgment, documented:
#   * COMMENTS/DOCSTRINGS/HELP TEXT: module/class/function docstrings,
#     standalone string statements, and help= / key_help= / description=
#     argument values (argparse/typer/click) — documentation, not code.
#   * SECRET-FORMAT PATTERNS in hr/keyscan.py: regex literals passed to
#     re.compile — the file's purpose is detecting vendor API-key formats
#     (sk-kimi-* etc.), not enumerating fleet models.
#   * PROVIDER-WIRE LITERALS (exact-value allowlist below): the remaining
#     gateway base URL and provider-id constants in hr/adapters/openai_compat.py
#     (the deepseek openai-compat special-case) plus the keyscan pattern label
#     — adapter infrastructure, not model names. Nothing else may appear.
#
# Model knowledge lives OUTSIDE the engine: reference scores / findings in
# configs/knowledge.yaml, the dynamic fleet in the opencode provider config +
# configs/fleet.yaml (wire_overrides) + configs/deployable.yaml (extras), and
# calibration anchors in configs/seats.yaml (calibration_anchors). configs/
# is data, not engine code, so (a) scans hr/ only — no whole-file skips.
#
# The raw plan probe (grep -rniE ... | grep -v "test") is printed for the
# record; the gate is the residual after the documented exclusions above.
#
# Why (d) needs an opt-in needle, and why CI never relies on the default:
# the scan needle IS the credential, so it must never be stored in this repo
# (not even split across string literals) and this script carries no
# credential in any form. The needle is resolved at run time: from
# $HR_SECRET_NEEDLE, else from the live compose file given by
# $HR_COMPOSE_FILE (whose POSTGRES_PASSWORD / DB_PASS values are extracted).
# With neither set, a bare local run SKIPS the scan with a printed notice —
# ergonomics for throwaway boxes only. Pass --require-secret-scan (what CI
# does, wired to a repository secret) and an unresolvable needle becomes a
# hard FAIL: a CI run can never certify the gate by not configuring it.
set -u

REQUIRE_SECRET_SCAN=0
for arg in "$@"; do
    case "$arg" in
        --require-secret-scan) REQUIRE_SECRET_SCAN=1 ;;
        -h|--help)
            echo "usage: check_universal.sh [--require-secret-scan]"
            echo "  --require-secret-scan  fail (instead of skipping) when no"
            echo "                           needle resolves from HR_SECRET_NEEDLE"
            echo "                           or HR_COMPOSE_FILE; CI default"
            exit 0 ;;
        *)
            echo "unknown argument: $arg (see --help)" >&2
            exit 2 ;;
    esac
done

cd "$(dirname "$0")/.." || exit 1

MODEL_RE='qwen|deepseek|kimi|glm|minimax|gpt-'
FAIL=0

# ---------------------------------------------------------------------------
# (a) model-name literals in engine code
# ---------------------------------------------------------------------------
echo "== (a) model-name literals in hr/ engine code =="
raw_count=$(grep -rniE "$MODEL_RE" hr/ --include="*.py" | grep -v "test" | wc -l)
echo "raw probe (plan grep, tests filtered): $raw_count lines"

residual=$(python3 - <<'PY'
import ast
import re
import sys
from pathlib import Path

RE = re.compile(r"qwen|deepseek|kimi|glm|minimax|gpt-", re.IGNORECASE)

# Exact-value allowlist — engine-internal identifiers that are NOT model
# names: the keyscan pattern label. Anything else matching the regex in a
# code position is a residual. No model ids, no fleet lists, no calibration
# anchors, no gateway URLs: those are config data (configs/) and never appear
# in engine code.
ALLOWED = {
    # keyscan.py pattern label for the kimi gateway key format.
    "sk_kimi",
}

DOC_KWARGS = {"help", "key_help", "description", "doc"}


def docstring_ranges(tree):
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                out.append((body[0].lineno, body[0].end_lineno))
    return out


def classify(path):
    """Return (category_count, residuals) for one file."""
    src = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return ({"syntax-error": 1}, [(0, f"<unparseable: {exc}>")])
    # link parents (ast.walk does not provide them)
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child.parent = node
    dr = docstring_ranges(tree)
    counts = {"docstring": 0, "keyscan-pattern": 0, "doc-kwarg": 0, "allowed": 0}
    residuals = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if not RE.search(node.value):
            continue
        if any(a <= node.lineno <= b for a, b in dr):
            counts["docstring"] += 1
            continue
        parent = getattr(node, "parent", None)
        if isinstance(parent, ast.keyword) and parent.arg in DOC_KWARGS:
            counts["doc-kwarg"] += 1  # argparse/typer/click help text
            continue
        if path.name == "keyscan.py" and isinstance(parent, ast.Call):
            f = parent.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "re" and f.attr == "compile":
                counts["keyscan-pattern"] += 1  # secret-format detection regexes
                continue
        if node.value in ALLOWED:
            counts["allowed"] += 1
            continue
        residuals.append((node.lineno, node.value))
    return (counts, residuals)


total_counts = {"docstring": 0, "keyscan-pattern": 0, "doc-kwarg": 0, "allowed": 0}
residuals = []
for path in sorted(Path("hr").rglob("*.py")):
    if any(part in ("__pycache__", ".venv") for part in path.parts):
        continue
    (counts, res) = classify(path)
    for k, v in counts.items():
        total_counts[k] += v
    residuals.extend((str(path), ln, lit) for ln, lit in res)

print(f"excluded: {total_counts}")
if residuals:
    for path, ln, lit in residuals:
        print(f"RESIDUAL {path}:{ln}: {lit!r}")
    sys.exit(1)
sys.exit(0)
PY
)
rc=$?
if [ "$rc" -ne 0 ]; then
    echo "$residual"
    echo "FAIL (a): model-name literal(s) in engine code positions (see RESIDUAL lines)."
    FAIL=1
else
    echo "$residual"
    echo "PASS (a)"
fi

# ---------------------------------------------------------------------------
# (b) absolute /home/ paths in hr/ + tests/ + itemrepo/
# ---------------------------------------------------------------------------
echo "== (b) absolute /home/ paths =="
b_hits=$(grep -rnE --exclude-dir=__pycache__ --exclude-dir=.venv '/home/' hr/ tests/ itemrepo/ 2>/dev/null)
if [ -n "$b_hits" ]; then
    echo "$b_hits"
    echo "FAIL (b): absolute /home/ paths found above."
    FAIL=1
else
    echo "0 hits -> PASS (b)"
fi

# ---------------------------------------------------------------------------
# (c) scheduler/taxonomy.py + items/loader.py alive (import smoke)
# ---------------------------------------------------------------------------
echo "== (c) import smoke: hr.adapters / hr.scheduler / hr.items =="
if ! err=$(python3 -c "import hr.adapters, hr.scheduler, hr.items" 2>&1); then
    echo "FAIL (c): package import failed: $err"
    FAIL=1
elif ! err=$(python3 -c "import hr.scheduler.taxonomy, hr.items.loader" 2>&1); then
    echo "FAIL (c): submodule import failed: $err"
    FAIL=1
else
    echo "hr.adapters, hr.scheduler, hr.items, hr.scheduler.taxonomy, hr.items.loader import OK -> PASS (c)"
fi

# ---------------------------------------------------------------------------
# (d) no secrets: DB password absent from full git history + worktree
# ---------------------------------------------------------------------------
# The needle is the credential itself and is resolved at run time only:
# $HR_SECRET_NEEDLE, else POSTGRES_PASSWORD / DB_PASS from the live compose
# file named by $HR_COMPOSE_FILE. Never stored in this repo (see header).
echo "== (d) secret scan (known DB password, full git history) =="
resolve_needle() {
    if [ -n "${HR_SECRET_NEEDLE:-}" ]; then
        printf '%s' "$HR_SECRET_NEEDLE"
        return
    fi
    if [ -n "${HR_COMPOSE_FILE:-}" ]; then
        python3 - "$HR_COMPOSE_FILE" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.is_file():
    sys.exit(0)
values = []
for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
    m = re.match(r"^\s*(POSTGRES_PASSWORD|DB_PASS)\s*[:=]\s*(.+?)\s*$", line)
    if m:
        values.append(m.group(2).strip().strip('\'"'))
print(" ".join(values))
PY
    fi
}

SECRET=$(resolve_needle)
if [ -z "$SECRET" ] && [ "$REQUIRE_SECRET_SCAN" -eq 1 ]; then
    echo "FAIL (d): --require-secret-scan was passed but neither HR_SECRET_NEEDLE"
    echo "          nor HR_COMPOSE_FILE resolves a needle — the scan did not run."
    echo "          CI: set the repository secret HR_SECRET_NEEDLE (Settings →"
    echo "          Secrets and variables → Actions) and mirror it into the job"
    echo "          env. A gate that can be skipped by not configuring it is"
    echo "          not a gate."
    FAIL=1
elif [ -z "$SECRET" ]; then
    D_SKIPPED=1
    echo "SKIP (d): HR_SECRET_NEEDLE and HR_COMPOSE_FILE both unset. The scan"
    echo "          needle would be the credential itself, which is never stored"
    echo "          in this repo; export HR_SECRET_NEEDLE=<value> or"
    echo "          HR_COMPOSE_FILE=<compose-file> to enable the scan (see the"
    echo "          header comment for why this skips with exit code 0)."
else
    d_hits=$(
        for needle in $SECRET; do
            {
                git grep -n -F -- "$needle" HEAD 2>/dev/null
                git grep -n -F -- "$needle" $(git rev-list --all) 2>/dev/null
                grep -rnF --exclude-dir=.git --exclude-dir=.venv --exclude-dir=__pycache__ --exclude-dir=.pytest_cache "$needle" . 2>/dev/null
            } | sort -u
        done
    )
    revs=$(git rev-list --all | wc -l)
    if [ -n "$d_hits" ]; then
        echo "$d_hits"
        echo "FAIL (d): secret present in history/worktree ($revs revs scanned)."
        FAIL=1
    else
        echo "0 hits across $revs revs + worktree -> PASS (d)"
    fi
fi

# ---------------------------------------------------------------------------
# (e) uv.lock header revision pin - lock-revision change policy, three rules:
#     1. exactly one machine regenerates the lock at a time (single point);
#     2. every other seat is notified before the bump lands;
#     3. the bump ships in the SAME commit that updates this pin below.
#     Rationale (owner ruling 2026-10-23): the toolchain floor in pyproject
#     [tool.uv] guards the uv version; this pin guards the lock format itself —
#     an unannounced revision change silently splits the fleet.
# ---------------------------------------------------------------------------
echo "== (e) uv.lock revision pin =="
LOCK_REV=$(head -2 uv.lock 2>/dev/null | sed -n 's/^revision = //p')
if [ "${LOCK_REV:-}" = "3" ]; then
    echo "revision = 3 -> PASS (e)"
else
    echo "uv.lock revision is '${LOCK_REV:-<missing>}', this gate pins 3."
    echo "FAIL (e): lock revision changed without the coordinated bump (see policy above)."
    FAIL=1
fi

# ---------------------------------------------------------------------------
# (f) repo-root markdown: machine-local paths + machine-local deny-markers
#     Root *.md files (AGENTS.md and friends) sit outside section (b)'s scan
#     surface. The marker list is a gitignored overlay (same pattern as
#     configs/*.local.yaml): scripts/universal-denylist.local.txt, one marker
#     per line, never committed - the gate itself ships zero institution data.
#     Known limit (named 2026-10-23): this is a lexical gate; prose rewording
#     of a marker passes it. Per-change human review of AGENTS.md remains the
#     real control.
# ---------------------------------------------------------------------------
echo "== (f) repo-root markdown machine paths/markers =="
f_hits=$(grep -nE '/home/|/root/|[A-Za-z]:\\Users\\' *.md 2>/dev/null)
if [ -f scripts/universal-denylist.local.txt ]; then
    while IFS= read -r marker; do
        [ -n "$marker" ] || continue
        marker_hits=$(grep -nF -- "$marker" *.md 2>/dev/null)
        if [ -n "$marker_hits" ]; then
            f_hits="${f_hits}
${marker_hits}"
        fi
    done < scripts/universal-denylist.local.txt
fi
if [ -n "$f_hits" ]; then
    echo "$f_hits"
    echo "FAIL (f): repo-root markdown references machine-local paths or deny-markers."
    FAIL=1
else
    echo "0 hits -> PASS (f)"
fi

# ---------------------------------------------------------------------------
echo
if [ "$FAIL" -ne 0 ]; then
    echo "UNIVERSALITY GATE: FAIL"
    exit 1
fi
if [ "${D_SKIPPED:-0}" -eq 1 ]; then
    echo "UNIVERSALITY GATE: PASS (a) (b) (c) (e) (f) (d skipped — see notice above)"
else
    echo "UNIVERSALITY GATE: PASS (a) (b) (c) (d) (e) (f)"
fi
exit 0