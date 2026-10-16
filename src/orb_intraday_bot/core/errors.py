"""Error taxonomy: every failure belongs to a family, and each family has one reaction.

The runner does not guess from the exception text. It asks `reaction_for(exc)`:

    RETRY            a data or network problem: wait, try again, alert if it lasts (watchdog)
    REJECT_SIGNAL    this one trade cannot be planned: record why, carry on with the day
    STOP             do not continue: save state, alert, exit (safety, configuration, a bug)
    FLATTEN_AND_STOP reserved for the risk engine and broker layer (Phases 15 and 16): close
                     everything and halt

Unknown exceptions (an SDK error, a parse error) get the default of the place that caught them:
a retry in the data path, a stop elsewhere. Classes keep their old built-in bases (`ValueError`,
`RuntimeError`) so existing `except` clauses keep working.
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar


class Reaction(StrEnum):
    RETRY = "retry"
    REJECT_SIGNAL = "reject_signal"
    STOP = "stop"
    FLATTEN_AND_STOP = "flatten_and_stop"


class BotError(Exception):
    reaction: ClassVar[Reaction] = Reaction.STOP


class DataUnavailableError(BotError):
    """A quote, candle, chain or login is not available right now (retryable)."""

    reaction: ClassVar[Reaction] = Reaction.RETRY


class TradePlanError(BotError):
    """A signal cannot be turned into a valid trade (stop, target, strike, price, size)."""

    reaction: ClassVar[Reaction] = Reaction.REJECT_SIGNAL


class SafetyViolationError(BotError):
    """A safety rule is broken (paper-only guard, order method reachable). Never continue."""

    reaction: ClassVar[Reaction] = Reaction.STOP


class ConfigurationError(BotError):
    """Missing or invalid configuration or credentials. Fix it, then start again."""

    reaction: ClassVar[Reaction] = Reaction.STOP


class BrokerRejectionError(BotError):
    """The broker refused an order. Reserved for Phase 15."""

    reaction: ClassVar[Reaction] = Reaction.REJECT_SIGNAL


class EntryNotFilledError(TradePlanError):
    """The entry did not happen: vetoed by risk, rejected, or not filled in time (reason inside).

    The trade is released and the day carries on. Nothing is left open at the broker.
    """


class ExitFailedError(BotError):
    """An exit order did not complete. The position is still open; the exit is retried."""

    reaction: ClassVar[Reaction] = Reaction.RETRY


class RiskBreachError(BotError):
    """A risk limit was hit. Reserved for Phase 16."""

    reaction: ClassVar[Reaction] = Reaction.FLATTEN_AND_STOP


def reaction_for(exc: BaseException, default: Reaction = Reaction.STOP) -> Reaction:
    """The reaction for `exc`: its own if it is a `BotError`, else `default`."""
    if isinstance(exc, BotError):
        return exc.reaction
    return default


def must_halt(exc: BaseException, default: Reaction = Reaction.RETRY) -> bool:
    """True if the reaction to `exc` is to stop the run (`default` applies to unknown errors)."""
    return reaction_for(exc, default) in {Reaction.STOP, Reaction.FLATTEN_AND_STOP}
