"""The error taxonomy: each family has one reaction, and the runner follows it."""

from __future__ import annotations

from pathlib import Path

import pytest

from orb_intraday_bot.adapters.groww.factory import MissingCredentialsError
from orb_intraday_bot.adapters.groww.market_data import NoSessionDataError
from orb_intraday_bot.adapters.groww.token import TokenCooldownError
from orb_intraday_bot.core.errors import (
    BotError,
    BrokerRejectionError,
    ConfigurationError,
    DataUnavailableError,
    Reaction,
    RiskBreachError,
    SafetyViolationError,
    TradePlanError,
    must_halt,
    reaction_for,
)
from orb_intraday_bot.options.pricing import OptionPlanError
from orb_intraday_bot.options.selector import OptionSelectionError
from orb_intraday_bot.risk.stops import PlanError
from orb_intraday_bot.runtime.guard import SafetyError
from tests.unit.test_runtime import DAY, Harness, at


@pytest.mark.parametrize(
    ("error", "reaction"),
    [
        (DataUnavailableError("x"), Reaction.RETRY),
        (NoSessionDataError("x"), Reaction.RETRY),
        (TokenCooldownError("x"), Reaction.RETRY),
        (TradePlanError("x"), Reaction.REJECT_SIGNAL),
        (PlanError("x"), Reaction.REJECT_SIGNAL),
        (OptionPlanError("x"), Reaction.REJECT_SIGNAL),
        (OptionSelectionError("x"), Reaction.REJECT_SIGNAL),
        (BrokerRejectionError("x"), Reaction.REJECT_SIGNAL),
        (SafetyError("x"), Reaction.STOP),
        (SafetyViolationError("x"), Reaction.STOP),
        (ConfigurationError("x"), Reaction.STOP),
        (MissingCredentialsError("x"), Reaction.STOP),
        (RiskBreachError("x"), Reaction.FLATTEN_AND_STOP),
    ],
)
def test_each_family_has_one_reaction(error: BotError, reaction: Reaction) -> None:
    assert reaction_for(error) is reaction


def test_old_builtin_bases_still_catch_the_classes() -> None:
    assert issubclass(PlanError, ValueError)
    assert issubclass(OptionPlanError, ValueError)
    assert issubclass(SafetyError, RuntimeError)
    assert issubclass(NoSessionDataError, RuntimeError)


def test_unknown_errors_get_the_default_of_the_place_that_caught_them() -> None:
    assert reaction_for(ValueError("sdk")) is Reaction.STOP
    assert reaction_for(ValueError("sdk"), Reaction.RETRY) is Reaction.RETRY
    assert not must_halt(ConnectionError("net"))
    assert must_halt(ConnectionError("net"), Reaction.STOP)
    assert must_halt(SafetyViolationError("x"))
    assert must_halt(RiskBreachError("x"))
    assert not must_halt(DataUnavailableError("x"))


def test_a_fatal_error_in_the_data_path_stops_the_run_instead_of_retrying(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path, at(9, 10))

    def forbidden(*_: object, **__: object) -> None:
        raise SafetyViolationError("an order method became reachable")

    h.source.session_candles = forbidden  # type: ignore[method-assign,assignment]
    outcome = h.runner().run()
    assert outcome.exit_code == 1
    assert "order method" in outcome.message
    assert "STOPPED" in h.notes.joined()
    assert "market data failing" not in h.notes.joined()      # it was not treated as a retry


def test_a_retryable_error_in_the_data_path_is_retried(tmp_path: Path) -> None:
    h = Harness(tmp_path, at(9, 10))
    real = h.source.session_candles
    calls = {"n": 0}

    def flaky(contract: object, day: object) -> object:
        calls["n"] += 1
        if calls["n"] <= 3:
            raise DataUnavailableError("feed lagging")
        return real(contract, day)  # type: ignore[arg-type]

    h.source.session_candles = flaky  # type: ignore[method-assign,assignment]
    outcome = h.runner().run()
    assert outcome.exit_code == 0
    assert h.store.positions_for_day(DAY)
