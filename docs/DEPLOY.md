# Deploy the ORB bot to Windows EC2

The bot (paper, dry run, or live) runs on the same Windows EC2 as `GrowwBot`, in a separate
directory and as separate NSSM services. Nothing of `GrowwBot` is touched.

## One-time EC2 prerequisites

Already done if the Groww bot is deployed:

- WinRM over HTTPS on port 5986 (Security Group allows inbound 5986)
- Python 3.12 on PATH
- Git on PATH
- `nssm.exe` on PATH (for example `C:\Windows\System32\nssm.exe`)

## GitHub configuration

The deploy job runs in the GitHub Actions environment named `prod`, so add the secrets and
variables below under Settings > Environments > prod (repository-level ones are also read).
If you rename the environment, change `environment:` in `.github/workflows/cd-ec2.yml`.

### Secrets

| Name | Purpose |
|---|---|
| `WINRM_PASSWORD` | Windows login password |
| `GH_PAT` | PAT with `repo` read access (git clone/pull) |
| `GROWW_TOTP_TOKEN` | Groww API TOTP token (data and, by default, live trading) |
| `GROWW_TOTP_SECRET` | Groww API TOTP secret |
| `TELEGRAM_BOT_TOKEN` | New dedicated bot from @BotFather |
| `GROWW_TRADING_TOTP_TOKEN` | Optional live override; leave unset to reuse `GROWW_TOTP_*` |
| `GROWW_TRADING_TOTP_SECRET` | Optional live override |

### Variables

| Name | Purpose |
|---|---|
| `WINRM_HOST` | Public IP or DNS of the EC2 instance |
| `WINRM_USERNAME` | Windows user (for example `Administrator`) |
| `TELEGRAM_CHAT_ID` | Your Telegram chat id |
| `LIVE_MODE` | `paper` (default), `shadow`, or `live` |
| `LIVE_ARMED` | `false` until you intentionally switch the live pipeline on (also needed for the dry run) |
| `DRY_RUN` | `true` (default): the whole live pipeline, but no order is sent to the broker. `false`: real orders |
| `HEALTHCHECK_URL` | Optional healthchecks.io ping URL |
| `CAPITAL_INR` | Optional default capital the bot sizes against (for example `750000`). Unset keeps `config/default.yaml` |
| `RISK_PER_TRADE_PCT` | Optional default risk per trade in percent (`1.5` = 1.5%) |
| `DAILY_LOSS_LIMIT_PCT` | Optional default daily loss limit in percent of capital (`1` = 1%) |
| `MAX_LOTS_PER_TRADE` | Optional default lot cap per trade |

The four limit variables are only the defaults. On Telegram `/capital`, `/riskpct`, `/dailyloss` and `/maxlots` change them at once without a redeploy, and the Telegram value wins (also after a redeploy) until `/limits reset`.

### The dry run (what to set before the first live market day)

| Variable | Value |
|---|---|
| `LIVE_MODE` | `live` |
| `LIVE_ARMED` | `true` |
| `DRY_RUN` | `true` |

Trading is then on every market day with nothing to send in the morning. To skip the rest of a
day send `/stop` to the Telegram bot; `/start` allows it again that day. A stop applies to that
day only and is remembered across a restart. With these settings **no order can reach Groww**: the order step is replaced by a
simulation that fills against the real bid/ask. See `docs/RUNBOOK.md` for what to check.

`/dryrun off` on Telegram switches to real orders without a deploy (and `/dryrun on` back). It saves the choice on the server, so it wins over the `DRY_RUN` variable until you change it again.

Real orders need all of: `LIVE_MODE=live`, `LIVE_ARMED=true`, `DRY_RUN=false` and Groww credentials. If `DRY_RUN=false` is set without the others the bot refuses to start.

## Deploy

CI runs automatically on every push to `main`. Deploy is **manual**:

1. Actions → **CD - Deploy ORB bot to Windows EC2** → Run workflow
2. There is no ref to choose and no option to set: it always deploys `main`, at any time

The workflow:

1. Clones or updates `C:\orb-bot` (preserves `data/`)
2. Writes `.env` from Secrets and Variables
3. Creates `.venv`, installs from `requirements.lock`
4. Installs / restarts NSSM services `OrbBot` (`orb-bot serve`) and `OrbBotWatch` (`orb-bot watchdog`)
5. Runs `orb-bot check` as a smoke test

Deploy is allowed at any time, including during market hours and with open positions. The
services restart; open positions are restored from SQLite, their broker stops are re-armed and
the books are reconciled. The bot does not watch the market for the few seconds the restart takes.

## Rollback

On the host:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\orb-bot\scripts\deploy\Rollback-OrbBot.ps1
```

The CD workflow always deploys `main`, so to roll back from GitHub, revert the bad commit on
`main` and run the workflow again.

## Local ops on the host

```powershell
nssm status OrbBot
nssm status OrbBotWatch
C:\orb-bot\.venv\Scripts\python.exe -m orb_intraday_bot status
C:\orb-bot\.venv\Scripts\python.exe -m orb_intraday_bot flatten
nssm restart OrbBot
```

Logs: `C:\orb-bot\logs\orb-bot.log`, `orb-bot-error.log`, `orb-watch.log`.

## Telegram

Create a **new** bot with @BotFather (do not reuse the Groww bot token). Message it once,
then read your chat id from `https://api.telegram.org/bot<token>/getUpdates`. Commands:

`/help` lists them; the full list is in `docs/RUNBOOK.md`. Status: `/status` `/mode` `/version` `/risk` `/funds` `/log`. Today: `/today` `/pnl` `/positions` `/progress` `/signals` `/orders` `/levels` `/report`. Control: `/stop` `/start` `/exit` `/flatten` `/rearm` `/reconcile` `/clearhalt` `/restart` `/dryrun`. Limits: `/limits` `/capital` `/riskpct` `/dailyloss` `/maxlots`.
