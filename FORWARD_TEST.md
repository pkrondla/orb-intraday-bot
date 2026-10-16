# Forward paper test (4 weeks)

Goal: show that the live bot behaves like the replay, on real option prices, before anyone
considers real money. Paper only: nothing here places an order.

## Daily routine

1. Once: `python -m orb_intraday_bot check` (login, contract, pivots, option chain, database).
2. Once: `powershell -ExecutionPolicy Bypass -File scripts\forward_test\register_tasks.ps1`.
   It schedules `scripts\forward_test\run_day.ps1` every weekday at 09:00 IST (converted to this
   PC's local time; run it again if the local clock changes for daylight saving). The PC must be
   on and signed in; the task wakes it from sleep. Remove it with `-Remove`.
3. Each day the task runs the session, then `reconcile` and `summary`, and writes
   `data\logs\task-YYYY-MM-DD.log` (and the bot's own `orb-bot-YYYY-MM-DD.log`).
4. Glance at Telegram (optional) or at `python -m orb_intraday_bot status` during the day.

## Weekly review

```
python -m orb_intraday_bot report                    # everything so far
python -m orb_intraday_bot report --from 2026-10-19  # a window
python -m orb_intraday_bot reconcile 2026-10-20      # one day, live versus replay
```

Check `data\logs` for `WARNING`/`ERROR` lines, and the heartbeat with `status`.

## What "passed" means (proposed; change them if you disagree)

Operational (pass or fail):

| Check | Target |
|---|---|
| Sessions that ran from the open to the final summary without a crash or manual restart | at least 18 of 20 |
| Days where `reconcile` shows no difference (or every difference is explained) | at least 90% |
| Signals recorded as MISSED (stale) because the bot was not running | 0 on days the PC was on |
| Duplicate positions, positions left open after 15:30, orders placed | 0 |
| Option-plan failures (`released`, stage `option`) | under 5% of accepted signals |
| Stall or data-error alerts that were real outages | each one explained |

Behaviour (judged by you, not pass or fail):

- Live exit points against the replay on the same day (`reconcile` prints both).
- The live option premium at entry against the modelled one, and the `delta_out_of_band` or
  `delta_missing` flags: how often does the chain give a usable delta?
- Real round-trip cost: the replay says one option point of slippage per fill turns a flat result
  into a loss (see PROGRESS.md, Phase 9). Compare the chain's bid/ask spread at entry.

Performance over 20 sessions is too small a sample to prove an edge. Use it to catch mistakes in
the bot, not to approve the strategy.

## Change during the test: pivot inputs (16 Oct 2026)

The pivots now match TradingView (previous session's daily-bar high and low, and the exchange's
closing price; see `intraday-setup.md` section 4). Before this date the bot built them from the
previous session's 3-minute candles, which put the levels 2 to 12 points off. Consequences:

- The run banner shows the inputs and the close source, for example
  `H 23095.0 L 22671.0 C 22984.8 (exchange)`. A day whose banner shows `(candle)` or `(vwap)`
  means the exchange close was unavailable that morning.
- Each live morning saves its inputs to `data\history\<symbol>.levels.json`, and `reconcile`
  replays with those same pivots, so live and replay agree on them.
- `reconcile` for a day run **before** this change replays with the new pivots while the live
  run used the old ones, so it can show differences that are not bugs. Treat sessions from
  before 16 Oct as a separate period when judging the criteria above.

## Known limits

- The streaming feed does not connect from this machine, so the bot polls (every 15 s with a
  position open, candles a few seconds after each boundary).
- A candle's high/low can reach both the stop and a target; the paper engine assumes the stop
  came first. Live polls shrink that blind spot only while a position is open.
- Groww contract notes, not these charge assumptions, are the truth for costs.
