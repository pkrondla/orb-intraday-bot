"""A whole session through the live runner with the dry-run executor."""

from __future__ import annotations

from datetime import datetime, time
from pathlib import Path

from orb_intraday_bot.execution.account import DryRunAccount
from orb_intraday_bot.execution.dry_run_venue import DryRunVenue
from orb_intraday_bot.indicators.pivots import pivots_from_session
from orb_intraday_bot.options.planner import OptionPlanner
from orb_intraday_bot.paper.models import ExitReason, Position
from orb_intraday_bot.risk.engine import RiskEngine
from orb_intraday_bot.risk.limits import RiskLimits
from orb_intraday_bot.runtime.day import TradingDay
from orb_intraday_bot.runtime.executor import LiveExecutor
from orb_intraday_bot.runtime.live import LiveRunner
from orb_intraday_bot.runtime.synthetic import SyntheticOptions
from tests.execution_support import AutoQuotes, FakeAccount
from tests.helpers import candle
from tests.runtime_support import CONTRACT, DAY, LOT, RUN_CFG, Harness, at, prev_session
from tests.unit.test_paper import make_trade


class Session:
    """A harness plus a dry-run executor, risk engine and hooks, wired the way serve does."""

    def __init__(
        self, tmp_path: Path, start: datetime, *, square_off_at: time | None = time(15, 20)
    ) -> None:
        self.h = Harness(tmp_path, start)
        self.quotes = AutoQuotes()
        self.limits = RiskLimits()
        self.risk = RiskEngine(limits=self.limits)
        self.risk.reset_day(DAY)
        self.alerts: list[str] = []
        self.opened: list[Position] = []
        self.closed: list[Position] = []
        self.enabled = True
        self.idles = 0
        venue = DryRunVenue(self.quotes, self.h.clock)
        self.venue = venue
        self.executor = LiveExecutor(
            mode="dry_run", venue=venue, quotes=self.quotes,
            account=DryRunAccount(venue, FakeAccount()), limits=self.limits,
            clock=self.h.clock, sleep=self.h.sleep, risk=self.risk, store=self.h.store,
            notify=self.alerts.append,
        )
        self.square_off_at = square_off_at

    def runner(self, **extra: object) -> LiveRunner:
        def on_open(p: Position) -> None:
            self.opened.append(p)
            self.risk.record_open()

        def on_close(p: Position) -> None:
            self.closed.append(p)
            self.risk.record_close(p.net_option_pnl)

        def on_idle() -> None:
            self.idles += 1

        return LiveRunner(
            RUN_CFG, self.h.data_dir, self.h.source, self.h.store, self.h.notes, self.h.clock,
            self.h.sleep, self.h.lines.append,
            entries_allowed=lambda: self.risk.check_entry(lots=1).allowed,
            executor=self.executor, executor_enabled=lambda: self.enabled,
            on_open=on_open, on_close=on_close, square_off_at=self.square_off_at,
            on_idle=on_idle, **extra,  # type: ignore[arg-type]
        )


def test_a_whole_session_runs_the_dry_run_pipeline_and_ends_flat(tmp_path: Path) -> None:
    s = Session(tmp_path, at(9, 10))
    outcome = s.runner().run()
    assert outcome.exit_code == 0
    assert "DRY RUN" in s.h.text                             # the banner names the mode
    assert s.opened, "the real morning has a trade; the pipeline should have taken it"
    assert all(p.order_mode == "dry_run" for p in s.opened)
    assert s.risk.state.trades_today == len(s.opened)
    assert s.risk.state.open_positions == 0 and not s.h.store.open_positions()
    rows = s.h.store.orders_for_signal(s.opened[0].position_id)
    assert {r["leg"] for r in rows} >= {"ENTRY", "PROTECT"}
    assert {r["mode"] for r in rows} == {"dry_run"}
    assert s.venue.net_quantities() == {}                    # nothing left in the ledger
    assert s.idles > 0                                       # operator commands get a turn
    # A nearer T1 can fill the broker BE stop a poll before the bot books it; reconcile
    # may shout once. The session is still flat (store and venue, above).


def test_the_square_off_time_closes_positions_and_blocks_new_entries(tmp_path: Path) -> None:
    s = Session(tmp_path, at(9, 10), square_off_at=time(9, 39))
    outcome = s.runner().run()
    assert outcome.exit_code == 0
    assert len(s.opened) == 1
    position = s.opened[0]
    assert [f.reason for f in position.fills] == [ExitReason.SQUARE_OFF]
    assert time(9, 39) <= position.fills[0].time.time() < time(9, 42)   # right at the time
    assert not position.is_open and not s.h.store.open_positions()
    assert s.risk.state.open_positions == 0 and len(s.closed) == 1


def test_paper_positions_ignore_the_square_off_time(tmp_path: Path) -> None:
    s = Session(tmp_path, at(9, 10), square_off_at=time(9, 39))
    s.enabled = False                                        # not armed: plain paper trading
    outcome = s.runner().run()
    assert outcome.exit_code == 0
    assert s.opened == []                                    # the hooks are for executed trades
    rows = s.h.store.positions_for_day(DAY)
    assert rows and all(p.order_mode == "paper" for p in rows)
    assert s.h.store.orders_for_signal(rows[0].position_id) == []


