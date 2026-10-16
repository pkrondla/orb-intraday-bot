# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/). Versions follow
[Semantic Versioning](https://semver.org/). The strategy rules are versioned with the code: a
change to a rule, a stop, a target or sizing is at least a MINOR release.

## [Unreleased]

## [0.9.0] - 2026-10-16

First tagged version: the paper-trading bot, ready for the forward test. **Paper only; no order
code exists.**

### Added
- Data layer and indicators: Groww REST adapter (read-only whitelist, TOTP login, token cache),
  futures contract selection, opening range, Traditional pivots, swings, zones.
- Setups: ORB, traps, Case 1/2 follow-ups, additional whipsaws, consolidation breakout,
  2LL/3LL/2HH/3HH, fake break, trending-day label, pivot (R2/S2) breakout.
- Signal engine: stops (35-point hard stop, level-distance sizing), targets, zone filter, arbiter.
- Option layer: CE/PE by delta, delta-converted stop and targets, lots from the 1% budget.
- Paper engine: exits, partial booking, charges, SQLite with idempotent restart.
- Live runtime: REST polling, catch-up, tick polling, watchdog, heartbeat, Telegram (optional),
  CLI (`run`, `replay`, `sweep`, `report`, `reconcile`, `status`, `summary`, `check`).
- Validation tooling: multi-day replay, statistics, sanity checks, config sweeps, live-versus-
  replay reconciliation, forward-test scripts.
- Delivery baseline (Phase 11): pinned lock files, CI, pre-commit, architecture rules
  (import-linter), coverage gate, developer tasks, contributing guide.

### Fixed
- `targets.move_sl_to_entry_after_t1` is now honoured (it was read from config but ignored).

### Known limits
- Replay uses synthetic option prices; the replay shows no edge distinguishable from zero and
  one option point of slippage per fill makes it a loss (`intraday-setup.md`, section 10, item 13).
- Two package cycles (`storage` and `paper`) are tolerated debt, tracked in `pyproject.toml`.
