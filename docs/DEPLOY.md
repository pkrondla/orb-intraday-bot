# Deploy the ORB bot to Windows EC2

The paper (and later live) bot runs on the same Windows EC2 as `GrowwBot`, in a separate
directory and as separate NSSM services. Nothing of `GrowwBot` is touched.

## One-time EC2 prerequisites

Already done if the Groww bot is deployed:

- WinRM over HTTPS on port 5986 (Security Group allows inbound 5986)
- Python 3.12 on PATH
- Git on PATH
- `nssm.exe` on PATH (for example `C:\Windows\System32\nssm.exe`)

## GitHub configuration

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
| `LIVE_ARMED` | `false` until you intentionally arm live |
| `HEALTHCHECK_URL` | Optional healthchecks.io ping URL |

## Deploy

CI runs automatically on every push to `main`. Deploy is **manual**:

1. Actions → **CD - Deploy ORB bot to Windows EC2** → Run workflow
2. There is no ref to choose: it always deploys `main`
3. Leave `force` as `false` unless you deliberately override the market-hours / open-position guard

The workflow:

1. Clones or updates `C:\orb-bot` (preserves `data/`)
2. Writes `.env` from Secrets and Variables
3. Creates `.venv`, installs from `requirements.lock`
4. Installs / restarts NSSM services `OrbBot` (`orb-bot serve`) and `OrbBotWatch` (`orb-bot watchdog`)
5. Runs `orb-bot check` as a smoke test

Deploy is refused between 09:00 and 15:45 IST, and while the database has open positions,
unless `force=true`.

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

`/status` `/today` `/positions` `/report` `/pause` `/resume` `/flatten` `/arm` `/disarm` `/help`
