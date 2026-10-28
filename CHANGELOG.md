# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/). Versions follow
[Semantic Versioning](https://semver.org/). The strategy rules are versioned with the code: a
change to a rule, a stop, a target or sizing is at least a MINOR release.

## [Unreleased]

## [0.19.1] - 2026-10-29

### Changed
- Telegram OPENED / BLOCKED labels now show pivot zone and trending-day status
  (`NEUTRAL · no trend`, `BULLISH · UPTREND`, …).

## [0.19.0] - 2026-10-29

### Changed
- Pivots stay reference. Longs below S1 and shorts above R1 are no longer
  blocked. OR setups (ORB, trap, 2LL/2HH, fake, extra whipsaw, R2/S2-as-OR)
  can fire in any zone. The only remaining zone block is aggressive entries
  (Case 1/2 and consolidation) inside R1–S1.
- Golden replay: 196 → 222 trades, fill-points 1693.7 → 2003.3.

## [0.18.0] - 2026-10-28

### Changed
- T2 is back as a **one-lot runner**: T1 ± OR-range (ORH − ORL), or the nearer pivot
  beyond T1, capped at R4/S4 (R5/S5 when T1 is already R4/S4). Neutral-zone T2
  stops at R1/S1. If there is no room, T2 = T1 and the whole position closes at T1.
- Booking: 1 lot closes all at T1. 2 or more lots book all but one at T1; the
  leftover lot runs to T2. After T1 the futures stop on the runner moves to entry.
  The Groww crash net stays put.
- T1 remains the notes Target. Both levels still fire 3 points in front.
- Golden replay: 198 → 196 trades, fill-points 345.05 → 1693.7. The runner restores a
  second fill on trades that reach T2; 1-lot trades still close all at T1.

## [0.17.0] - 2026-10-28

### Changed
- T1 is the notes' single Target. Hitting it closes the whole position. The halfway
  scalp, 50% book, move-SL-to-cost and T2 are gone. Neutral-zone T1 is the nearest
  in-zone pivot. Trap / Case 1/2 T1 is the opposite OR, capped at R2/S2.
- ORB and R2/S2-as-OR stops match the notes: candle close back through the level, or
  35 points, whichever first. The trade is no longer skipped when the level is farther
  than 35 or closer than 10. Lots are sized on the 35-point hard stop.
- Broker CE/PE SL stays the crash net (same 35 × delta calculation). All strategy
  exits fire on futures.
- Golden replay: 182 → 198 trades, fill-points 2195.48 → 345.05. More trades because
  ORB is no longer skipped when the level is outside 10–35. Fewer points because T1
  closes the whole position at the notes Target instead of scalping then running to T2.

## [0.16.0]

### Changed
- Trap / Case 1/2 targets: ORL sitting on S2 (or ORH on R2, within 10 points) is one
  magnet. No halfway T1 into a 36-point window. T1 is a distinct pivot in front, else
  T2 itself. The 28 Oct Case 2 short (22671 → ORL 22635 ≈ S2) no longer scalps 18 points
  then moves the stop to cost.
- After T1 the Groww crash net stays put. Only the futures stop moves to entry. Raising
  the PE/CE trigger to the entry premium was filling immediately (28 Oct BROKER_STOP at
  233.55 while futures were still at T1).
- Live SL / T1 / T2 stay on the **futures** chart levels. A worse CE/PE fill no longer
  shifts the broker option stop closer to the market (that was stopping trades before
  ORH / the 35-pt stop). Groww holds a crash net at the unshifted plan SL or a 35-point
  conversion from the fill, whichever is lower. Extra rupee risk is allowed. The OPENED
  card shows the futures SL and labels the option price as the broker net.
- Neutral-zone targets are scalp-and-run for every standard setup: T2 is the nearest
  pivot still inside R1–S1 (P, else R1 / S1); T1 is the first magnet in front (ORH / ORL
  or a pivot), else halfway. R2 / S2 are not aims in this zone. Other zones keep the
  setup T2 table. Golden replay: 186 → 182 trades, fill-points 2655.74 → 2195.48.
- T2 fires when futures come within 3 points of the target (`targets.t2_proximity_points`),
  so an S2/R2 rejection that misses the print still books. Fill is at the band (or at T2 /
  the open if price traded through). The OPENED card still shows the chart level. Golden
  replay: same 186 trades, fill-points 2612.65 → 2655.74.
- ORB and R2/S2 breakout stops sit on the setup level (touch + close-through), not at a
  relocated 35-point line. The trade is skipped if that distance is under 10 or over 35
  points. Traps, structure, Case 1/2 and consolidation are unchanged.
- Telegram OPENED card: BUY now shows the futures price at signal time (same as SL / T1 / T2),
  so the four chart levels can be copied from one message. SL / T1 / T2 also name the level
  (ORL, P, S2, 35 pts, halfway) next to the prices.
- Telegram SL / T1 / T2 exits are the same card style: option fill and the futures price at
  the event, so the trigger can be marked on the chart.

### Fixed
- Groww stop fills that report quantity but `average_fill_price` 0 are no longer
  booked as a ₹0 sale (that turned a break-even PE stop into a −₹8,986 wipe).
  Fill price falls back to the stop trigger, then the limit, then entry. The
  already-stored 26 Oct books stay wrong until those fills are repaired.
- Bump certifi to 2026.10.19 (PYSEC-2026-4320). growwapi 1.5.0 still declares
  `certifi<2026.0.0`, so lock installs use `--no-deps` (the lock is a full freeze).
- EC2 deploy: stop OrbBot/OrbBotWatch with `sc.exe` (and a taskkill timeout)
  before git, never wait unbounded on `nssm stop`. Nested `powershell.exe`
  was keep-alived by WinRM until the 30-minute job cancel. Resolve `nssm.exe`
  to a real path (WinRM's `Get-Command nssm` has an empty `.Source`).
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
