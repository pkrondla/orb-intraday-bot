"""/dryrun: show or switch DRY_RUN, kept in data/DRY_RUN_OVERRIDE and applied at start."""

from __future__ import annotations

from pathlib import Path

from orb_intraday_bot.runtime.dry_run_switch import (
    apply_override,
    ignored_override_note,
    read_override,
    real_orders_blocker,
    source_text,
    write_override,
)
from orb_intraday_bot.runtime.telegram_commands import CommandKind, parse_command
from tests.runtime_support import at
from tests.unit.test_live_runner_execution import _primed
from tests.unit.test_operator_commands import _send, _state
from tests.unit.test_serve_execution import _ctx, _settings

LIVE = {"live_mode": "live", "live_armed": True}


# ---- the saved override ------------------------------------------------------------------
def test_the_override_is_saved_and_read_back(tmp_path: Path) -> None:
    assert read_override(tmp_path) is None
    write_override(tmp_path, dry_run=False)
    assert read_override(tmp_path) is False
    write_override(tmp_path, dry_run=True)
    assert read_override(tmp_path) is True
    (tmp_path / "DRY_RUN_OVERRIDE").write_text("maybe\n", encoding="utf-8")
    assert read_override(tmp_path) is None                  # anything else is no override


def test_a_saved_choice_replaces_dry_run_at_start(tmp_path: Path) -> None:
    settings = _settings(**LIVE)
    apply_override(settings, tmp_path)                      # no file: unchanged
    assert settings.dry_run is True and source_text(settings, tmp_path).startswith("from")
    write_override(tmp_path, dry_run=False)
    apply_override(settings, tmp_path)
    assert settings.dry_run is False and source_text(settings, tmp_path) == "set by /dryrun"
    assert ignored_override_note(settings, tmp_path) == ""


def test_real_orders_are_not_switched_on_when_the_settings_do_not_allow_them(
    tmp_path: Path,
) -> None:
    write_override(tmp_path, dry_run=False)
    for extra, why in (
        ({"live_mode": "paper"}, "LIVE_MODE is paper"),
        ({"live_mode": "live", "live_armed": False}, "LIVE_ARMED is false"),
    ):
        settings = _settings(**extra)
        apply_override(settings, tmp_path)
        assert settings.dry_run is True                     # stays a dry run
        assert why in ignored_override_note(settings, tmp_path)
    no_creds = _settings(**LIVE)
    no_creds.groww_totp_token = type(no_creds.groww_totp_token)("")
    assert "credentials" in (real_orders_blocker(no_creds) or "")


def test_turning_the_dry_run_on_is_always_allowed(tmp_path: Path) -> None:
    write_override(tmp_path, dry_run=True)
    settings = _settings(live_mode="paper", dry_run=False)
    apply_override(settings, tmp_path)
    assert settings.dry_run is True


# ---- the command -------------------------------------------------------------------------
def test_dryrun_is_parsed() -> None:
    command = parse_command("/dryrun off")
    assert (command.kind, command.arg) == (CommandKind.DRYRUN, "off")


def test_dryrun_shows_the_setting_and_where_it_comes_from(tmp_path: Path) -> None:
    ctx, state = _ctx(tmp_path, **LIVE), _state()
    shown = _send(CommandKind.DRYRUN, "", ctx=ctx, state=state)[0]
    assert "ON" in shown and "no order reaches Groww" in shown and "from the .env" in shown
    write_override(tmp_path, dry_run=True)
    assert "set by /dryrun" in _send(CommandKind.DRYRUN, "status", ctx=ctx, state=state)[0]
    assert "Usage" in _send(CommandKind.DRYRUN, "maybe", ctx=ctx, state=state)[0]
    assert read_override(tmp_path) is True                  # nothing was changed by those


def test_dryrun_off_saves_the_choice_and_restarts_the_bot(tmp_path: Path) -> None:
    ctx, state = _ctx(tmp_path, **LIVE), _state()
    reply = _send(CommandKind.DRYRUN, "off", ctx=ctx, state=state)[0]
    assert "REAL order" in reply and "Restarting" in reply
    assert read_override(tmp_path) is False
    assert state.stop_requested


def test_dryrun_on_goes_back_to_a_dry_run(tmp_path: Path) -> None:
    ctx, state = _ctx(tmp_path, dry_run=False, **LIVE), _state()
    reply = _send(CommandKind.DRYRUN, "on", ctx=ctx, state=state)[0]
    assert "now ON" in reply and "no order will reach the broker" in reply
    assert read_override(tmp_path) is True and state.stop_requested


def test_dryrun_to_the_current_value_changes_nothing(tmp_path: Path) -> None:
    ctx, state = _ctx(tmp_path, **LIVE), _state()
    assert "already" in _send(CommandKind.DRYRUN, "on", ctx=ctx, state=state)[0]
    assert read_override(tmp_path) is None and not state.stop_requested


def test_dryrun_off_is_refused_when_real_orders_are_not_possible(tmp_path: Path) -> None:
    ctx, state = _ctx(tmp_path, live_mode="paper"), _state()
    reply = _send(CommandKind.DRYRUN, "off", ctx=ctx, state=state)[0]
    assert "cannot be turned on" in reply and "LIVE_MODE is paper" in reply
    assert read_override(tmp_path) is None and not state.stop_requested


def test_dryrun_is_not_switched_while_a_position_is_open(tmp_path: Path) -> None:
    s, runner, _, position = _primed(tmp_path, at_time=at(10, 0))
    ctx, state = _ctx(tmp_path, **LIVE), _state(runner, s.risk)
    reply = _send(CommandKind.DRYRUN, "off", ctx=ctx, state=state)[0]
    assert "Not switched" in reply and position.trading_symbol in reply
    assert read_override(tmp_path) is None and not state.stop_requested
    runner.exit_positions({position.position_id})
    assert "Restarting" in _send(CommandKind.DRYRUN, "off", ctx=ctx, state=state)[0]


def test_dryrun_between_sessions_checks_the_database(tmp_path: Path) -> None:
    from tests.unit.test_operator_commands import _database

    ctx = _ctx(tmp_path, **LIVE)
    store = _database(ctx)
    store.close()
    state = _state()
    assert "Not switched" in _send(CommandKind.DRYRUN, "off", ctx=ctx, state=state)[0]
    assert read_override(tmp_path) is None
