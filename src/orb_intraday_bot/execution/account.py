"""What the executor reads about the trading account: funds and positions.

Both are best-effort. A reader that cannot tell returns None, and the caller then does not guess:
a missing margin figure does not block an entry (the broker rejects an unaffordable order), and a
missing positions list is reported as "could not reconcile", never as a mismatch.
"""

from __future__ import annotations

from typing import Protocol

from orb_intraday_bot.execution.dry_run_venue import DryRunVenue


class AccountReader(Protocol):
    def available_option_margin(self) -> float | None:
        """Cash available to buy options, in rupees, or None if unknown."""
        ...

    def net_quantities(self) -> dict[str, int] | None:
        """Net open quantity per trading symbol, or None if the broker could not be read."""
        ...


class DryRunAccount:
    """The dry run's account: funds from the real reader, positions from the simulated ledger.

    A dry run places no order, so the broker's own positions never include its trades. The
    reconciliation therefore compares the bot with the venue's ledger instead, through the very
    same code path.
    """

    def __init__(self, venue: DryRunVenue, funds: AccountReader | None = None) -> None:
        self._venue = venue
        self._funds = funds

    def available_option_margin(self) -> float | None:
        return self._funds.available_option_margin() if self._funds is not None else None

    def net_quantities(self) -> dict[str, int] | None:
        return self._venue.net_quantities()
