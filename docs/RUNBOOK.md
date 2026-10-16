# ORB bot runbook

Paper-first. Live trading stays disarmed until you deliberately arm it.

## Daily (unattended)

NSSM services on the EC2 host:

| Service | Command | Purpose |
|---|---|---|
| `OrbBot` | `orb-bot serve` | Sleep until the next session, run it, reconcile, repeat |
| `OrbBotWatch` | `orb-bot watchdog` | Telegram alert if the heartbeat goes silent (never trades) |

Telegram commands (authorized chat only): `/status` `/today` `/positions` `/report`
`/pause` `/resume` `/flatten` `/arm` `/disarm` `/help`.

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
| 3. Live 1 lot | `LIVE_MODE=live`, `LIVE_ARMED=true`, trading key set, daily `/arm <code>` | Daily review; risk limits in `config/risk.yaml` |
| 4. Scale | Raise `max_lots_per_trade` only by changing `config/risk.yaml` and redeploying | Written go/no-go |

Live still requires a Telegram `/arm <code>` every morning before 09:10. Without it the bot
falls back to paper and says so on Telegram.

## Kill switch

Trips on daily loss limit (1% of capital) or 2 consecutive losses (defaults in `config/risk.yaml`).
When tripped: no new entries, `data/FLATTEN` is written, Telegram kill-switch alert.
