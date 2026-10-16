## What and why

<!-- One or two sentences. Link the phase in PROGRESS.md. -->

## Type

- [ ] Behaviour-preserving (refactor, tooling, docs): the golden replay and tests are unchanged
- [ ] Behaviour change (strategy rule, sizing, exits, orders): replay before and after is attached, config version bumped
- [ ] Safety-relevant (guard, capabilities, risk limits, order path): second reviewer required

## Checklist

- [ ] `scripts/dev.ps1 ci` passes locally (ruff, mypy, import-linter, tests with coverage)
- [ ] New behaviour has tests; new failure modes have a test that triggers them
- [ ] No secrets, tokens, `.env` or `data/` content in the diff
- [ ] `DRY_RUN` still defaults to true, and nothing but `DRY_RUN=false` can reach a broker order call, or the change is explained below
- [ ] `PROGRESS.md` and `CHANGELOG.md` updated

## Evidence

<!-- Replay numbers, screenshots of a session, test output. -->

## Risk and rollback

<!-- What could go wrong, and how to undo it (revert, previous tag). -->
