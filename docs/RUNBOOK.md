# ORB bot runbook

Paper-first. With `LIVE_MODE=live` and `LIVE_ARMED=true` the bot trades every market day on its
own (dry run or real). Real orders also need `DRY_RUN=false`. Send `/stop` to skip the rest of a
day and `/start` to allow it again.

## Daily (unattended)

NSSM services on the EC2 host:

| Service | Command | Purpose |
|---|---|---|
| `OrbBot` | `orb-bot serve` | Sleep until the next session, run it, reconcile, repeat |
| `OrbBotWatch` | `orb-bot watchdog` | Telegram alert if the heartbeat goes silent (never trades) |

Telegram commands (authorized chat only). `/help` lists them.

| Group | Commands |
|---|---|
| Status | `/status` (heartbeat, order mode, today's trading state, risk), `/mode`, `/version`, `/risk`, `/funds`, `/log [n]` |
| Today | `/today`, `/pnl [day\|week\|month]`, `/positions`, `/progress` (stops, targets and broker stop per position), `/signals`, `/orders` (with entry-versus-plan slippage), `/levels`, `/report` |
| Control | `/stop`, `/start`, `/exit <symbol>`, `/flatten`, `/rearm`, `/reconcile`, `/clearhalt`, `/restart` |

- `/stop`: no new entries for the rest of today (open positions are still managed). It ends by
  itself at midnight and survives a restart. `/start` allows entries again the same day.
  `/pause` and `/resume` are the same commands.
- `/exit <symbol>` closes one open position (the full symbol, or a unique part such as `24500CE`).
  When the sale is a real order the bot first replies with a one-time code to send back.
- `/flatten`, `/clearhalt` and `/restart` also ask for a one-time code first.
- `/rearm` puts the broker-side stop back on open positions that lost it. `/reconcile` checks the
  bot's books against the broker (or the dry-run ledger) now. `/exit`, `/rearm` and `/reconcile`
  work while a session is running; between sessions they say so.
- `/clearhalt` clears the kill switch or a reconciliation entry block. Check the broker first.
- `/restart` ends the session and the service; NSSM starts it again. Open positions and their
  broker stops are restored on startup.
- `/log` shows the last lines of today's log with tokens and secrets masked.
- Every control command leaves an `operator_command` line in the JSONL log.

## Restart

```powershell
nssm restart OrbBot
nssm restart OrbBotWatch
C:\orb-bot\.venv\Scripts\python.exe -m orb_intraday_bot status
```

Open positions are restored from SQLite. A mid-day restart catches up on candles and marks
stale signals as MISSED (never entered late).

## Flatten

1. Telegram: `/flatten` then `/flatten <code>`, or
2. CLI on the host: `python -m orb_intraday_bot flatten`, or
3. Create `C:\orb-bot\data\FLATTEN`

The running session closes every open position **this bot owns**. On a shared Groww account it
never cancels the other bot's orders (ORB- reference prefix only).

## Broker down

- Data errors are retried; three in a row alert on Telegram.
- Stall (no candle) alerts after `runtime.stall_alert_sec`.
- If live and the bot dies, the **broker-side stop-loss** protects the position. The watchdog
  only alerts; it does not trade.

## Rollback

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\orb-bot\scripts\deploy\Rollback-OrbBot.ps1
```

Or revert the bad commit on `main` and re-run the CD workflow (it always deploys `main`). Deploy is blocked during market hours and
while positions are open unless `force=true`.

## Rotate keys

1. Create a new Groww API key (or keep using the existing `GROWW_TOTP_*` key).
2. Update GitHub Secrets (`GROWW_TOTP_*`; optionally `GROWW_TRADING_TOTP_*` to override).
3. Redeploy (or edit `C:\orb-bot\.env` and `nssm restart OrbBot`).
4. Revoke the old key in Groww.

## Staged rollout checklist

| Stage | How | Exit check |
|---|---|---|
| 1. Paper on EC2 | `LIVE_MODE=paper` (default), run 4 weeks | `FORWARD_TEST.md` operational criteria |
| 2. Shadow | `LIVE_MODE=shadow` | Shadow fills vs modelled within tolerance |
| 3. Dry run on the live market | `LIVE_MODE=live`, `LIVE_ARMED=true`, `DRY_RUN=true` | Two or more weeks of clean sessions (see below) |
| 4. Live 1 lot | Same, with `DRY_RUN=false` | Daily review; risk limits in `config/risk.yaml` |
| 5. Scale | Raise `max_lots_per_trade` only by changing `config/risk.yaml` and redeploying | Written go/no-go |

There is no morning confirmation: once the settings are on, every market day trades. If
`LIVE_ARMED` is false or credentials are missing the bot runs as paper and says so on Telegram.

## Dry run and live: what the bot does

`DRY_RUN=true` and `DRY_RUN=false` run **the same code**. The only difference is the last step:
the call that sends the order to Groww is replaced by a simulation that fills against the real
bid/ask. Everything else is shared, and the logs, database and Telegram messages look the same
(the `mode` column says `dry_run` or `live`).

Per trade:

1. **Entry.** The signal is planned as before. The risk gate checks the day limits, the option
   bid/ask spread (max 5%), that the ask has not run more than 2% above the plan, and the funds.
   One limit buy is sent 2% above the ask. If it has not filled after 10 seconds it is cancelled
   and the trade is skipped (never chased, no market fallback). A partial fill is sold straight
   back.
2. **Protective stop.** Straight after the fill a stop-loss sell is placed at the broker, at the
   option stop. It follows the position (moves to cost after T1, shrinks after a partial exit).
   In live mode it protects the position if the bot dies.
3. **Exits.** The bot decides when, with the same rules as paper (hard stop, close stop, T1, T2,
   option stop). To exit it cancels the stop, sells with a limit just under the bid, waits 6 s,
   then sells at market. It never sells while it cannot confirm the stop is gone. If the stop
   itself was executed, that is booked as `BROKER_STOP`.
4. **Square-off.** At `square_off_time` (15:20) every executed position is closed and no new
   entry is taken. Paper positions keep the 15:30 close.
5. **Reconciliation.** Every minute the bot compares what it thinks it holds with the broker (in
   a dry run: with the simulated ledger). A mismatch **blocks new entries and alerts, and never
   sells anything**. A buy that filled with no position on record is also reported.

### Checking a dry-run day

- Telegram: the entry, exit and square-off messages, and no `CRITICAL` alert.
- `/today` and `/report` as usual. Positions with `order_mode = dry_run` are the dry-run trades.
- The database (`data/paper_trades.sqlite`): table `orders` has every simulated order (`mode = 'dry_run'`,
  legs `ENTRY`, `PROTECT`, `EXIT`, `FLATTEN`), `events` has the order timeline.
- The JSON log (`logs/orb-bot-<day>.jsonl`): `order_sent`, `entry_filled`, `entry_vetoed`,
  `exit_filled`, `reconcile_mismatch`.
- Compare `planned_entry` with `entry_ltp` on each position: that is the slippage a real order
  would have had.
- Every session should end with no open position and no `CRITICAL` alert.

### Known limits (read before turning `DRY_RUN` off)

- The dry run proves the logic, the quotes, the timing and the alerts. It cannot prove the Groww
  order calls themselves (field names for positions and funds, how a stop-loss-limit order is
  accepted, rejection messages). Those are checked for the first time with `DRY_RUN=false`.
  Start with one lot and watch the first trades.
- Sharing the Groww account with `GrowwBot` means reconciliation can only detect that the
  broker holds *less* than the bot expects, not more.
- The dry run fills the whole quantity at the best bid or ask. A real order on a thin option can
  fill worse or partly.

## Kill switch

Trips on daily loss limit (1% of capital) or 2 consecutive losses (defaults in `config/risk.yaml`).
When tripped: no new entries, `data/FLATTEN` is written, Telegram kill-switch alert. A flatten
that cannot complete (the sale does not fill) is retried on every poll and ends in a `CRITICAL`
alert if anything is still open at the end of the session.

A reconciliation mismatch is different: it only blocks new entries and alerts, because selling
into a mismatch could do harm. Check the broker, fix it, then restart the service.
