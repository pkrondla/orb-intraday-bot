# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/). Versions follow
[Semantic Versioning](https://semver.org/). The strategy rules are versioned with the code: a
change to a rule, a stop, a target or sizing is at least a MINOR release.

## [Unreleased]

### Changed (pivot inputs now match TradingView; this changes live and replay behaviour)
- The bot's pivots differed from TradingView's "Pivot Points Standard" (Traditional) by 2 to
  12 points. The formulas were right; the previous session's low and close were not. TradingView's
  daily bar includes the pre-open trade in the low and closes on the exchange's closing price.
- High and low now come from Groww's daily bar (includes the pre-open trade). The close is the
  exchange's previous close from the live quote (cross-checked against `last_price - day_change`),
  else the volume-weighted 1-minute closes of 15:00 to 15:40, else the last 3-minute candle.
  Checked on 15 Oct 2026: R3, R2, R1, P, S1 and S2 equal TradingView's to the displayed 0.1.
- The run banner and `check` print the high, low, close and where the close came from.
- The live run saves each morning's levels to `data/history/<symbol>.levels.json`. Replay, sweep
  and reconcile read them back (and fetch the VWAP version once for days the bot never saw), so a
  replay of a live day uses the same pivots the live run used. Days with no levels fall back to
  the candles. Replays of earlier days use the VWAP close, which is close to, not exactly, the
  exchange's number.
- Results of replays and sweeps will differ from before for the same days. The golden replay test
  is unchanged: it runs on frozen candles without saved levels.

### Changed (Phase 12, behaviour-preserving: the golden 54-day replay is unchanged)
- Golden replay test (`tests/golden`): the 54-session replay, 212 trades, is frozen as a file.
- `paper` depends on a `PositionRepository` port instead of SQLite; the storage/paper import
  cycle and its `ignore_imports` exceptions are gone.
- `cli.py` only parses and prints; the work moved to `app/` services (replay, forward test,
  health) that take their collaborators as parameters. `runtime/alerts.py` owns every alert.
- Error taxonomy (`core/errors.py`): each failure family has one reaction (retry, reject the
  signal, stop, flatten). The runner stops on safety or configuration errors instead of retrying.
- Positions record the strategy hash (`config/fingerprint.py`); the run banner prints it.
- Structured JSON event log with a run id (`logs/orb-bot-<day>.jsonl`) next to the text log.
- Coverage gate raised from 85% to 92%; test fakes shared in `tests/runtime_support.py`.

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