def test_the_banner_for_a_paper_session_says_so(tmp_path: Path) -> None:
    h = Harness(tmp_path, at(9, 10))
    h.runner().run()
    assert "PAPER MODE" in h.text


# ---- the runner's own duties around orders ------------------------------------------------
class _SellsRest(DryRunVenue):
    """A dry-run venue whose sells never fill, to model a market that will not take the sale."""

    block_sells = False

    def _match_price(self, record, quote):  # type: ignore[no-untyped-def]
        if self.block_sells and record.intent.side == "SELL" and record.intent.order_type != "SL":
            return None
        return super()._match_price(record, quote)


def _primed(tmp_path: Path, *, at_time: datetime | None = None) -> tuple[
    Session, LiveRunner, TradingDay, Position
]:
    """A runner with an open dry-run position, without running a whole session."""
    s = Session(tmp_path, at_time or at(15, 21))
    venue = _SellsRest(s.quotes, s.h.clock)
    s.venue = venue
    s.executor = LiveExecutor(
        mode="dry_run", venue=venue, quotes=s.quotes,
        account=DryRunAccount(venue, FakeAccount()), limits=s.limits, clock=s.h.clock,
        sleep=s.h.sleep, risk=s.risk, store=s.h.store, notify=s.alerts.append,
    )
    runner = s.runner()
    trading = TradingDay(
        RUN_CFG, DAY, CONTRACT.trading_symbol, pivots_from_session(prev_session()),
        OptionPlanner(RUN_CFG, SyntheticOptions(LOT)), s.h.store,
        executor=s.executor, executor_enabled=lambda: s.enabled,
    )
    trading.last_candle = candle(at(15, 15), 22800, 22810, 22790, 22800)
    trade, plan = make_trade()
    position = trading.paper.open(trade, plan, at(9, 36))
    assert position is not None
    runner._day = trading
    runner._contract = CONTRACT
    return s, runner, trading, position


def test_past_square_off_only_applies_when_orders_are_being_carried_out(
    tmp_path: Path,
) -> None:
    s, runner, _, _ = _primed(tmp_path, at_time=at(15, 19))
    assert not runner._past_square_off()
    s.h.clock.set(at(15, 20))
    assert runner._past_square_off()
    s.enabled = False                                         # paper: the normal 15:30 close
    assert not runner._past_square_off()


def test_a_flatten_whose_sale_fails_stays_pending_and_is_retried(tmp_path: Path) -> None:
    s, runner, trading, position = _primed(tmp_path)
    runner._flatten_requested = lambda: True
    marker = s.h.data_dir / "FLATTEN"
    marker.write_text("operator\n", encoding="utf-8")
    assert isinstance(s.venue, _SellsRest)
    s.venue.block_sells = True
    runner._maybe_flatten(trading)
    assert position.is_open and runner._flatten_announced and marker.exists()
    s.venue.block_sells = False
    runner._maybe_flatten(trading)
    assert not position.is_open and not runner._flatten_announced and not marker.exists()
    assert [f.reason for f in position.fills] == [ExitReason.FLATTEN]

def test_a_square_off_whose_sale_fails_is_retried_on_the_next_poll(tmp_path: Path) -> None:
    s, runner, trading, position = _primed(tmp_path)
    assert isinstance(s.venue, _SellsRest)
    s.venue.block_sells = True
    runner._maybe_square_off(trading)
    assert position.is_open
    assert s.alerts and any("incomplete" in a for a in s.alerts)
    s.venue.block_sells = False
    runner._maybe_square_off(trading)
    assert not position.is_open
    assert [f.reason for f in position.fills] == [ExitReason.SQUARE_OFF]


def test_the_session_end_alerts_loudly_about_a_position_still_open(tmp_path: Path) -> None:
    s, runner, trading, position = _primed(tmp_path)
    runner._finish(trading, DAY)
    assert position.is_open
    assert "CRITICAL" in s.h.notes.joined() and position.trading_symbol in s.h.notes.joined()


def test_the_runner_reconciles_open_positions_against_the_ledger(tmp_path: Path) -> None:
    s, runner, trading, _ = _primed(tmp_path)
    assert isinstance(s.venue, _SellsRest)
    runner._reconcile(trading)
    assert not s.risk.state.entries_blocked                    # the books agree
    s.h.clock.advance(seconds=120)
    s.venue._net.clear()                                       # the ledger loses the position
    runner._reconcile(trading)
    assert s.risk.state.entries_blocked
    assert any("CRITICAL" in a for a in s.alerts)


def test_a_reconcile_that_raises_is_logged_not_fatal(tmp_path: Path) -> None:
    s, runner, trading, _ = _primed(tmp_path)

    def boom(*_: object) -> list[str]:
        raise OSError("broker down")

    s.executor.maybe_reconcile = boom                          # type: ignore[method-assign]
    runner._reconcile(trading)                                 # must not raise


def test_an_executor_startup_that_raises_is_logged_not_fatal(tmp_path: Path) -> None:
    s, runner, trading, _ = _primed(tmp_path)

    def boom(*_: object) -> list[str]:
        raise OSError("broker down")

    s.executor.startup = boom                                  # type: ignore[method-assign]
    runner._start_executor(trading)                            # must not raise
