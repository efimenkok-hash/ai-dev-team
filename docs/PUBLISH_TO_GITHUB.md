# Publishing AI Dev Team to GitHub

These steps are run on **your** Mac, not in the sandbox. AI Dev Team is ready
for publication: 411 unit tests, 93.1% coverage, 0 ruff violations, MIT license.

## Pre-flight on your machine

```bash
cd /Users/efimenko_k/ai-dev-team
source .venv/bin/activate

# 1. Confirm everything is green locally.
bash scripts/quality_check.sh

# 2. Sanity-check that .env is gitignored before adding it by accident.
grep -E '^\.env$' .gitignore && echo "OK: .env is ignored"
```

## Initialise the repository

```bash
cd /Users/efimenko_k/ai-dev-team

git init -b main
git config user.name  "Кирилл Ефименко"
git config user.email "efimenkokirill1991@gmail.com"

git add README.md LICENSE .env.example .gitignore \
        pyproject.toml pytest.ini requirements.txt requirements-dev.txt \
        main.py core/ tests/ scripts/ docs/ .github/

# Verify .env is NOT staged.
git status | grep -E '\.env(\s|$)' && echo "WARNING: .env is staged!" \
                                  || echo "OK: .env not staged"

git commit -m "Initial commit: AI Dev Team v4 ULTRA — 411 tests, 93.1% coverage"
```

## Create the GitHub repository

Two options. Pick one.

### Option A — gh CLI (recommended)

```bash
# Private repo, push the existing main branch.
gh repo create ai-dev-team --private --source=. --remote=origin --push
```

If you do not have `gh` installed: `brew install gh` then `gh auth login`.

### Option B — UI + manual remote

1. Open <https://github.com/new>, create `ai-dev-team` (private), do **not** initialise with README/license/.gitignore (we already have them).
2. Then locally:

```bash
git remote add origin git@github.com:efimenkok-hash/ai-dev-team.git
git push -u origin main
```

## After the first push

The CI workflow at `.github/workflows/ci.yml` runs ruff + pytest + coverage on
Python 3.10 / 3.11 / 3.12. After the first push you should see a green check
mark on the repo within ~2 minutes.

## When you are ready to integrate AI Dev Team into hedgekeeper-v2

This is the step we deliberately deferred. When all features are in place, the
plan is:

1. Build a `ProjectAdapter` that points at your `hedgekeeper-v2` checkout:
   - `language="python"`
   - `forbidden_paths=...` (anything you do not want agents to touch)
   - `forbidden_tokens=...` (any project-specific secrets / sentinel strings)
   - `commands={ "test": (...), "lint": (...) }` from your existing scripts
2. Run `python main.py "your task"` from inside the adapter context.
3. Apply the resulting patches via `core.git_integration` on a feature branch.
4. Review and merge through GitHub UI.

This integration step is **not** done now and should not be done until every
remaining ULTRA spec step (12, 14, 15) is complete.
