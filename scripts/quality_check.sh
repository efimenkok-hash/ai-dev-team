#!/usr/bin/env bash
# Local entry-point for Step 10 quality gates: lint + tests + coverage.
# Mirrors what core.quality_gates.QualityGates does programmatically.
# Use this on a developer machine before pushing a feature branch.
#
# Exit codes:
#   0 — all checks passed
#   1 — at least one check failed

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

PYTHON="${PYTHON:-python3}"
MIN_COVERAGE="${MIN_COVERAGE:-80.0}"
COVERAGE_FILE="${COVERAGE_FILE:-${REPO_ROOT}/.coverage}"
export COVERAGE_FILE

echo "==> Step 10/A: ruff check"
"${PYTHON}" -m ruff check core tests

echo "==> Step 10/B: pytest"
"${PYTHON}" -m pytest tests/ -q -p no:cacheprovider

echo "==> Step 10/C: coverage (min ${MIN_COVERAGE}%)"
"${PYTHON}" -m coverage run --source=core -m pytest tests/ -q -p no:cacheprovider
"${PYTHON}" -m coverage report --precision=1 --fail-under="${MIN_COVERAGE}"

echo "==> All quality gates passed."
