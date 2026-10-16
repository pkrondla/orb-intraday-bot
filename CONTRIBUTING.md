# Working on the ORB Intraday Bot

This bot will one day handle real money, so the process is part of the safety system. Read
`ARCHITECTURE.md` (design and layer rules) and `PROGRESS.md` (what is done, what is next) first.

## Setup

```
powershell -ExecutionPolicy Bypass -File scripts\dev.ps1 setup    # clean venv from the lock file
powershell -ExecutionPolicy Bypass -File scripts\dev.ps1 ci       # every gate CI runs
```

Credentials go in `.env` (git-ignored, copy `.env.example`). Use a **data-only** Groww key. Never
put a key with trading permission on a development machine.

## Branches, commits, pull requests

- `main` is always releasable and is never committed to directly. Work happens on
  `type/short-name` branches (`feat/`, `fix/`, `refactor/`, `chore/`, `docs/`, `test/`) and is
  merged by pull request after CI is green.
- Commit messages: `type(scope): imperative summary`, then a body that says *why*. One logical
  change per commit.
- A pull request uses the template. A change is **safety-relevant** if it touches the paper-only
  guard, broker capabilities, order code, risk limits or the kill switch. Those need a second
  reviewer, even while the project has one developer (wait a day and review it yourself, with the
  checklist).
- Repository settings to apply on GitHub (they cannot be set from the code): protect `main`,
  require a pull request, require the `Quality gates` and `Dependency audit` checks, require
  linear history, block force pushes, and turn on secret scanning with push protection.

## Definition of done

1. `scripts\dev.ps1 ci` is green: ruff, mypy strict, import-linter, tests, coverage at least 92% (it was 89% at v0.9.0 and is raised only, never lowered), and the golden replay unchanged.
2. New behaviour has tests, including the failure case. A bug fix starts with a failing test.
3. Behaviour changes carry a replay before and after (`orb-bot replay --last 60`, or the golden
   replay once Phase 12 lands) and a bump of the strategy version.
4. No secrets, `.env` or `data/` in the diff (the pre-commit hook and CI both look).
5. `PROGRESS.md` and `CHANGELOG.md` are updated; docs say what the code now does.

## Architecture rules (enforced by `lint-imports`)

- Strategy code (`core`, `indicators`, `patterns`, `risk`, `engine`) does no I/O and imports no
  broker, database, runtime or CLI code.
- Only `adapters/groww` imports the Groww SDK. Adapters never import strategy code.
- Layers only import downwards (see `[tool.importlinter]` in `pyproject.toml`). There are no
  exceptions; do not add `ignore_imports`. Persistence is reached through ports (for example
  `paper/ports.py`), implemented in `storage`.

## Dependencies

Runtime and dev dependencies are pinned in `requirements.lock` and `requirements-dev.lock`.
Change `pyproject.toml`, run `scripts\dev.ps1 lock`, review the diff, run `ci`, commit all three
files together. Dependabot proposes updates weekly; each one goes through the same gates.

## Pre-commit (optional but recommended)

```
pre-commit install --hook-type pre-commit --hook-type pre-push
```

Blocks direct commits to `main`, secrets and runtime data, and runs ruff, mypy and the import
rules on commit and the tests on push.

## Releases and the forward test

- Versions follow `MAJOR.MINOR.PATCH`; the version lives in `pyproject.toml` and
  `src/orb_intraday_bot/__init__.py` (a test keeps them equal).
- A release is an annotated tag `vX.Y.Z` on `main` with a `CHANGELOG.md` entry:
  `git tag -a v0.9.0 -m "..."`.
- **A forward test runs on a tag and nothing else.** Check out the tag, install from the lock
  file, run. Behaviour changes during a forward test restart the test; behaviour-preserving
  refactors must reproduce the golden replay exactly.
- Roll back by checking out the previous tag. Positions and signals in the database are
  idempotent, so restarting on an older version does not duplicate trades.
