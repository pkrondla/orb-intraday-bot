# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/). Versions follow
[Semantic Versioning](https://semver.org/). The strategy rules are versioned with the code: a
change to a rule, a stop, a target or sizing is at least a MINOR release.

## [Unreleased]

### Changed
- Telegram OPENED card: BUY now shows the futures price at signal time (same as SL / T1 / T2),
  so the four chart levels can be copied from one message.
- Telegram SL / T1 / T2 exits are the same card style: option fill and the futures price at
  the event, so the trigger can be marked on the chart.

### Fixed
- Telegram `/help` (and other commands) in the ORB bot chat: drop a restrictive
  `allowed_updates` filter that could make `getUpdates` miss messages, clear any
  webhook before polling, and treat quoted/whitespace chat ids as the same chat.
- Telegram send on Windows EC2: use certifi's CA bundle for `api.telegram.org`
  (urllib's default store was failing with URLError while Groww's requests path
  still worked). `check --send-test-alert` now reports a failed send.

## [0.12.0] - 2026-10-17

More Telegram commands for the operator.

### Added
- Status: `/mode` (settings and what they resolve to), `/version`, `/risk` (limits, kill switch,
  entry block, `/stop` state), `/funds` (capital and available margin), `/log [n]` (tail of
  today's log with secrets masked). `/status` now also shows the order mode, today's trading
  state, the session state and the risk counters.
- Today: `/pnl [day|week|month]`, `/progress` (stops, targets, broker stop per position),
  `/signals`, `/orders` (with entry-versus-plan slippage), `/levels`.
- Control: `/exit <symbol>` (one position), `/rearm`
  (re-place a missing broker stop), `/reconcile` (check the books now), `/clearhalt` (clear the
  kill switch or entry block) and `/restart` (graceful service restart).
- `ExitReason.MANUAL` for `/exit`. Every control command writes an `operator_command` log line.
- `TradeStore.orders_for_day`, `PaperEngine.protect_all`, and `close_all(position_ids=...)`.
- Money limits from Telegram: `/limits`, `/capital <amount>`, `/riskpct <percent>`,
  `/dailyloss <percent>` and `/maxlots <n>` change capital, risk per trade, the daily loss limit and
  the lot cap at once, without a restart or redeploy. Saved in `data/LIMITS_OVERRIDE.json`; `reset`
  restores the default. New optional GitHub variables `CAPITAL_INR`, `RISK_PER_TRADE_PCT`,
  `DAILY_LOSS_LIMIT_PCT` and `MAX_LOTS_PER_TRADE` set the defaults. Ranges are enforced.
  `RiskEngine.recheck_daily_loss` trips the kill switch when a new limit is already breached.
- `/dryrun [on|off|status]`: show or switch `DRY_RUN` from Telegram, without a code. It saves
  `data/DRY_RUN_OVERRIDE` and restarts the bot. It refuses while a position is open, and refuses
  real orders unless live mode, `LIVE_ARMED` and credentials are all in place. The saved choice
  replaces the `DRY_RUN` environment value until changed again.

- The first EC2 deploy failed to create the `OrbBot` NSSM service: `Invoke-Native`'s
  `$Name` parameter shadowed the service name inside the scriptblocks, so `nssm install`
  never created `OrbBot`. The helper's label is now `$Label`.

### Changed
- No Telegram command asks for a confirmation code any more. `/flatten`, `/exit`,
  `/clearhalt` and `/restart` run as soon as the authorized chat sends them. `/help` lists
  every command with its purpose.
- The deploy workflow no longer refuses to run between 09:00 and 15:45 IST or while positions are
  open, and the `force` input is gone. A deploy restarts the services, which restore open
  positions, re-arm broker stops and reconcile.

### Fixed
- A flatten request made while nothing was open used to mark the flatten as done for the whole
  session and leave the `FLATTEN` marker behind, so a later `/flatten` did nothing and the next
  day's first trade could be flattened at once. A request with nothing to close is now spent and
  the marker removed.
- The session banner said "when armed today, paper otherwise" (a leftover of the daily `/arm`).

## [0.11.0] - 2026-10-17

Dry run and live order path. Real orders stay off unless `DRY_RUN=false`.

### Added
- `DRY_RUN` setting (default `true`). With it true the bot runs exactly as live trading and only
  the broker order call is skipped (`DryRunVenue` fills against real bid/ask quotes). `DRY_RUN=false`
  sends real orders and needs `LIVE_MODE=live`, `LIVE_ARMED=true`, and credentials. There is no daily
  confirmation.
- `runtime/executor.py` (`LiveExecutor`): pre-trade risk gate (limits, spread, price sanity,
  funds), entry limit order with a 10 s timeout then cancel (never chased), partial fills sold
  back, broker-side protective stop kept in step with the position, exits (limit just under the
  bid, then market), partial-sale bookkeeping so nothing is sold twice, reconciliation with the
  broker, alerts and structured logs.
- Square-off at `risk.square_off_time` (15:20) for dry-run and live positions; no entries after it.
- `GrowwAccountReader` (funds and positions), `GrowwQuoteSource` (bid/ask), `AccountReadView`
  (the trading client with every order method removed).
- Schema v3: `orders.mode` and `events.mode` (`paper`, `dry_run`, `live`).
- `RiskEngine.block_entries` (blocks entries without a flatten) and `RiskEngine.rebuild`
  (today's counters after a restart). New risk limits for order timing in `config/risk.yaml`.

### Changed
- `LIVE_TRADING` is removed; `DRY_RUN` replaces it. The startup guard now refuses
  `DRY_RUN=false` unless live mode, `LIVE_ARMED` and credentials.
- No daily `/arm`. With the settings on, every market day trades. New `/stop` (no new entries for
  the rest of today, exits still managed) and `/start` (allow entries again today); a stop applies
  to that day only and survives a restart (`data/TRADING_OFF`). `/pause` and `/resume` are aliases.
  `/arm` and `/disarm` are gone. Operator commands (`/stop`, `/start`, `/flatten`) are now also
  handled while a session is running (before, only between sessions).
- A flatten that cannot sell is retried on every poll instead of being marked done. A session
  that ends with an open position raises a CRITICAL alert.

### Fixed
- `GrowwVenue` read the ordered quantity and the limit price as the fill; a cancel was assumed
  to have worked; a timeout while placing could leave an order untracked. The venue now reads
  `filled_quantity` and `average_fill_price`, confirms a cancel from the order status, and looks a
  timed-out order up by its reference before calling it rejected.

## [0.10.0] - 2026-10-16

Unattended paper service with Telegram, EC2 deploy, and live-order foundation (disarmed).

### Added
- Telegram notifier with retries, outbox, message split; command poller
  (`/status` `/today` `/positions` `/report` `/pause` `/resume` `/flatten` `/arm` `/disarm`).
- `orb-bot serve` (daily scheduler, NSE holidays, graceful stop) and `orb-bot watchdog`
  (dead-man Telegram alert only).
- GitHub Actions CD to Windows EC2 (WinRM + NSSM services `OrbBot` / `OrbBotWatch`),
  `docs/DEPLOY.md`, `docs/RUNBOOK.md`, `scripts/deploy/*`.
- Execution layer: `OrderIntent`, order state machine, `PaperVenue`, `ShadowVenue`,
  `OrderManager`, SQLite migrations (orders / order_fills / events).
- Risk engine (`config/risk.yaml`: 1 lot, 3 trades/day, 1% daily loss, halt after 2 losses),
  kill switch, staged arming (`LIVE_MODE` paper/shadow/live + daily `/arm`).
- `GrowwTradingClient` + `GrowwVenue` (separate trading key, ORB- reference reconciliation
  on a shared account). Live ships disarmed.

### Changed
- Direct commits to `main` are the working method (see `CONTRIBUTING.md`).
- Pivot inputs match TradingView (from the previous unreleased notes).

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
