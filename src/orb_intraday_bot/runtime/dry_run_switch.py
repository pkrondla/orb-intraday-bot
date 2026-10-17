"""The operator's `/dryrun on|off`: a saved override of the DRY_RUN setting.

`DRY_RUN` normally comes from `.env` (the GitHub variable). `/dryrun` writes `data/DRY_RUN_OVERRIDE`
(`on` or `off`) and restarts the bot; on start the override replaces the `.env` value. It stays
until `/dryrun` changes it again, so a redeploy does not undo it.

Turning real orders on is only honoured when the usual conditions hold (live mode, armed, trading
credentials). If they no longer hold, the override is ignored and the bot stays a dry run.
"""

from __future__ import annotations

import logging
from pathlib import Path

from orb_intraday_bot.config.settings import Settings

log = logging.getLogger("orb.dry_run_switch")
OVERRIDE_FILE_NAME = "DRY_RUN_OVERRIDE"


def override_path(data_dir: Path) -> Path:
    return data_dir / OVERRIDE_FILE_NAME


def read_override(data_dir: Path) -> bool | None:
    """The saved `dry_run` value (True = dry run), or None when there is no usable override."""
    try:
        text = override_path(data_dir).read_text(encoding="utf-8").strip().lower()
    except OSError:
        return None
    if text == "on":
        return True
    if text == "off":
        return False
    return None


def write_override(data_dir: Path, *, dry_run: bool) -> None:
    path = override_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(("on" if dry_run else "off") + "\n", encoding="utf-8")


def real_orders_blocker(settings: Settings) -> str | None:
    """Why real orders cannot be on with these settings, or None when they could be."""
    if settings.live_mode != "live":
        return f"LIVE_MODE is {settings.live_mode}, not live"
    if not settings.live_armed:
        return "LIVE_ARMED is false"
    if not settings.has_trading_credentials():
        return "the Groww trading credentials are missing"
    return None


def apply_override(settings: Settings, data_dir: Path) -> None:
    """At start: let a saved `/dryrun` choice replace DRY_RUN, when that is allowed."""
    wanted = read_override(data_dir)
    if wanted is None or wanted == settings.dry_run:
        return
    if not wanted:
        blocker = real_orders_blocker(settings)
        if blocker:
            log.warning("ignoring /dryrun off: %s", blocker)
            return
    settings.dry_run = wanted


def ignored_override_note(settings: Settings, data_dir: Path) -> str:
    """A warning for the startup message when a saved override could not be applied."""
    wanted = read_override(data_dir)
    if wanted is None or wanted == settings.dry_run:
        return ""
    return (
        "The saved /dryrun off was NOT applied, so this is still a dry run: "
        f"{real_orders_blocker(settings) or 'unknown reason'}."
    )


def source_text(settings: Settings, data_dir: Path) -> str:
    """Where the DRY_RUN in force comes from."""
    if read_override(data_dir) == settings.dry_run:
        return "set by /dryrun"
    return "from the .env / GitHub setting"
